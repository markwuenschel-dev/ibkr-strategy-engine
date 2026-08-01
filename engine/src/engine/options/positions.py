"""Open option structures, persisted so a restart does not lose the book.

Until this module existed the engine could open a position and then forget it,
which is the single worst failure available to a strategy that manages positions
on a schedule: a spread nobody is watching still expires, still gets assigned,
and still loses the full width.

**Event-sourced, not a mutable record.** The store is an append-only JSONL log of
lifecycle events, and current state is rebuilt by replaying it. Deliberately the
same shape as :mod:`engine.journal`, and for the same reason -- an in-place
"positions.json" that is rewritten on every change has a window during which a
crash leaves it truncated, and the thing lost is the record of a position that
exists in the market whether or not the file mentions it. Appending cannot lose
an earlier fact.

**Writes are fatal on failure.** Inherited from the journal's contract: an
engine that cannot record that it opened a position must not open one. The
recording therefore happens *before* transmission, as an ``OPENING`` intent, and
is confirmed afterwards. A crash between the two leaves a position marked
``OPENING`` that reconciliation will find and resolve against the broker -- which
is recoverable. The reverse order is not: a fill with no record is a position
nobody knows about.

**Reconciliation is a comparison, never a rewrite.** ``reconcile_against_broker``
reports differences and refuses to guess. If the broker holds a structure the
store does not, that is a fact to surface loudly, not something to silently
adopt -- adopting it would mean inventing an entry credit and a maximum loss that
nothing ever validated.

**This engine does not own the account.** Reconciliation is scoped to orders it
can prove are its own -- by ``orderRef`` (a strategy id it minted) or by a
``permId`` it persisted. A resting order placed by hand in TWS, or by another
tool, is ignored rather than reported: treating it as a disagreement would block
every entry for as long as it rested, and no amount of correct engine behaviour
would clear it.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

from ..errors import InvalidPortfolioStateError, JournalError
from .domain import (
    OptionLegIntent,
    OptionRight,
    OptionStrategyIntent,
    OrderAction,
    PriceEffect,
    StrategyAction,
    StrategyType,
)
from .portfolio import PositionExposure

__all__ = [
    "PositionState",
    "PositionEvent",
    "OpenPosition",
    "PositionStore",
    "ReconciliationReport",
    "ReconciliationOutcome",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = 1


class PositionState(str, Enum):
    """Where a structure is in its life.

    ``OPENING`` and ``CLOSING`` are real, persisted states rather than transient
    in-memory ones. They are exactly the states a crash can strand a position in,
    and a state machine that cannot represent "we sent it and do not yet know"
    forces the reconciler to guess.
    """

    OPENING = "OPENING"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ROLLED = "ROLLED"
    #: The outcome of a transmitted order is unknown -- a timeout, a disconnect,
    #: or a status this engine does not recognise. Deliberately distinct from
    #: both OPEN and CLOSED: there may or may not be a position, and the only
    #: safe response is to stop opening new risk and reconcile. It counts as
    #: live, because something may well be in the market.
    UNCERTAIN = "UNCERTAIN"


class PositionEvent(str, Enum):
    """The append-only vocabulary. Replaying these rebuilds the book.

    ``*_ACKNOWLEDGED`` and ``*_PARTIAL`` exist because a restart can land between
    any two of these, and a vocabulary that cannot say "the broker has it, id
    12345, nothing filled yet" forces the reconciler to guess between "never
    sent" and "filled and lost". ``*_UNCERTAIN`` is the state a timeout or a
    disconnect leaves behind: not a failure, not a fill, and specifically not
    something to retry.
    """

    OPEN_SUBMITTED = "OPEN_SUBMITTED"
    OPEN_ACKNOWLEDGED = "OPEN_ACKNOWLEDGED"
    OPEN_PARTIAL = "OPEN_PARTIAL"
    OPEN_FILLED = "OPEN_FILLED"
    OPEN_FAILED = "OPEN_FAILED"
    OPEN_UNCERTAIN = "OPEN_UNCERTAIN"
    CLOSE_SUBMITTED = "CLOSE_SUBMITTED"
    CLOSE_ACKNOWLEDGED = "CLOSE_ACKNOWLEDGED"
    CLOSE_PARTIAL = "CLOSE_PARTIAL"
    CLOSE_FILLED = "CLOSE_FILLED"
    CLOSE_FAILED = "CLOSE_FAILED"
    CLOSE_UNCERTAIN = "CLOSE_UNCERTAIN"
    ROLLED = "ROLLED"
    #: The broker's own executions and commission reports for a fill, captured
    #: after the fact. A separate event rather than a richer ``*_FILLED`` because
    #: the commission genuinely arrives later than the fill -- IBKR delivers
    #: ``commissionReport`` on its own callback, and the first real fill this
    #: engine took recorded ``commission=None`` because nothing ever went back
    #: for it. An append-only log can record a fact learned late; it cannot
    #: rewrite the line that did not know it yet.
    EXECUTIONS_RECORDED = "EXECUTIONS_RECORDED"


def _refuse(message: str, *, hint: str | None = None) -> None:
    raise InvalidPortfolioStateError(message, hint=hint)


@dataclass(frozen=True)
class OpenPosition:
    """One structure the engine believes it holds.

    Carries the whole validated :class:`OptionStrategyIntent` rather than a
    flattened copy of its fields, so a close is always built from the exact legs
    that were opened -- see
    :meth:`engine.options.domain.OptionStrategyIntent.closing_intent`.
    """

    strategy_id: UUID
    intent: OptionStrategyIntent
    opened_at: dt.datetime
    state: PositionState
    buying_power_reserved: Decimal
    filled_credit: Decimal
    closed_at: dt.datetime | None = None
    closing_debit: Decimal | None = None
    rolled_to: UUID | None = None

    # -- broker order identity -------------------------------------------
    #
    # Both are kept. ``order_id`` is client-assigned and unique only within a
    # session; ``perm_id`` is IBKR's durable identifier and is the one that
    # survives a restart. A reconciler holding only the first cannot match an
    # order it can plainly see in a fresh session.
    open_order_id: int | None = None
    open_perm_id: int | None = None
    close_order_id: int | None = None
    close_perm_id: int | None = None
    #: Quantity actually filled on the opening order. Below ``quantity`` means a
    #: partial fill: the position is real but smaller than intended.
    filled_quantity: Decimal = Decimal("0")
    #: Quantity actually filled on the *closing* order, which is a separate order
    #: with its own independent fill count. Kept because the exit can partially
    #: fill too, and when it does the difference from ``filled_quantity`` is the
    #: only thing that says how many contracts are still held. Without it a
    #: cancelled-after-partial exit leaves the store unable to answer "how much of
    #: this position did we actually get out of", which is the mirror of the
    #: opening-side defect recorded as C21.
    close_filled_quantity: Decimal = Decimal("0")
    commission: Decimal | None = None
    #: Whether ``commission`` is the **whole** cost of the opening fill, proven
    #: leg by leg, rather than however much of it happened to arrive. Carried
    #: separately because ``commission`` alone cannot express the difference: a
    #: two-legged spread with one leg costed holds a real, finite, too-small
    #: number, and net profit computed against it is wrong in the flattering
    #: direction with nothing about it looking wrong. See
    #: :mod:`engine.options.executions`, which is what establishes it.
    commission_complete: bool = False
    #: Why the outcome is unknown, when ``state`` is UNCERTAIN.
    uncertainty: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.intent, OptionStrategyIntent):
            _refuse(f"intent must be an OptionStrategyIntent, got {type(self.intent).__name__}")
        if self.intent.strategy_action is not StrategyAction.OPEN:
            _refuse(
                f"a stored position must hold its OPEN intent, got "
                f"{self.intent.strategy_action.value}"
            )
        if self.intent.strategy_id != self.strategy_id:
            _refuse(
                f"strategy_id {self.strategy_id} does not match its intent "
                f"{self.intent.strategy_id}"
            )
        if not isinstance(self.state, PositionState):
            _refuse(f"state must be a PositionState, got {self.state!r}")
        if not isinstance(self.opened_at, dt.datetime) or self.opened_at.tzinfo is None:
            _refuse("opened_at must be a timezone-aware datetime")
        if not isinstance(self.filled_credit, Decimal) or self.filled_credit <= 0:
            _refuse(
                f"filled_credit must be a positive Decimal, got {self.filled_credit!r}",
                hint="a credit structure that collected nothing has no upside to "
                "justify its risk",
            )
        if not isinstance(self.buying_power_reserved, Decimal):
            _refuse("buying_power_reserved must be a Decimal")
        if self.buying_power_reserved < 0:
            _refuse(f"buying_power_reserved must not be negative, got {self.buying_power_reserved}")
        if self.state is PositionState.CLOSED and self.closed_at is None:
            _refuse("a CLOSED position must record when it closed")
        if self.state is PositionState.ROLLED and self.rolled_to is None:
            _refuse("a ROLLED position must name the strategy it rolled into")
        if self.state is PositionState.UNCERTAIN and not (self.uncertainty or "").strip():
            _refuse(
                "an UNCERTAIN position must record why the outcome is unknown",
                hint="an unexplained uncertainty is indistinguishable from a bug, "
                "and the operator has to know whether to look at the socket or "
                "at the order",
            )
        if not isinstance(self.filled_quantity, Decimal):
            _refuse(
                f"filled_quantity must be a Decimal, got "
                f"{type(self.filled_quantity).__name__}"
            )
        if self.filled_quantity < 0:
            _refuse(f"filled_quantity must not be negative, got {self.filled_quantity}")
        if self.filled_quantity > Decimal(self.intent.quantity):
            _refuse(
                f"filled_quantity {self.filled_quantity} exceeds the ordered "
                f"quantity {self.intent.quantity}",
                hint="a fill larger than the order is a parsing error, and sizing "
                "an exit off it would sell contracts that were never bought",
            )
        if not isinstance(self.close_filled_quantity, Decimal):
            _refuse(
                f"close_filled_quantity must be a Decimal, got "
                f"{type(self.close_filled_quantity).__name__}"
            )
        if self.close_filled_quantity < 0:
            _refuse(
                f"close_filled_quantity must not be negative, got "
                f"{self.close_filled_quantity}"
            )
        # Bounded by the *ordered* quantity rather than by ``filled_quantity``.
        # Replay applies events in journal order, and a CLOSE_PARTIAL can legally
        # be read before an OPEN_FILLED that has not yet raised the opening count.
        # Validating against the tighter bound would raise inside ``_replace``,
        # where ``positions()`` records it as a problem and *drops the
        # transition* -- losing the very fill this field exists to keep.
        if self.close_filled_quantity > Decimal(self.intent.quantity):
            _refuse(
                f"close_filled_quantity {self.close_filled_quantity} exceeds the "
                f"ordered quantity {self.intent.quantity}",
                hint="closing more than was ever ordered is a parsing error",
            )
        if not isinstance(self.commission_complete, bool):
            _refuse(
                f"commission_complete must be a bool, got "
                f"{type(self.commission_complete).__name__}"
            )
        if self.commission_complete and self.commission is None:
            _refuse(
                "commission_complete is set but no commission is recorded",
                hint="'the whole cost is known' and 'the cost is unknown' cannot "
                "both be true; a complete claim with no number is what would let "
                "net profit be computed against nothing",
            )

    # -- derived ----------------------------------------------------------

    @property
    def underlying(self) -> str:
        return self.intent.underlying.strip().upper()

    @property
    def strategy_type(self) -> StrategyType:
        return self.intent.strategy_type

    @property
    def expiration(self) -> dt.date:
        return self.intent.expiration

    @property
    def quantity(self) -> int:
        return self.intent.quantity

    @property
    def multiplier(self) -> int:
        return self.intent.multiplier

    @property
    def legs(self) -> tuple[OptionLegIntent, ...]:
        return self.intent.legs

    @property
    def total_maximum_loss(self) -> Decimal:
        return self.intent.total_maximum_loss

    @property
    def is_live(self) -> bool:
        """States in which the market can still move against this position.

        ``UNCERTAIN`` counts. It is exactly the state in which we do not know
        whether something is in the market, and treating "might be" as "is not"
        is how a real position stops being managed.
        """
        return self.state in (
            PositionState.OPENING,
            PositionState.OPEN,
            PositionState.CLOSING,
            PositionState.UNCERTAIN,
        )

    @property
    def is_uncertain(self) -> bool:
        """The outcome of a transmitted order is unresolved.

        The runner blocks new entries while any position is in this state: an
        order that may be resting in the book must be resolved before another is
        sent, or one intended position becomes two.
        """
        return self.state is PositionState.UNCERTAIN

    @property
    def is_partially_filled(self) -> bool:
        """Filled for less than the intended quantity.

        A real position, smaller than asked for. It must still be managed, and
        its exit must be sized to what actually filled rather than to what was
        requested -- closing three contracts of a one-contract fill opens a
        naked short.
        """
        return (
            self.filled_quantity > Decimal("0")
            and self.filled_quantity < Decimal(self.quantity)
        )

    @property
    def manageable_quantity(self) -> int:
        """How many contracts an exit should actually close.

        The filled quantity when that is known and positive, otherwise the
        intended quantity. Sizing an exit off the intent after a partial fill is
        how a defensive close becomes an opening trade in the other direction.
        """
        if self.filled_quantity > Decimal("0"):
            return int(self.filled_quantity)
        return self.quantity

    @property
    def is_partially_closed(self) -> bool:
        """The exit filled for less than what is held, and stopped there.

        A real and dangerous shape: some contracts are out, the rest are not,
        and the two numbers that say so live on different orders.
        """
        return (
            self.close_filled_quantity > Decimal("0")
            and self.close_filled_quantity < self.filled_quantity
        )

    @property
    def remaining_quantity(self) -> Decimal:
        """Contracts still held: what the open filled, less what the close did.

        Reported, never yet used to size an order. Sizing an exit off this is
        the obvious next step and is deliberately **not** taken here -- a
        position mid-close is held by ``decide_management_action``
        (``lifecycle.py``) precisely so a second close cannot double the order,
        and changing that is a lifecycle decision rather than a storage one.
        This property exists so the number is *available* and durable when that
        decision is made, instead of being unrecoverable after the fact.
        """
        remaining = self.filled_quantity - self.close_filled_quantity
        return remaining if remaining > Decimal("0") else Decimal("0")

    def dte(self, today: dt.date) -> int:
        """Calendar days to expiry. Needs no market data, which is why the
        21-DTE rule keeps working when the quote feed does not."""
        return (self.expiration - today).days

    def exposure(self) -> PositionExposure:
        """The governor's view of this position."""
        return PositionExposure(
            underlying=self.underlying,
            buying_power_reserved=self.buying_power_reserved,
            maximum_loss=self.total_maximum_loss,
            strategy_id=self.strategy_id,
        )

    def describe(self) -> str:
        return (
            f"{self.state.value:<8} {self.underlying:<6} "
            f"{self.strategy_type.value} {self.expiration} "
            f"x{self.quantity} @ {self.filled_credit} "
            f"[max loss {self.total_maximum_loss}]"
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "strategy_id": str(self.strategy_id),
            "state": self.state.value,
            "underlying": self.underlying,
            # Emitted even though `record_open_submitted` also injects it.
            # `from_record` reads this key, so a record produced by `to_record`
            # alone must be reloadable -- otherwise the two are not inverses and
            # a future writer that reuses `to_record` produces lines that fail to
            # replay, which `positions()` would swallow as a vanished position.
            "entry_credit": str(self.intent.limit_price),
            "configuration_version": self.intent.configuration_version,
            "strategy_type": self.strategy_type.value,
            "expiration": self.expiration.isoformat(),
            "quantity": self.quantity,
            "multiplier": self.multiplier,
            "filled_credit": str(self.filled_credit),
            "buying_power_reserved": str(self.buying_power_reserved),
            "maximum_loss_per_contract": str(self.intent.maximum_loss_per_contract),
            "total_maximum_loss": str(self.total_maximum_loss),
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "closing_debit": str(self.closing_debit) if self.closing_debit is not None else None,
            "rolled_to": str(self.rolled_to) if self.rolled_to else None,
            "open_order_id": self.open_order_id,
            "open_perm_id": self.open_perm_id,
            "close_order_id": self.close_order_id,
            "close_perm_id": self.close_perm_id,
            "filled_quantity": str(self.filled_quantity),
            "close_filled_quantity": str(self.close_filled_quantity),
            "commission": str(self.commission) if self.commission is not None else None,
            "commission_complete": self.commission_complete,
            "uncertainty": self.uncertainty,
            "legs": [
                {
                    "con_id": leg.con_id,
                    "symbol": leg.symbol,
                    "expiration": leg.expiration.isoformat(),
                    "strike": str(leg.strike),
                    "right": leg.right.value,
                    "action": leg.action.value,
                    "ratio": leg.ratio,
                    "multiplier": leg.multiplier,
                    "exchange": leg.exchange,
                    "trading_class": leg.trading_class,
                }
                for leg in self.legs
            ],
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "OpenPosition":
        """Rebuild from a persisted record.

        Every field is reconstructed through the domain constructors, so a
        tampered or corrupted line fails to load rather than producing a
        position whose maximum loss disagrees with its legs.
        """
        legs = tuple(
            OptionLegIntent(
                con_id=int(leg["con_id"]),
                symbol=str(leg["symbol"]),
                expiration=dt.date.fromisoformat(leg["expiration"]),
                strike=Decimal(str(leg["strike"])),
                right=OptionRight(leg["right"]),
                action=OrderAction(leg["action"]),
                ratio=int(leg["ratio"]),
                multiplier=int(leg["multiplier"]),
                exchange=str(leg["exchange"]),
                trading_class=leg.get("trading_class"),
            )
            for leg in record["legs"]
        )
        strategy_id = UUID(record["strategy_id"])
        intent = OptionStrategyIntent(
            strategy_id=strategy_id,
            strategy_type=StrategyType(record["strategy_type"]),
            strategy_action=StrategyAction.OPEN,
            underlying=str(record["underlying"]),
            quantity=int(record["quantity"]),
            legs=legs,
            expiration=dt.date.fromisoformat(record["expiration"]),
            limit_price=Decimal(str(record["entry_credit"])),
            price_effect=PriceEffect.CREDIT,
            maximum_loss_per_contract=Decimal(str(record["maximum_loss_per_contract"])),
            configuration_version=str(record.get("configuration_version", "restored")),
            created_at=dt.datetime.fromisoformat(record["opened_at"]),
        )
        return cls(
            strategy_id=strategy_id,
            intent=intent,
            opened_at=dt.datetime.fromisoformat(record["opened_at"]),
            state=PositionState(record["state"]),
            buying_power_reserved=Decimal(str(record["buying_power_reserved"])),
            filled_credit=Decimal(str(record["filled_credit"])),
            closed_at=(
                dt.datetime.fromisoformat(record["closed_at"])
                if record.get("closed_at")
                else None
            ),
            closing_debit=(
                Decimal(str(record["closing_debit"]))
                if record.get("closing_debit") is not None
                else None
            ),
            rolled_to=UUID(record["rolled_to"]) if record.get("rolled_to") else None,
            open_order_id=_int_or_none(record.get("open_order_id")),
            open_perm_id=_int_or_none(record.get("open_perm_id")),
            close_order_id=_int_or_none(record.get("close_order_id")),
            close_perm_id=_int_or_none(record.get("close_perm_id")),
            filled_quantity=(
                _decimal_or_none(record.get("filled_quantity")) or Decimal("0")
            ),
            # Absent from every line written before this field existed, which is
            # why it defaults rather than raising: an older journal must still
            # replay, and "we never recorded a closing fill" is exactly zero.
            close_filled_quantity=(
                _decimal_or_none(record.get("close_filled_quantity")) or Decimal("0")
            ),
            commission=_decimal_or_none(record.get("commission")),
            # Absent from every line written before the field existed, and
            # defaulting to False rather than raising for the same reason
            # ``close_filled_quantity`` does: an older journal must still replay,
            # and "we never proved the cost" is exactly what those lines mean.
            commission_complete=bool(record.get("commission_complete"))
            and _decimal_or_none(record.get("commission")) is not None,
            uncertainty=record.get("uncertainty") or None,
        )


@dataclass(frozen=True)
class ReconciliationReport:
    """What the store and the broker disagree about.

    Carries no repair. Every field is a difference for a human or a later,
    deliberately-built reconciler to resolve -- see the module docstring.
    """

    checked_at: dt.datetime
    known_open: tuple[UUID, ...] = ()
    stranded_opening: tuple[UUID, ...] = ()
    stranded_closing: tuple[UUID, ...] = ()
    missing_at_broker: tuple[UUID, ...] = ()
    unknown_at_broker: tuple[str, ...] = ()
    replay_errors: tuple[str, ...] = ()
    #: Store positions mid-transition whose order the broker is **confirmed to
    #: be working**. Not a defect and not an absence: the order is alive and can
    #: still fill. Reported so the operator is told what is actually true.
    orders_working_at_broker: tuple[UUID, ...] = ()
    #: Store positions mid-transition whose order the broker was asked about and
    #: is not working. Either it filled unobserved or it was never accepted --
    #: opposite fixes. Only ever populated when the broker really was asked.
    orders_absent_at_broker: tuple[UUID, ...] = ()
    #: Store positions mid-transition that could not be checked at all, because
    #: the caller supplied no open-order enumeration. Distinct from *absent* on
    #: purpose: "I did not ask" is not evidence that nothing is working, and
    #: reporting it as absence is exactly the false claim this field removes.
    orders_unverified_at_broker: tuple[UUID, ...] = ()
    #: Broker orders **this engine owns** that the live book cannot account for.
    #: Orders belonging to anything else on the account are ignored entirely --
    #: see :meth:`PositionStore._reconcile_orders`.
    orders_unknown_to_store: tuple[int, ...] = ()

    @property
    def agrees(self) -> bool:
        """True only when the book is both fully readable and matched.

        ``replay_errors`` counts. A book that could not be fully replayed is not
        a book the engine understands, and "the parts I could read match" is not
        agreement.

        ``orders_working_at_broker`` deliberately does **not** count. A working
        order is the broker and the store agreeing about a live order; the
        position it belongs to is already reported as stranded OPENING/CLOSING
        or as uncertain, and those are what block an entry. Counting it here as
        well would say the two sides disagree about the one thing they agree on.
        """
        return not (
            self.stranded_opening
            or self.stranded_closing
            or self.missing_at_broker
            or self.unknown_at_broker
            or self.replay_errors
            or self.orders_absent_at_broker
            or self.orders_unverified_at_broker
            or self.orders_unknown_to_store
        )

    def describe(self) -> str:
        if self.agrees:
            return f"  reconciled: {len(self.known_open)} open position(s), broker agrees"
        lines = [f"  reconciled: {len(self.known_open)} open position(s), DISAGREEMENT"]
        if self.replay_errors:
            lines.append(
                f"    UNREADABLE EVENTS {list(self.replay_errors)} "
                "-- the position log could not be fully replayed"
            )
        if self.stranded_opening:
            lines.append(
                f"    stranded OPENING  {[str(i) for i in self.stranded_opening]} "
                "-- sent but never confirmed; check TWS before trading"
            )
        if self.stranded_closing:
            lines.append(
                f"    stranded CLOSING  {[str(i) for i in self.stranded_closing]} "
                "-- close sent but never confirmed"
            )
        if self.missing_at_broker:
            lines.append(
                f"    missing at broker {[str(i) for i in self.missing_at_broker]} "
                "-- the store thinks these are open and the broker does not"
            )
        if self.unknown_at_broker:
            lines.append(
                f"    unknown structures at broker on {list(self.unknown_at_broker)} "
                "-- not opened by this engine, or opened before the store existed"
            )
        if self.orders_working_at_broker:
            lines.append(
                f"    ORDERS WORKING    {[str(i) for i in self.orders_working_at_broker]} "
                "-- transmitted, and the broker IS working them; unfilled and live, "
                "so they can be repriced or cancelled rather than chased"
            )
        if self.orders_absent_at_broker:
            lines.append(
                f"    ORDERS ABSENT     {[str(i) for i in self.orders_absent_at_broker]} "
                "-- transmitted, the broker was asked, and it is not working them; "
                "either they filled unobserved or were never accepted"
            )
        if self.orders_unverified_at_broker:
            lines.append(
                "    ORDERS UNVERIFIED "
                f"{[str(i) for i in self.orders_unverified_at_broker]} "
                "-- transmitted, and the broker's open orders were never enumerated; "
                "this says nothing about whether they are working"
            )
        if self.orders_unknown_to_store:
            lines.append(
                f"    ORDERS UNKNOWN    {list(self.orders_unknown_to_store)} "
                "-- this engine's own orderRef, live at the broker, with no live "
                "position to account for them"
            )
        return "\n".join(lines)

    def to_record(self) -> dict[str, Any]:
        return {
            "event": "position_reconciliation",
            "checked_at": self.checked_at.isoformat(),
            "agrees": self.agrees,
            "known_open": [str(i) for i in self.known_open],
            "stranded_opening": [str(i) for i in self.stranded_opening],
            "stranded_closing": [str(i) for i in self.stranded_closing],
            "missing_at_broker": [str(i) for i in self.missing_at_broker],
            "unknown_at_broker": list(self.unknown_at_broker),
            "replay_errors": list(self.replay_errors),
            "orders_working_at_broker": [str(i) for i in self.orders_working_at_broker],
            "orders_absent_at_broker": [str(i) for i in self.orders_absent_at_broker],
            "orders_unverified_at_broker": [
                str(i) for i in self.orders_unverified_at_broker
            ],
            "orders_unknown_to_store": list(self.orders_unknown_to_store),
        }


class ReconciliationOutcome(Enum):
    """Whether the book is understood well enough to open new risk.

    Four states, and **never absent**. The runner used to carry this as a
    ``ReconciliationReport | None`` and read the ``None`` -- the reconciler could
    not be run at all -- as silence rather than as doubt. A broker that refused
    to answer therefore authorised exactly what a broker that answered "you hold
    nothing" would have, which is how a restart could re-send a spread it was
    already holding. Naming the outcome removes the state that had no meaning:
    every path ends on one of these four, and only one of them opens risk.
    """

    #: The broker answered and its answer matches the replayed store.
    RECONCILED = "RECONCILED"
    #: The broker answered and it does not match.
    DISAGREEMENT = "DISAGREEMENT"
    #: The broker could not be asked -- exception, disconnect, or no data.
    UNAVAILABLE = "UNAVAILABLE"
    #: The store could not be replayed cleanly, so there is nothing to compare.
    CORRUPT = "CORRUPT"

    @property
    def may_open_new_risk(self) -> bool:
        """Only a positive answer authorises an entry; everything else is doubt.

        Deliberately not the inverse of "is there a disagreement". Absence of a
        disagreement is not the same as evidence of agreement, and conflating
        the two is the defect this type exists to make unrepresentable.
        """
        return self is ReconciliationOutcome.RECONCILED

    @classmethod
    def for_report(cls, report: ReconciliationReport) -> "ReconciliationOutcome":
        """Classify a reconciliation the broker actually answered.

        ``replay_errors`` outrank the comparison. A book that could not be
        replayed cleanly is not a book whose agreement means anything, and
        calling that CORRUPT rather than DISAGREEMENT points the operator at the
        store rather than at the broker. Both block entries either way.
        """
        if report.replay_errors:
            return cls.CORRUPT
        return cls.RECONCILED if report.agrees else cls.DISAGREEMENT


class PositionStore:
    """Append-only, fsync'd, never-rotated record of every option structure.

    Shares :class:`engine.journal.OrderJournal`'s durability contract on purpose:
    a write that cannot be made durable raises :class:`~engine.errors.JournalError`
    and stops trading. The two files are kept separate because the journal is a
    record of *orders* and this is a record of *positions* -- different lifetimes,
    and one is replayed to rebuild state while the other is never replayed at all.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- writing ----------------------------------------------------------

    def _append(self, record: dict[str, Any]) -> dict[str, Any]:
        import os  # noqa: PLC0415 - kept local, mirrors journal._append

        payload = {"v": SCHEMA_VERSION, **record}
        try:
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except Exception as exc:  # noqa: BLE001 - default=str can raise anything
            raise JournalError(
                f"could not serialize a {record.get('event')!r} position event: "
                f"{type(exc).__name__}: {exc}",
                hint="a position that cannot be recorded must not be opened",
            ) from exc

        data = (line + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                written = os.write(fd, data)
                if written != len(data):  # pragma: no cover - short append
                    raise JournalError(
                        f"short write to the position store ({written}/{len(data)} bytes)"
                    )
                os.fsync(fd)
            finally:
                os.close(fd)
        except JournalError:
            raise
        except OSError as exc:
            raise JournalError(
                f"cannot write the position store at {self.path}: {exc}",
                hint=(
                    "trading is halted: the engine will not open a position it "
                    "cannot record. An unrecorded spread still expires."
                ),
            ) from exc
        return payload

    def record_open_submitted(
        self,
        intent: OptionStrategyIntent,
        *,
        at: dt.datetime,
        buying_power_reserved: Decimal,
    ) -> dict[str, Any]:
        """Persist the intent **before** it is transmitted.

        Ordering is the whole point. A crash after this and before the fill
        leaves an ``OPENING`` record the reconciler can resolve against the
        broker. A crash after a fill with no record leaves a live spread nobody
        knows about, and there is nothing to resolve it against.
        """
        if intent.strategy_action is not StrategyAction.OPEN:
            _refuse("record_open_submitted takes an OPEN intent")
        position = OpenPosition(
            strategy_id=intent.strategy_id,
            intent=intent,
            opened_at=at,
            state=PositionState.OPENING,
            buying_power_reserved=buying_power_reserved,
            filled_credit=intent.limit_price,
        )
        record = position.to_record()
        record["entry_credit"] = str(intent.limit_price)
        record["configuration_version"] = intent.configuration_version
        return self._append(
            {"event": PositionEvent.OPEN_SUBMITTED.value, "at": at.isoformat(), **record}
        )

    def record_acknowledged(
        self,
        strategy_id: UUID,
        *,
        at: dt.datetime,
        closing: bool = False,
        order_id: int | None = None,
        perm_id: int | None = None,
    ) -> dict[str, Any]:
        """The broker has the order and told us its identifiers.

        Written the moment either identifier is known, not when the order
        resolves. This is the record that lets a restart mid-flight match the
        store to a live broker order instead of guessing whether anything was
        ever sent.
        """
        event = (
            PositionEvent.CLOSE_ACKNOWLEDGED if closing else PositionEvent.OPEN_ACKNOWLEDGED
        )
        return self._append(
            {
                "event": event.value,
                "at": at.isoformat(),
                "strategy_id": str(strategy_id),
                "order_id": order_id,
                "perm_id": perm_id,
            }
        )

    def record_partial_fill(
        self,
        strategy_id: UUID,
        *,
        at: dt.datetime,
        filled_quantity: Decimal,
        average_price: Decimal | None = None,
        closing: bool = False,
    ) -> dict[str, Any]:
        """Some quantity is in the market and the rest is still working.

        A distinct event rather than a fill with a smaller number, because the
        two demand different handling: a partial is *not terminal*, and the
        position it creates must be managed at the filled size while the
        remainder may still fill or be cancelled.
        """
        event = PositionEvent.CLOSE_PARTIAL if closing else PositionEvent.OPEN_PARTIAL
        return self._append(
            {
                "event": event.value,
                "at": at.isoformat(),
                "strategy_id": str(strategy_id),
                "filled_quantity": str(filled_quantity),
                "average_price": str(average_price) if average_price is not None else None,
            }
        )

    def record_uncertain(
        self,
        strategy_id: UUID,
        *,
        at: dt.datetime,
        reason: str,
        closing: bool = False,
        order_id: int | None = None,
        perm_id: int | None = None,
    ) -> dict[str, Any]:
        """We transmitted and do not know what happened.

        Deliberately not ``*_FAILED``. A failure means nothing is in the market;
        this means we cannot say. Recording it as a failure would let the runner
        open a second position on top of an order that may be resting in the
        book, which is the specific duplicate this whole state exists to prevent.
        """
        event = PositionEvent.CLOSE_UNCERTAIN if closing else PositionEvent.OPEN_UNCERTAIN
        return self._append(
            {
                "event": event.value,
                "at": at.isoformat(),
                "strategy_id": str(strategy_id),
                "reason": reason,
                "order_id": order_id,
                "perm_id": perm_id,
            }
        )

    def record_open_filled(
        self,
        strategy_id: UUID,
        *,
        at: dt.datetime,
        filled_credit: Decimal,
        filled_quantity: Decimal | None = None,
        commission: Decimal | None = None,
        order_id: int | None = None,
        perm_id: int | None = None,
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": PositionEvent.OPEN_FILLED.value,
                "at": at.isoformat(),
                "strategy_id": str(strategy_id),
                "filled_credit": str(filled_credit),
                "filled_quantity": (
                    str(filled_quantity) if filled_quantity is not None else None
                ),
                "commission": str(commission) if commission is not None else None,
                "order_id": order_id,
                "perm_id": perm_id,
            }
        )

    def record_executions(
        self,
        strategy_id: UUID,
        *,
        at: dt.datetime,
        executions: Sequence[dict[str, Any]],
        total_commission: Decimal | None,
        complete: bool,
        gaps: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Persist what the broker says actually filled, and what it charged.

        Written whenever executions are read back, complete or not. Recording an
        *incomplete* capture is the point of doing it at all: it is the durable
        difference between "this fill cost nothing" and "nobody ever asked what
        this fill cost", and the second is what the store said for a real
        position because the commission callback was never read.

        ``complete`` is refused unless a total accompanies it. The two are a pair
        -- see :class:`engine.options.executions.CommissionEvidence`, which
        couples them at construction -- and a claim of completeness with no
        number is exactly the shape that would let net profit be stated against
        nothing.
        """
        if complete and total_commission is None:
            _refuse(
                "a complete commission capture must record the total it proved",
                hint="incomplete evidence records what is missing instead; a "
                "partial sum understates the cost and nothing about it looks wrong",
            )
        return self._append(
            {
                "event": PositionEvent.EXECUTIONS_RECORDED.value,
                "at": at.isoformat(),
                "strategy_id": str(strategy_id),
                "executions": list(executions),
                "total_commission": (
                    str(total_commission) if total_commission is not None else None
                ),
                "commission_complete": bool(complete),
                "commission_gaps": list(gaps),
            }
        )

    def record_open_failed(
        self, strategy_id: UUID, *, at: dt.datetime, reason: str
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": PositionEvent.OPEN_FAILED.value,
                "at": at.isoformat(),
                "strategy_id": str(strategy_id),
                "reason": reason,
            }
        )

    def record_close_submitted(
        self, strategy_id: UUID, *, at: dt.datetime, target_debit: Decimal | None = None
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": PositionEvent.CLOSE_SUBMITTED.value,
                "at": at.isoformat(),
                "strategy_id": str(strategy_id),
                "target_debit": str(target_debit) if target_debit is not None else None,
            }
        )

    def record_close_filled(
        self, strategy_id: UUID, *, at: dt.datetime, closing_debit: Decimal
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": PositionEvent.CLOSE_FILLED.value,
                "at": at.isoformat(),
                "strategy_id": str(strategy_id),
                "closing_debit": str(closing_debit),
            }
        )

    def record_close_failed(
        self, strategy_id: UUID, *, at: dt.datetime, reason: str
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": PositionEvent.CLOSE_FAILED.value,
                "at": at.isoformat(),
                "strategy_id": str(strategy_id),
                "reason": reason,
            }
        )

    def record_rolled(
        self, strategy_id: UUID, *, into: UUID, at: dt.datetime
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": PositionEvent.ROLLED.value,
                "at": at.isoformat(),
                "strategy_id": str(strategy_id),
                "rolled_to": str(into),
            }
        )

    # -- reading ----------------------------------------------------------

    def events(self) -> Iterator[dict[str, Any]]:
        """Every well-formed event, skipping a torn final line."""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise JournalError(f"cannot read the position store: {exc}") from exc

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                yield parsed

    def positions(self, *, errors: list[str] | None = None) -> dict[UUID, OpenPosition]:
        """Rebuild the book by replaying the log, oldest event first.

        **A corrupt line degrades the replay; it does not brick it.** Every
        branch is guarded, not just the opening one. An earlier version wrapped
        only ``OPEN_SUBMITTED``, so a single ``CLOSE_FILLED`` carrying a naive
        timestamp raised out of this method and made the whole book unreadable --
        and an engine that cannot read its book cannot manage the positions it
        already holds, which is a worse failure than any single lost line.

        Skipping is not free either: a dropped ``CLOSE_FILLED`` leaves a position
        looking open when it is closed. So the skip is *recorded*. Callers get
        the problems through ``errors`` or :meth:`integrity_errors`, and
        :meth:`reconcile_against_broker` refuses to agree while any exist -- which
        is what stops the runner opening new risk against a book it only partly
        understands. Degraded, loud, and still able to exit.
        """
        problems: list[str] = [] if errors is None else errors
        book: dict[UUID, OpenPosition] = {}
        for event in self.events():
            kind = event.get("event")
            raw_id = event.get("strategy_id")
            if not isinstance(raw_id, str):
                continue
            try:
                strategy_id = UUID(raw_id)
            except (ValueError, AttributeError):
                continue

            if kind == PositionEvent.OPEN_SUBMITTED.value:
                try:
                    book[strategy_id] = OpenPosition.from_record(event)
                except (
                    KeyError,
                    ValueError,
                    TypeError,
                    InvalidOperation,
                    InvalidPortfolioStateError,
                ) as exc:
                    problems.append(
                        f"{strategy_id}: unreadable OPEN_SUBMITTED "
                        f"({type(exc).__name__}: {exc})"
                    )
                    continue
                continue

            current = book.get(strategy_id)
            if current is None:
                continue

            try:
                updated = self._apply(current, kind, event)
            except (
                KeyError,
                ValueError,
                TypeError,
                InvalidOperation,
                InvalidPortfolioStateError,
            ) as exc:
                problems.append(
                    f"{strategy_id}: unreadable {kind} ({type(exc).__name__}: {exc})"
                )
                continue
            if updated is None:
                book.pop(strategy_id, None)
            else:
                book[strategy_id] = updated
        return book

    @staticmethod
    def _apply(
        current: OpenPosition, kind: Any, event: dict[str, Any]
    ) -> OpenPosition | None:
        """Apply one transition, or ``None`` to drop the position.

        Raises on a malformed event rather than swallowing. The caller records
        the failure and moves on, so one bad line costs one transition instead
        of the whole book.
        """
        if kind in (
            PositionEvent.OPEN_ACKNOWLEDGED.value,
            PositionEvent.CLOSE_ACKNOWLEDGED.value,
        ):
            closing = kind == PositionEvent.CLOSE_ACKNOWLEDGED.value
            ids = {
                ("close_order_id" if closing else "open_order_id"): _int_or_none(
                    event.get("order_id")
                )
                or (current.close_order_id if closing else current.open_order_id),
                ("close_perm_id" if closing else "open_perm_id"): _int_or_none(
                    event.get("perm_id")
                )
                or (current.close_perm_id if closing else current.open_perm_id),
            }
            # Acknowledgement never moves a position backwards. A duplicate or
            # late-arriving ack on an already-filled order must record the
            # identifiers and leave the state alone -- IBKR re-sends status
            # callbacks freely, and out-of-order delivery is ordinary.
            return _replace(current, **ids)

        if kind == PositionEvent.OPEN_PARTIAL.value:
            quantity = _decimal_or_none(event.get("filled_quantity")) or Decimal("0")
            price = _decimal_or_none(event.get("average_price"))
            # Monotonic: a duplicate or out-of-order partial must never reduce
            # the recorded fill. IBKR reports cumulative fills, and taking the
            # smaller of two callbacks would under-size the exit.
            quantity = max(quantity, current.filled_quantity)
            return _replace(
                current,
                state=PositionState.OPEN,
                filled_quantity=quantity,
                filled_credit=abs(price) if price is not None else current.filled_credit,
            )

        if kind == PositionEvent.CLOSE_PARTIAL.value:
            # The quantity used to be dropped here. ``record_partial_fill``
            # wrote it to the journal and this branch replayed only the state,
            # so a cancelled-after-partial exit reloaded as "closing, amount
            # unknown" -- the contracts that got out and the ones still held
            # were indistinguishable on disk. Monotonic for the same reason the
            # opening side is: IBKR reports cumulative fills and re-sends
            # callbacks freely, so the smaller of two deliveries is never the
            # newer fact.
            quantity = _decimal_or_none(event.get("filled_quantity")) or Decimal("0")
            return _replace(
                current,
                state=PositionState.CLOSING,
                close_filled_quantity=max(quantity, current.close_filled_quantity),
            )

        if kind in (
            PositionEvent.OPEN_UNCERTAIN.value,
            PositionEvent.CLOSE_UNCERTAIN.value,
        ):
            closing = kind == PositionEvent.CLOSE_UNCERTAIN.value
            reason = str(event.get("reason") or "").strip()
            if not reason:
                raise ValueError("an uncertainty event must record a reason")
            ids: dict[str, Any] = {}
            order_id = _int_or_none(event.get("order_id"))
            perm_id = _int_or_none(event.get("perm_id"))
            if order_id is not None:
                ids["close_order_id" if closing else "open_order_id"] = order_id
            if perm_id is not None:
                ids["close_perm_id" if closing else "open_perm_id"] = perm_id
            return _replace(
                current, state=PositionState.UNCERTAIN, uncertainty=reason, **ids
            )

        if kind == PositionEvent.OPEN_FILLED.value:
            credit = _decimal_or_none(event.get("filled_credit"))
            quantity = _decimal_or_none(event.get("filled_quantity"))
            order_id = _int_or_none(event.get("order_id")) or current.open_order_id
            perm_id = _int_or_none(event.get("perm_id")) or current.open_perm_id
            return _replace(
                current,
                state=PositionState.OPEN,
                filled_credit=credit if credit is not None else current.filled_credit,
                filled_quantity=(
                    max(quantity, current.filled_quantity)
                    if quantity is not None
                    else Decimal(current.intent.quantity)
                ),
                commission=_decimal_or_none(event.get("commission")) or current.commission,
                open_order_id=order_id,
                open_perm_id=perm_id,
                # A fill resolves any earlier uncertainty.
                uncertainty=None,
            )

        if kind == PositionEvent.OPEN_FAILED.value:
            # An open that never filled is not a position -- but only if nothing
            # filled. A failure recorded after a partial would otherwise erase a
            # real position from the book.
            if current.filled_quantity > Decimal("0"):
                return _replace(current, state=PositionState.OPEN, uncertainty=None)
            return None

        if kind == PositionEvent.EXECUTIONS_RECORDED.value:
            total = _decimal_or_none(event.get("total_commission"))
            claimed = bool(event.get("commission_complete"))
            # Completeness is monotonic: evidence is not un-learned. A later
            # capture that could not reach the broker must not demote a cost that
            # was already proven, or a transient query failure would make a net
            # figure disappear and reappear between passes.
            complete = current.commission_complete or (claimed and total is not None)
            return _replace(
                current,
                commission=total if total is not None else current.commission,
                commission_complete=complete,
            )

        if kind == PositionEvent.CLOSE_SUBMITTED.value:
            return _replace(current, state=PositionState.CLOSING)

        if kind == PositionEvent.CLOSE_FILLED.value:
            closed_at = _datetime_or_none(event.get("at"))
            if closed_at is None:
                # OpenPosition refuses CLOSED without a timestamp, so this would
                # raise anyway -- named here so the recorded problem says what
                # was wrong rather than reporting a generic invariant failure.
                raise ValueError("CLOSE_FILLED has no usable timezone-aware 'at'")
            return _replace(
                current,
                state=PositionState.CLOSED,
                closed_at=closed_at,
                closing_debit=_decimal_or_none(event.get("closing_debit")),
                # A completed close retires everything that was held, so the
                # closing count catches up to the opening one. Without this a
                # CLOSED position that partially filled first would still report
                # a positive ``remaining_quantity`` -- contracts nobody holds.
                close_filled_quantity=max(
                    current.close_filled_quantity, current.filled_quantity
                ),
            )

        if kind == PositionEvent.CLOSE_FAILED.value:
            # Back to OPEN: the close did not happen, so the position is still
            # live and must stay eligible for management.
            return _replace(current, state=PositionState.OPEN)

        if kind == PositionEvent.ROLLED.value:
            rolled_to = event.get("rolled_to")
            if not isinstance(rolled_to, str):
                raise ValueError("ROLLED does not name the strategy it rolled into")
            return _replace(
                current,
                state=PositionState.ROLLED,
                closed_at=_datetime_or_none(event.get("at")),
                rolled_to=UUID(rolled_to),
            )

        # An event kind this version does not know. Left alone rather than
        # guessed at: a newer writer's vocabulary is not corruption.
        return current

    def integrity_errors(self) -> tuple[str, ...]:
        """Every event the replay could not apply.

        Non-empty means the book on disk is only partly understood. The
        reconciler refuses to agree while that is true, which is what stops new
        risk being opened against it.
        """
        problems: list[str] = []
        self.positions(errors=problems)
        return tuple(problems)

    def open_positions(self) -> list[OpenPosition]:
        """Everything the market can still move against, oldest first."""
        return sorted(
            (p for p in self.positions().values() if p.is_live),
            key=lambda p: p.opened_at,
        )

    def exposures(self) -> tuple[PositionExposure, ...]:
        """What the governor aggregates. This is what closes gap G1."""
        return tuple(p.exposure() for p in self.open_positions())

    def get(self, strategy_id: UUID) -> OpenPosition | None:
        return self.positions().get(strategy_id)

    # -- reconciliation ---------------------------------------------------

    def reconcile_against_broker(
        self,
        broker_positions: Any,
        *,
        checked_at: dt.datetime,
        broker_orders: Any = None,
    ) -> ReconciliationReport:
        """Compare the replayed book against what the broker reports holding.

        ``broker_positions`` is the ``[(symbol, quantity, average_cost)]`` shape
        :meth:`engine.broker.Broker.positions` returns. That granularity cannot
        prove a *specific* spread is present -- it reports contracts, not
        structures -- so this deliberately checks only what it can: that every
        underlying the store believes it holds appears at the broker, and that
        no state is stranded mid-transition. It reports; it never repairs.

        ``broker_orders`` distinguishes three cases, and the distinction is the
        whole reason this argument is not a plain sequence:

        ``None``   the caller did not enumerate open orders. Nothing may be
                   concluded about working orders, so every in-flight position
                   is reported as *unverified*.
        ``()``     the caller asked and the broker is working nothing. An
                   in-flight position is genuinely *absent*.
        entries    matched, per :meth:`_reconcile_orders`.

        The default used to be ``()``, which made "I never asked" indistinguish-
        able from "I asked and there is nothing" -- and the report then said, of
        an order the broker was demonstrably working, that the broker was not
        working it. That claim was false and it was the reconciler's own
        default that produced it.
        """
        problems: list[str] = []
        book = self.positions(errors=problems)
        live = sorted(
            (p for p in book.values() if p.is_live), key=lambda p: p.opened_at
        )
        broker_symbols = {
            str(row[0]).strip().upper() for row in (broker_positions or []) if row
        }
        store_symbols = {p.underlying for p in live}

        return ReconciliationReport(
            checked_at=checked_at,
            known_open=tuple(p.strategy_id for p in live),
            stranded_opening=tuple(
                p.strategy_id for p in live if p.state is PositionState.OPENING
            ),
            stranded_closing=tuple(
                p.strategy_id for p in live if p.state is PositionState.CLOSING
            ),
            missing_at_broker=tuple(
                p.strategy_id for p in live if p.underlying not in broker_symbols
            ),
            unknown_at_broker=tuple(sorted(broker_symbols - store_symbols)),
            replay_errors=tuple(problems),
            **self._reconcile_orders(broker_orders, live, book),
        )

    def _reconcile_orders(
        self,
        broker_orders: Any,
        live: list[OpenPosition],
        book: dict[UUID, OpenPosition] | None = None,
    ) -> dict[str, tuple[Any, ...]]:
        """Match the store's transmitted orders against the broker's open ones.

        Matching is on ``permId`` first and ``orderId`` second. ``permId`` is
        IBKR's durable identifier and is the only one that survives a restart --
        ``orderId`` is client-assigned and unique only within a session, so a
        reconciler that trusted it after a reconnect would match this session's
        order 3 against the previous session's unrelated order 3.

        Accepts anything with ``orderId``/``permId`` attributes or a 2-tuple, so
        the caller can pass ``ib.openOrders()``, ``ib.trades()``, or a test
        fixture without an adapter in between.
        """
        # An order is matched ONCE, by the strongest identity available:
        #
        #     permId  ->  orderRef (our strategy id)  ->  orderId
        #
        # ``permId`` is IBKR's durable identifier. ``orderRef`` is our own
        # correlation id -- ``build_combo`` sets it to ``str(strategy_id)`` -- and
        # is durable because we chose it, which makes it usable even before the
        # broker has assigned a permId. ``orderId`` is client-assigned and unique
        # only within a session, so it is evidence of last resort: after a
        # reconnect it says nothing, and trusting it would pair this session's
        # order 3 with the previous session's unrelated order 3.
        #
        # Matching once is the point. Differencing the id spaces independently
        # reported an order as unknown by its reassigned orderId even after its
        # permId had matched, so any reconnect produced a disagreement that never
        # cleared and blocked new entries permanently.
        asked = broker_orders is not None
        broker_entries: list[tuple[int | None, int | None, str | None]] = []
        broker_perm: set[int] = set()
        broker_order: set[int] = set()
        for entry in broker_orders or ():
            # An ``ib_async`` Trade carries the identifiers on ``.order``, not on
            # itself, so ``ib.openTrades()`` would otherwise contribute nothing
            # but blanks -- and a blank entry is indistinguishable from an
            # account with no working orders. Unwrapped here rather than in the
            # caller so every caller gets it.
            inner = getattr(entry, "order", None)
            if inner is not None and getattr(entry, "orderId", None) is None:
                entry = inner
            order_id = getattr(entry, "orderId", None)
            perm_id = getattr(entry, "permId", None)
            order_ref = getattr(entry, "orderRef", None)
            if order_id is None and perm_id is None and isinstance(entry, (tuple, list)):
                order_id = entry[0] if len(entry) > 0 else None
                perm_id = entry[1] if len(entry) > 1 else None
                order_ref = entry[2] if len(entry) > 2 else None
            order_id = order_id if _is_identifier(order_id) else None
            perm_id = perm_id if _is_identifier(perm_id) else None
            order_ref = str(order_ref).strip() if order_ref else None
            if order_id is None and perm_id is None and not order_ref:
                continue
            broker_entries.append((order_id, perm_id, order_ref))
            if order_id is not None:
                broker_order.add(order_id)
            if perm_id is not None:
                broker_perm.add(perm_id)

        store_perm = {p.open_perm_id for p in live if p.open_perm_id} | {
            p.close_perm_id for p in live if p.close_perm_id
        }
        store_order = {p.open_order_id for p in live if p.open_order_id} | {
            p.close_order_id for p in live if p.close_order_id
        }

        broker_refs = {ref for _o, _p, ref in broker_entries if ref}
        store_refs = {str(p.strategy_id) for p in live}

        # Ownership, which is a *different* question from matching, and keeping
        # them apart is what stops an unrelated resting order on the same paper
        # account from becoming a permanent DISAGREEMENT that blocks every
        # entry forever.
        #
        # An order is this engine's only on evidence that cannot be coincidence:
        #
        #   * its ``orderRef`` is a strategy id this store has recorded --
        #     ``build_combo`` sets ``orderRef = str(strategy_id)`` (see
        #     ``execution.build_combo``), and a v4 UUID is not something another
        #     trading tool stamps on its orders by accident; or
        #   * its ``permId`` is one this store persisted for an order it sent.
        #
        # ``orderId`` is deliberately NOT ownership evidence. It is client-
        # assigned and reused across sessions, so an unrelated order can carry
        # one of ours; that is the C22 defect pointed the other way, and using
        # it here would claim a stranger's order as our own. It stays what it
        # was: last-resort evidence for matching an order already known to be
        # ours.
        #
        # Everything that fails both tests is another tool's order, or a manual
        # ticket the operator entered in TWS. Those are ignored -- not reported,
        # not counted as a disagreement. This engine does not own the account.
        every_position = list((book or {}).values()) or list(live)
        owned_refs = {str(p.strategy_id) for p in every_position}
        owned_perm = {p.open_perm_id for p in every_position if p.open_perm_id} | {
            p.close_perm_id for p in every_position if p.close_perm_id
        }

        def is_ours(perm_id: int | None, order_ref: str | None) -> bool:
            if order_ref and order_ref in owned_refs:
                return True
            return perm_id is not None and perm_id in owned_perm

        def known_to_broker(position: OpenPosition) -> bool:
            """Matched by the strongest identity available. See above."""
            perms = {position.open_perm_id, position.close_perm_id} - {None}
            if perms & broker_perm:
                return True
            if str(position.strategy_id) in broker_refs:
                return True
            orders = {position.open_order_id, position.close_order_id} - {None}
            return bool(orders & broker_order)

        def matched_to_store(
            order_id: int | None, perm_id: int | None, order_ref: str | None
        ) -> bool:
            if perm_id is not None and perm_id in store_perm:
                return True
            if order_ref and order_ref in store_refs:
                return True
            return order_id is not None and order_id in store_order

        # A position mid-transition whose order the broker is not working is the
        # dangerous shape: it either filled while we were not looking, or it was
        # never accepted, and those demand opposite responses. Reported, never
        # guessed at.
        in_flight = [
            p
            for p in live
            if p.state in (PositionState.OPENING, PositionState.CLOSING, PositionState.UNCERTAIN)
            and (p.open_order_id or p.open_perm_id or p.close_order_id or p.close_perm_id)
        ]
        # Three outcomes, not two. "Working" and "absent" are opposite facts and
        # both are conclusions; "unverified" is the absence of a conclusion, and
        # collapsing it into "absent" is how the report came to state, of a live
        # working order, that the broker was not working it.
        working = tuple(p.strategy_id for p in in_flight if asked and known_to_broker(p))
        absent = tuple(
            p.strategy_id for p in in_flight if asked and not known_to_broker(p)
        )
        unverified = () if asked else tuple(p.strategy_id for p in in_flight)

        return {
            "orders_working_at_broker": working,
            "orders_absent_at_broker": absent,
            "orders_unverified_at_broker": unverified,
            # Ours, and the live book cannot account for it. Scoped by
            # ``is_ours`` first: a foreign order is not a disagreement, it is
            # none of our business. Reported by whichever id it carries,
            # preferring the durable one so the operator can look it up in TWS
            # after a reconnect.
            "orders_unknown_to_store": tuple(
                sorted(
                    perm_id if perm_id is not None else order_id  # type: ignore[misc]
                    for order_id, perm_id, order_ref in broker_entries
                    if is_ours(perm_id, order_ref)
                    and not matched_to_store(order_id, perm_id, order_ref)
                    and (perm_id is not None or order_id is not None)
                )
            ),
        }


