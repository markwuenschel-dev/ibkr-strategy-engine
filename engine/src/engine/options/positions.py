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
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
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
    commission: Decimal | None = None
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
            "commission": str(self.commission) if self.commission is not None else None,
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
            commission=_decimal_or_none(record.get("commission")),
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
    #: Store positions mid-transition whose order the broker is not working.
    #: Either it filled unobserved or it was never accepted -- opposite fixes.
    orders_absent_at_broker: tuple[UUID, ...] = ()
    #: Broker orders this engine has no record of transmitting.
    orders_unknown_to_store: tuple[int, ...] = ()

    @property
    def agrees(self) -> bool:
        """True only when the book is both fully readable and matched.

        ``replay_errors`` counts. A book that could not be fully replayed is not
        a book the engine understands, and "the parts I could read match" is not
        agreement.
        """
        return not (
            self.stranded_opening
            or self.stranded_closing
            or self.missing_at_broker
            or self.unknown_at_broker
            or self.replay_errors
            or self.orders_absent_at_broker
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
        if self.orders_absent_at_broker:
            lines.append(
                f"    ORDERS ABSENT     {[str(i) for i in self.orders_absent_at_broker]} "
                "-- transmitted, and the broker is not working them; either they "
                "filled unobserved or were never accepted"
            )
        if self.orders_unknown_to_store:
            lines.append(
                f"    ORDERS UNKNOWN    {list(self.orders_unknown_to_store)} "
                "-- live at the broker with no record of this engine sending them"
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
            "orders_absent_at_broker": [str(i) for i in self.orders_absent_at_broker],
            "orders_unknown_to_store": list(self.orders_unknown_to_store),
        }


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
            return _replace(current, state=PositionState.CLOSING)

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
        broker_orders: Any = (),
    ) -> ReconciliationReport:
        """Compare the replayed book against what the broker reports holding.

        ``broker_positions`` is the ``[(symbol, quantity, average_cost)]`` shape
        :meth:`engine.broker.Broker.positions` returns. That granularity cannot
        prove a *specific* spread is present -- it reports contracts, not
        structures -- so this deliberately checks only what it can: that every
        underlying the store believes it holds appears at the broker, and that
        no state is stranded mid-transition. It reports; it never repairs.
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
            **self._reconcile_orders(broker_orders, live),
        )

    def _reconcile_orders(
        self, broker_orders: Any, live: list[OpenPosition]
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
        broker_entries: list[tuple[int | None, int | None, str | None]] = []
        broker_perm: set[int] = set()
        broker_order: set[int] = set()
        for entry in broker_orders or ():
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
        return {
            "orders_absent_at_broker": tuple(
                p.strategy_id for p in in_flight if not known_to_broker(p)
            ),
            # A broker order is unknown only if NEITHER identifier matches the
            # store. Reported by whichever id it does carry, preferring the
            # durable one so the operator can look it up after a reconnect.
            "orders_unknown_to_store": tuple(
                sorted(
                    perm_id if perm_id is not None else order_id  # type: ignore[misc]
                    for order_id, perm_id, order_ref in broker_entries
                    if not matched_to_store(order_id, perm_id, order_ref)
                    and (perm_id is not None or order_id is not None)
                )
            ),
        }


def _replace(position: OpenPosition, **changes: Any) -> OpenPosition:
    """A frozen-dataclass update that re-runs every invariant.

    ``dataclasses.replace`` would do this too; it is spelled out so the
    re-validation is visible -- a replayed state transition is exactly where a
    corrupt log would otherwise smuggle in an impossible position.
    """
    fields: dict[str, Any] = {
        "strategy_id": position.strategy_id,
        "intent": position.intent,
        "opened_at": position.opened_at,
        "state": position.state,
        "buying_power_reserved": position.buying_power_reserved,
        "filled_credit": position.filled_credit,
        "closed_at": position.closed_at,
        "closing_debit": position.closing_debit,
        "rolled_to": position.rolled_to,
        "open_order_id": position.open_order_id,
        "open_perm_id": position.open_perm_id,
        "close_order_id": position.close_order_id,
        "close_perm_id": position.close_perm_id,
        "filled_quantity": position.filled_quantity,
        "commission": position.commission,
        "uncertainty": position.uncertainty,
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
