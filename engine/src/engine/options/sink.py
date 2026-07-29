"""Where broker lifecycle observations go, as they happen.

Until this module existed, ``place_combo`` polled the broker, then persisted a
**single** snapshot after the loop finished. Final exposure was right, but the
event log recorded the destination rather than the journey: the sequence

    SUBMITTED -> ACKNOWLEDGED -> PARTIALLY_FILLED -> CANCELLED

collapsed to whatever was true at the end, and a crash mid-poll lost the most
recent fill transition entirely -- contracts in the market, with the store still
saying the order was merely submitted.

**The sink records; it cannot act.** :class:`OrderLifecycleSink` has exactly one
method, and that method takes an observation and returns nothing. There is no
authorize, no place, no cancel, no retry, no order construction. This is not a
convention -- it is the whole reason the seam is safe to call from inside the
polling loop, where the authorization token is no longer in scope. A sink that
could transmit would be a second door, reachable from a context that has already
passed the first one, which is precisely the shape the chokepoint exists to
prevent. ``test_options_transmit.py`` asserts the protocol has no such method.

**Ingestion is idempotent and monotonic.** IBKR re-sends status callbacks freely,
delivers fills out of order with respect to status, and reconnects mid-flight.
So an observation is appended only when it is *materially new*, a filled quantity
never decreases, and a stale callback cannot walk a position backwards from a
state it has already passed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from .orderstate import BrokerOrderSnapshot, OrderLifecycleState
from .positions import PositionStore

__all__ = [
    "OrderLifecycleSink",
    "LifecycleRecorder",
    "NullLifecycleSink",
    "PROGRESSION",
]

ZERO = Decimal("0")

#: How far through its life an order has got. Used only to reject *regressions*
#: from stale callbacks -- a later-arriving ``ACKNOWLEDGED`` must not undo a
#: ``PARTIALLY_FILLED`` that already reached the store.
#:
#: The uncertain states sit outside this ordering entirely: a timeout or a
#: disconnect can legitimately follow any state, and is never a regression
#: because it is not a claim about the order at all.
PROGRESSION: dict[OrderLifecycleState, int] = {
    OrderLifecycleState.SUBMITTED: 1,
    OrderLifecycleState.ACKNOWLEDGED: 2,
    OrderLifecycleState.PARTIALLY_FILLED: 3,
    OrderLifecycleState.FILLED: 4,
    OrderLifecycleState.CANCELLED: 4,
    OrderLifecycleState.REJECTED: 4,
    OrderLifecycleState.INACTIVE: 4,
}


@runtime_checkable
class OrderLifecycleSink(Protocol):
    """Receives broker lifecycle observations. **Records only.**

    Deliberately one method. Every capability that could change the world --
    authorizing, constructing an order, transmitting, cancelling, retrying --
    is absent by design, so handing a sink to code running inside the polling
    loop grants it the ability to write history and nothing else.
    """

    def observe(
        self,
        strategy_id: UUID,
        observation: BrokerOrderSnapshot,
        *,
        closing: bool = False,
    ) -> bool:
        """Record one observation. Returns whether it was materially new.

        The return value exists for reporting and tests, not for control flow:
        a caller must not need it to stay correct.
        """
        ...


@dataclass
class _Known:
    """What the recorder has already written for one order."""

    state: OrderLifecycleState | None = None
    filled: Decimal = ZERO
    order_id: int | None = None
    perm_id: int | None = None
    commission: Decimal | None = None
    priced: bool = False


@dataclass
class NullLifecycleSink:
    """Discards everything. For call sites that genuinely persist elsewhere.

    Named rather than defaulting ``sink=None`` at the transmit boundary so that
    "nothing is being recorded" is a visible decision at the call site instead of
    an omission -- the same reason the exit path no longer has a default quantity.
    """

    observed: list[tuple[UUID, BrokerOrderSnapshot, bool]] = field(default_factory=list)

    def observe(
        self,
        strategy_id: UUID,
        observation: BrokerOrderSnapshot,
        *,
        closing: bool = False,
    ) -> bool:
        self.observed.append((strategy_id, observation, closing))
        return True


class LifecycleRecorder:
    """Writes observations into a :class:`PositionStore`, once each.

    Holds a small in-memory picture of what it has already written per order so
    materiality can be decided without replaying the log on every poll. That
    cache is an optimisation and never the source of truth: it is seeded from
    the store, so a recorder constructed after a restart already knows what the
    previous process persisted and will not re-append transitions it can see.
    """

    def __init__(self, store: PositionStore) -> None:
        self.store = store
        # Keyed on (strategy_id, closing). The opening and closing orders are
        # two different orders against one position, and they have independent
        # fill counts -- sharing a key made a close look already-filled because
        # the open had filled the same quantity.
        self._known: dict[tuple[UUID, bool], _Known] = {}
        self._seed()

    def _seed(self) -> None:
        """Learn what is already on disk, so a restart does not duplicate."""
        try:
            book = self.store.positions()
        except Exception:  # noqa: BLE001 - a broken log is the store's problem
            return
        for strategy_id, position in book.items():
            self._known[(strategy_id, False)] = _Known(
                state=None,
                filled=position.filled_quantity,
                order_id=position.open_order_id,
                perm_id=position.open_perm_id,
                commission=position.commission,
                priced=position.filled_quantity > ZERO,
            )

    # -- the sink ---------------------------------------------------------

    def observe(
        self,
        strategy_id: UUID,
        observation: BrokerOrderSnapshot,
        *,
        closing: bool = False,
    ) -> bool:
        """Persist this observation if it changes exposure or identity.

        Returns ``True`` when something was written. Everything below is
        deliberately ordered so that the *durable* facts -- identity first, then
        exposure -- are recorded before the merely-descriptive state change: a
        crash between two writes should lose the least important one.
        """
        known = self._known.setdefault((strategy_id, closing), _Known())

        if self._is_stale(known, observation):
            return False

        wrote = False

        # 1. Identity, first and unconditionally. Broker ids are the only thing
        #    that lets a restart find this order again, and they are cheap to
        #    write. A disconnect immediately afterwards still leaves us able to
        #    reconcile.
        if self._new_identity(known, observation):
            self.store.record_acknowledged(
                strategy_id,
                at=observation.observed_at,
                closing=closing,
                order_id=observation.order_id,
                perm_id=observation.perm_id,
            )
            known.order_id = observation.order_id or known.order_id
            known.perm_id = observation.perm_id or known.perm_id
            wrote = True

        # 2. Exposure. A confirmed fill reaches disk before anything waits for a
        #    terminal status -- this is the transition a crash used to lose.
        if observation.filled > known.filled:
            wrote = self._record_fill(strategy_id, observation, known, closing) or wrote

        # 3. Uncertainty. Not a claim about the order, so it is recorded even
        #    when nothing else changed: an operator needs to see that the engine
        #    stopped knowing, and it is what blocks the next entry.
        if observation.is_uncertain and known.state is not observation.state:
            self.store.record_uncertain(
                strategy_id,
                at=observation.observed_at,
                reason=(
                    observation.message
                    or f"{observation.state.value} while awaiting the order outcome"
                ),
                closing=closing,
                order_id=observation.order_id,
                perm_id=observation.perm_id,
            )
            wrote = True

        # 4. A terminal refusal with nothing filled. Recorded last because it is
        #    the only branch that can remove a position, and it must never run
        #    ahead of a fill that arrived in the same observation.
        elif (
            observation.is_terminal
            and not observation.has_position
            and known.filled == ZERO
            and observation.state
            in (
                OrderLifecycleState.REJECTED,
                OrderLifecycleState.INACTIVE,
                OrderLifecycleState.CANCELLED,
            )
            and known.state is not observation.state
        ):
            method = (
                self.store.record_close_failed if closing else self.store.record_open_failed
            )
            method(
                strategy_id,
                at=observation.observed_at,
                reason=observation.message or observation.state.value,
            )
            wrote = True

        known.state = observation.state
        return wrote

    # -- helpers ----------------------------------------------------------

    def _is_stale(self, known: _Known, observation: BrokerOrderSnapshot) -> bool:
        """Whether this observation walks the order backwards.

        A duplicate of the current state is not stale -- it simply writes
        nothing, because none of the materiality tests fire. What is rejected is
        a *lower* progression rank carrying no new information, which is what a
        re-delivered early callback looks like after a fill has landed.
        """
        if known.state is None:
            return False
        if observation.is_uncertain:
            return False
        current = PROGRESSION.get(known.state)
        incoming = PROGRESSION.get(observation.state)
        if current is None or incoming is None:
            return False
        if incoming < current and observation.filled <= known.filled:
            return True
        return False

    def _new_identity(self, known: _Known, observation: BrokerOrderSnapshot) -> bool:
        if observation.order_id is not None and observation.order_id != known.order_id:
            return True
        return observation.perm_id is not None and observation.perm_id != known.perm_id

    def _record_fill(
        self,
        strategy_id: UUID,
        observation: BrokerOrderSnapshot,
        known: _Known,
        closing: bool,
    ) -> bool:
        """Persist a fill, complete or partial.

        A complete fill is only recorded as complete when there is a usable
        price. Without one the quantity is still durable -- the contracts are
        real -- but the position is left carrying an unknown entry credit rather
        than a fabricated one, which is what disables the profit target.
        """
        price = observation.average_price
        complete = observation.state is OrderLifecycleState.FILLED

        if complete and price is not None:
            if closing:
                self.store.record_close_filled(
                    strategy_id, at=observation.observed_at, closing_debit=abs(price)
                )
            else:
                self.store.record_open_filled(
                    strategy_id,
                    at=observation.observed_at,
                    filled_credit=abs(price),
                    filled_quantity=observation.filled,
                    commission=observation.commission,
                    order_id=observation.order_id,
                    perm_id=observation.perm_id,
                )
            known.filled = observation.filled
            known.priced = True
            known.commission = observation.commission or known.commission
            return True

        self.store.record_partial_fill(
            strategy_id,
            at=observation.observed_at,
            filled_quantity=observation.filled,
            average_price=price,
            closing=closing,
        )
        known.filled = observation.filled
        known.priced = known.priced or price is not None
        return True


def observations_recorded(sink: Any) -> int:
    """How many observations a sink has written, when it can say.

    Used by the verification command for its timeline count. Returns ``0`` for a
    sink that does not track it rather than raising -- reporting is never a
    reason for the execution path to fail.
    """
    observed = getattr(sink, "observed", None)
    if observed is not None:
        try:
            return len(observed)
        except TypeError:  # pragma: no cover - defensive
            return 0
    return 0


def _utcnow() -> dt.datetime:  # pragma: no cover - trivial, kept for symmetry
    return dt.datetime.now(dt.timezone.utc)