def _replace(position: OpenPosition, **changes: Any) -> OpenPosition:
    """A frozen-dataclass update that re-runs every invariant.

    Reconstructing through the constructor rather than mutating is deliberate:
    a replayed state transition is exactly where a corrupt log would otherwise
    smuggle in an impossible position, and ``OpenPosition.__post_init__`` is the
    thing that stops it.

    **The carried fields are enumerated from the dataclass, never by hand.** They
    used to be a written-out literal, and it silently went stale the moment
    ``close_filled_quantity`` was added for C24: the field was absent from the
    literal, so every transition that did not pass it explicitly reset it to its
    default of zero. A ``CLOSE_PARTIAL`` followed by a ``CLOSE_FAILED`` -- an exit
    cancelled after filling part of the way, which is the exact shape C24 was
    written to make representable -- replayed as a position holding everything it
    had ever bought. ``remaining_quantity`` then reported contracts that were
    already out of the market, and an exit sized off it would sell them a second
    time. That is C21's failure reached by a different road, so the fix is the one
    that cannot go stale again rather than one more name in a list.
    """
    fields: dict[str, Any] = {
        field.name: getattr(position, field.name)
        for field in dataclass_fields(position)
    }
    fields.update(changes)
    return OpenPosition(**fields)


def _is_identifier(value: Any) -> bool:
    """A usable broker identifier: a non-zero int that is not a bool.

    Zero is excluded because IBKR uses it for an unassigned ``permId``, and
    treating it as real would make every unacknowledged order share one id.
    """
    return isinstance(value, int) and not isinstance(value, bool) and bool(value)


def _int_or_none(value: Any) -> int | None:
    """A broker identifier, or ``None``.

    Zero is rejected: IBKR uses ``0`` for an unassigned ``permId``, and storing
    it would make every unacknowledged order look like it shared one identifier.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value or None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _datetime_or_none(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
