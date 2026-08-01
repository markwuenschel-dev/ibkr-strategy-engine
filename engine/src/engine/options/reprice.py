"""Working an unfilled entry: a bounded cancel/replace ladder with a deadline.

This module exists because of a specific, observed failure. The engine's first
real order went ``Submitted`` and stayed there, working and unfilled, and there
was nothing the engine could do about it: no cancel, no reprice, no deadline.
The order sat in the book while the run reported it as unresolved and refused
every subsequent entry -- correctly, and permanently, because nothing in the
process could clear the condition.

**Every bound here is structural rather than configurable.**
:class:`RepriceLadder` refuses at construction to allow more than
:data:`MAXIMUM_ATTEMPTS` replaces or a time-to-live longer than
:data:`MAXIMUM_TIME_TO_LIVE`, in the same style as
:class:`engine.options.proof.ExecutionProofProfile`: the worst case of a ladder
is knowable by reading this file, and no environment variable can widen it.

**The ladder can only ever reduce the credit, and only inside the envelope.**
A resting credit spread that will not fill is asking for too much, so each rung
asks for less. The floor is not an opinion: it is
:func:`engine.options.proof.envelope_for`, the same band the risk assessment and
the governor were run against. A rung that would step below it does not step --
it cancels instead. This is why the ladder never needs to re-run the gates: it
cannot leave the region they approved.

**The ladder cancels rather than gives up.** When the attempts or the deadline
run out, the last act is a cancellation, not a shrug. Leaving a working order
behind at the end of a bounded process would recreate the exact state the module
was written to end.

**A rung sends nothing until the previous order is confirmed dead.** The cancel
comes first, and ``_cancel`` reports success only on a **terminal** snapshot.
An accepted cancel request is not a dead order: ``PendingCancel`` is a working
state, a timeout means we stopped waiting rather than that the broker stopped,
and a disconnect means nothing at all. Treating any of those as "cancelled"
puts a replacement into the book beside an order that is still working -- two
live orders for one approved structure, produced by the code written to prevent
duplicates. So an unconfirmed cancellation stops the ladder at
:attr:`RepriceStop.REFUSED` and it sends nothing more.

**A fill discovered during a cancel stops everything.** A cancel races a fill,
and losing that race means contracts are in the book. The ladder reads
``has_position`` on the cancellation result and stops there. Replacing an order
that partly filled is how one intended position becomes one and a half.

**Each replacement is recorded before it is sent, and journalled after.** Two
separate books, and a rung has to touch both:

``record_submission``
    The **position store**, before the send. A cancelled rung is a genuine
    ``OPEN_FAILED`` -- nothing is in the market -- and replaying that retires
    the position *and releases its buying-power reservation*. Without a fresh
    ``OPEN_SUBMITTED`` the replacement goes to the broker with nothing on disk
    describing it and with the governor looking at a book that believes it has
    that capital free. Same ordering ``run_once`` uses for the first send, and
    for the same reason: a crash after the record is recoverable, a crash after
    the send is not.

``record_transmission``
    The **order journal**, after the send. ``SafetyGate.gate_daily_count``
    counts ``order_placed`` records, so an order that never lands there is an
    order the daily cap cannot see. A ladder is one logical *entry*, but it is
    up to five real transmissions, and the cap counts transmissions. The
    consequence is deliberate and worth stating: with the default cap of five
    orders per session, one exhausted ladder is a session's budget. Five orders
    really did go to the broker.

Both are **required** arguments, for exactly the reason
:class:`~engine.options.sink.NullLifecycleSink` is a named type rather than a
``None`` default -- "nothing is being recorded" must be a visible decision at
the call site. :data:`RECORDED_ELSEWHERE` and :data:`NOT_JOURNALLED` are how a
caller says it out loud.

Nothing here transmits or cancels directly. Every send goes through
:func:`engine.options.transmit.place_combo` and every cancellation through
:func:`engine.options.transmit.cancel_combo`, each behind its own unforgeable
token.

Tick increments
---------------

An off-tick limit is rejected or silently rounded by the exchange, and a
silently rounded limit is an order at a price the engine did not choose. The
rule encoded in :func:`tick_size` is the OPRA/Penny Interval Program schedule as
stated in Cboe Rule 5.4(a) ("Minimum Increments for Bids and Offers") and
restated on the MIAX Penny Program pages:

===========================================  ==============  ==============
class                                        below $3.00     $3.00 and up
===========================================  ==============  ==============
SPY, QQQ, IWM, XSP                           $0.01           $0.01
in the Penny Interval Program                $0.01           $0.05
not in the Penny Interval Program            $0.05           $0.10
===========================================  ==============  ==============

Program membership is rebalanced every January and April and is published by
OCC, not by anything this process can read offline. So only the four all-price
classes are named here, and **every other symbol falls back to the non-penny
row**. That is deliberately the coarse answer: a coarser increment is always an
exact multiple of the finer one, so a price valid under the coarse rule is valid
under the fine rule too. Guessing the other way would produce limits the
exchange refuses.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import Enum
from typing import Any, Callable
from uuid import UUID

from ..errors import ConfigError, RefusedError
from ..safety import SafetyGate
from .domain import OptionStrategyIntent
from .orderstate import OrderLifecycleState
from .proof import PriceEnvelope, envelope_for
from .approval import ApprovalContext, VerifierGate
from .transmit import (
    RepricedOrder,
    TransmitAuthorization,
    TransmitResult,
    authorize_cancel,
    authorize_reprice,
    cancel_combo,
    place_combo,
)

__all__ = [
    "MAXIMUM_ATTEMPTS",
    "MAXIMUM_TIME_TO_LIVE",
    "PENNY_ALL_PRICES",
    "PENNY_BREAKPOINT",
    "NOT_JOURNALLED",
    "RECORDED_ELSEWHERE",
    "RepriceLadder",
    "RepriceOutcome",
    "RepriceStop",
    "DEFAULT_LADDER",
    "tick_size",
    "execution_increment",
    "PAPER_EXECUTION_INCREMENT",
    "round_to_tick",
    "work_order",
]


def RECORDED_ELSEWHERE(_intent: OptionStrategyIntent) -> None:  # noqa: N802
    """Persist nothing. For a caller that genuinely records somewhere else.

    Named, and spelled loudly, so that a ladder running with no durable record
    of its replacements is a decision someone typed rather than an argument
    someone forgot.
    """
    return None


def NOT_JOURNALLED(_result: TransmitResult) -> None:  # noqa: N802
    """Journal nothing. The same visible decision, for the order journal.

    A ladder wired this way is invisible to ``gate_daily_count``, so its
    replacements do not consume the session's order budget. That is only ever
    correct for a caller that journals them itself.
    """
    return None

ZERO = Decimal("0")

#: The hard ceiling on replaces. Four, from the milestone brief, and enforced at
#: construction so no caller can raise it.
MAXIMUM_ATTEMPTS = 4

#: The hard ceiling on how long an order may be worked before it is pulled.
MAXIMUM_TIME_TO_LIVE = dt.timedelta(minutes=2)

#: Where the standard increment schedule steps up.
PENNY_BREAKPOINT = Decimal("3.00")

#: Classes quoted in $0.01 at **every** price level. Cboe Rule 5.4(a) lists
#: QQQ, IWM and SPY explicitly, plus XSP for as long as SPY participates in the
#: Penny Interval Program.
PENNY_ALL_PRICES = frozenset({"SPY", "QQQ", "IWM", "XSP"})

#: The conservative fallback for a class whose Penny Program membership this
#: process cannot check. See the module docstring: coarser is always safe.
_NON_PENNY_BELOW = Decimal("0.05")
_NON_PENNY_ABOVE = Decimal("0.10")
_PENNY_ALL = Decimal("0.01")


def tick_size(symbol: str, price: Decimal) -> Decimal:
    """The minimum price increment for one option series. See the module docs."""
    if symbol.strip().upper() in PENNY_ALL_PRICES:
        return _PENNY_ALL
    return _NON_PENNY_BELOW if Decimal(price) < PENNY_BREAKPOINT else _NON_PENNY_ABOVE


#: What IBKR's *paper* simulator will actually fill, which is not what the
#: contract's ``minTick`` says may be quoted. IBKR documents that paper accounts
#: may submit US option orders at penny prices but do not receive penny fills,
#: alongside limited combo support and top-of-book simulated fills.
#:
#: This distinction cost a real experiment. A 1-wide SPY vertical walked
#: 0.21/0.20/0.19/0.18 on the penny grid would sit unfilled for simulator
#: reasons, and the result would read as evidence about *pricing* when it is
#: evidence about the *venue*. An order the simulator structurally cannot fill
#: teaches nothing about whether the price was right.
#:
#: This engine connects to paper ports by construction (``PAPER_PORTS``), so the
#: execution grid IS the paper grid. A live path would derive its increment from
#: contract and market-rule data instead, and must not inherit this constant.
PAPER_EXECUTION_INCREMENT = Decimal("0.05")


def execution_increment(symbol: str, price: Decimal) -> Decimal:
    """The grid a submitted price must land on to be *fillable* here.

    Deliberately separate from :func:`tick_size`, which is the OPRA quoting
    schedule. Quoting and filling are different facts, and conflating them is
    how a penny-quoted symbol produces an order the simulator will never fill.
    Never finer than the quoting increment -- a price off the quoting grid is
    rejected outright, which is a worse failure than one that merely rests.
    """
    quoting = tick_size(symbol, price)
    if Decimal(price) >= PENNY_BREAKPOINT:
        return max(quoting, _NON_PENNY_ABOVE)
    return max(quoting, PAPER_EXECUTION_INCREMENT)


def round_to_tick(price: Decimal, tick: Decimal, *, up: bool = False) -> Decimal:
    """Snap a price onto the increment grid.

    Rounds **down** by default, which for a credit means asking for less --
    the direction that gets filled. ``up`` is for step sizes, where rounding a
    step down could produce a zero-width rung that never makes progress.
    """
    if tick <= ZERO:
        raise ConfigError(f"tick increment must be positive, got {tick}")
    quotient = (Decimal(price) / tick).quantize(
        Decimal("1"), rounding=ROUND_UP if up else ROUND_DOWN
    )
    return (quotient * tick).quantize(tick)


class RepriceStop(str, Enum):
    """Why the ladder stopped. Nine order states compress to four reasons here.

    Named rather than reported as a boolean, for the same reason
    :class:`engine.options.positions.ReconciliationOutcome` is: "the ladder
    finished" is not one fact, and the four cases want different operator
    responses.
    """

    #: The order resolved on its own -- filled, rejected, or otherwise terminal.
    RESOLVED = "REPRICE_RESOLVED"
    #: The attempts or the deadline ran out and the order was cancelled.
    EXHAUSTED = "REPRICE_EXHAUSTED"
    #: The next rung would have left the approved envelope, so it was cancelled.
    ENVELOPE = "REPRICE_ENVELOPE_FLOOR"
    #: Something reached the market. Stop and let reconciliation resolve it.
    FILLED_DURING_CANCEL = "REPRICE_FILLED_DURING_CANCEL"
    #: The ladder could not act -- refused, or the broker stopped answering.
    REFUSED = "REPRICE_REFUSED"


@dataclass(frozen=True)
class RepriceLadder:
    """How hard, and for how long, an unfilled entry may be worked.

    Validated at construction against the module ceilings, so a ladder that
    exists is a ladder within bounds. ``attempt_timeout`` times
    ``maximum_attempts`` is what actually consumes the wall clock, and it
    defaults to exactly the two-minute deadline: four rungs, thirty seconds
    each.
    """

    maximum_attempts: int = MAXIMUM_ATTEMPTS
    time_to_live: dt.timedelta = MAXIMUM_TIME_TO_LIVE
    attempt_timeout: float = 30.0
    poll_seconds: float = 0.5
    cancel_timeout: float = 15.0

    def __post_init__(self) -> None:
        if not isinstance(self.maximum_attempts, int) or self.maximum_attempts < 1:
            raise ConfigError(
                f"maximum_attempts must be a positive int, got {self.maximum_attempts!r}"
            )
        if self.maximum_attempts > MAXIMUM_ATTEMPTS:
            raise ConfigError(
                f"maximum_attempts {self.maximum_attempts} exceeds the ceiling of "
                f"{MAXIMUM_ATTEMPTS}",
                hint="the bound is structural; a ladder cannot be configured looser "
                "than the module constant",
            )
        if self.time_to_live <= dt.timedelta(0):
            raise ConfigError(f"time_to_live must be positive, got {self.time_to_live}")
        if self.time_to_live > MAXIMUM_TIME_TO_LIVE:
            raise ConfigError(
                f"time_to_live {self.time_to_live} exceeds the ceiling of "
                f"{MAXIMUM_TIME_TO_LIVE}"
            )
        if self.attempt_timeout <= 0 or self.cancel_timeout <= 0:
            raise ConfigError("timeouts must be positive")

    def step_for(self, envelope: PriceEnvelope, *, tick: Decimal) -> Decimal:
        """How far one rung moves the credit.

        The whole usable band divided by the attempt budget, rounded **down** to
        a tick. Down rather than up because ``n`` steps of ``floor(room / n)``
        can never exceed ``room``: the ladder gets to use all four attempts
        instead of walking off the envelope floor on the last one and spending
        a rung on a refusal.

        Floored at one tick, because a step that rounded to zero would re-send
        the same price four times -- four attempts, no progress, and a
        ``RefusedError`` on the "did not change the price" guard.
        """
        room = envelope.reference - envelope.minimum
        if room <= ZERO:
            return tick
        return max(tick, round_to_tick(room / self.maximum_attempts, tick))


#: What ``run_once`` uses. A named module constant rather than a default
#: argument, so the bounds an unattended run is subject to are one lookup away.
DEFAULT_LADDER = RepriceLadder()


@dataclass(frozen=True)
class RepriceOutcome:
    """Everything the ladder did, and why it stopped."""

    strategy_id: UUID
    stop: RepriceStop
    attempts: int
    started_at: dt.datetime
    finished_at: dt.datetime
    final: TransmitResult | None
    prices: tuple[Decimal, ...] = ()
    cancelled: bool = False
    detail: str = ""

    @property
    def state(self) -> OrderLifecycleState:
        return self.final.state if self.final else OrderLifecycleState.UNKNOWN

    @property
    def has_position(self) -> bool:
        return bool(self.final and self.final.has_position)

    def describe(self) -> str:
        walk = " -> ".join(str(p) for p in self.prices) or "no reprice"
        return (
            f"{self.stop.value} after {self.attempts} attempt(s) [{walk}]"
            f"{'  CANCELLED' if self.cancelled else ''}"
            f"{'  ' + self.detail if self.detail else ''}"
        )

    def to_record(self) -> dict[str, Any]:
        # No ``event`` key: the journal takes the event name as its first
        # positional argument and expands this as keywords, so carrying one
        # here is a duplicate-argument TypeError at the call site.
        return {
            "strategy_id": str(self.strategy_id),
            "stop": self.stop.value,
            "attempts": self.attempts,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "prices": [str(p) for p in self.prices],
            "cancelled": self.cancelled,
            "state": self.state.value,
            "detail": self.detail or None,
        }


def work_order(
    ib: Any,
    intent: OptionStrategyIntent,
    result: TransmitResult,
    *,
    authorization: TransmitAuthorization,
    gate: SafetyGate,
    armed: bool,
    started_at: dt.datetime,
    clock: Callable[[], dt.datetime],
    record_submission: Callable[[OptionStrategyIntent], None],
    record_transmission: Callable[[TransmitResult], None],
    ladder: RepriceLadder = DEFAULT_LADDER,
    envelope: PriceEnvelope | None = None,
    account: str = "",
    sink: Any = None,
    closing: bool = False,
    verifier: VerifierGate | None = None,
    approval_context: ApprovalContext | None = None,
) -> RepriceOutcome:
    """Work a transmitted order that is still alive, then stop -- flat or filled.

    ``result`` is what :func:`~engine.options.transmit.place_combo` returned. If
    it is already terminal, or carries no broker handle, this returns
    immediately having done nothing: the ladder is for the one case that
    motivated it, an order the broker is working and will not fill.

    ``clock`` is injected rather than read from the wall clock so the deadline
    is a decision the caller can make deterministic. It is called, not sampled
    once, because the deadline is about elapsed time and a single sample cannot
    measure it.

    ``record_submission`` is called with each replacement **before** it is sent,
    and anything it raises propagates: an order that cannot be recorded must not
    be placed, which is the same contract ``run_once`` honours for the first
    send. It must restore the position's buying-power reservation, because the
    cancel that preceded it released one. ``record_transmission`` is called with
    each result **after** the send and must reach the order journal, or the
    daily order cap cannot see the transmission. Pass
    :data:`RECORDED_ELSEWHERE` / :data:`NOT_JOURNALLED` to say out loud that
    nothing is being written.

    Returns a :class:`RepriceOutcome`; raises only what the kill switch raises.
    Every broker interaction is already recorded by the sink as it happens --
    this function's own return value is a report, never the record.
    """
    envelope = envelope if envelope is not None else envelope_for(intent)
    tick = tick_size(intent.underlying, intent.limit_price)
    step = ladder.step_for(envelope, tick=tick)

    current_intent = intent
    current_auth = authorization
    current = result
    prices: list[Decimal] = []
    attempts = 0

    def done(stop: RepriceStop, *, cancelled: bool = False, detail: str = "") -> RepriceOutcome:
        return RepriceOutcome(
            strategy_id=intent.strategy_id,
            stop=stop,
            attempts=attempts,
            started_at=started_at,
            finished_at=clock(),
            final=current,
            prices=tuple(prices),
            cancelled=cancelled,
            detail=detail,
        )

    if current.trade is None:
        return done(
            RepriceStop.REFUSED,
            detail="no broker handle for the order; it cannot be worked",
        )

    while True:
        if _leave_alone(current):
            # Terminal, or something already reached the market. Either way
            # there is nothing here to work: a partially filled order is a
            # position to manage, and cancelling its remainder to re-send at a
            # lower credit would put a second position on top of a real one.
            return done(
                RepriceStop.RESOLVED,
                detail=(
                    f"{current.filled} already filled; not worked"
                    if current.has_position and not _is_finished(current)
                    else ""
                ),
            )

        expired = clock() - started_at >= ladder.time_to_live
        spent = attempts >= ladder.maximum_attempts
        next_price = round_to_tick(current_intent.limit_price - step, tick)
        floored = not envelope.contains(next_price)

        if expired or spent or floored:
            stop = (
                RepriceStop.ENVELOPE
                if floored and not (expired or spent)
                else RepriceStop.EXHAUSTED
            )
            detail = (
                f"the next rung {next_price} is below the envelope floor "
                f"{envelope.minimum}"
                if stop is RepriceStop.ENVELOPE
                else (
                    "the two-minute deadline expired"
                    if expired
                    else f"all {ladder.maximum_attempts} attempts were used"
                )
            )
            cancelled, current, failure = _cancel(
                ib,
                current,
                gate=gate,
                armed=armed,
                now=clock(),
                ladder=ladder,
                quantity=current_intent.quantity,
                strategy_id=intent.strategy_id,
                closing=closing,
                sink=sink,
                reason=detail,
            )
            if failure:
                return done(RepriceStop.REFUSED, cancelled=cancelled, detail=failure)
            return done(stop, cancelled=cancelled, detail=detail)

        # A rung. Cancel first, and only proceed on a **confirmed terminal**
        # cancellation -- ``_cancel`` returns a failure for anything else. A
        # replace is a new order, and sending it while the old one may still be
        # working leaves two live orders for one approved structure: the
        # duplicate every gate in this package exists to prevent, created by the
        # code meant to tidy up. "The cancel request was accepted" is not the
        # fact this needs; "the order is dead" is.
        cancelled, current, failure = _cancel(
            ib,
            current,
            gate=gate,
            armed=armed,
            now=clock(),
            ladder=ladder,
            quantity=current_intent.quantity,
            strategy_id=intent.strategy_id,
            closing=closing,
            sink=sink,
            reason=f"repricing to {next_price}",
        )
        if failure:
            return done(RepriceStop.REFUSED, cancelled=cancelled, detail=failure)
        if current.has_position:
            # The cancel lost the race with a fill. Contracts are in the book
            # and the sink has already recorded them; a replacement now would
            # open a second position on top of a real one.
            return done(
                RepriceStop.FILLED_DURING_CANCEL,
                cancelled=cancelled,
                detail=f"{current.filled} filled while cancelling; not replacing",
            )

        try:
            repriced: RepricedOrder = authorize_reprice(
                current_auth,
                current_intent,
                limit_price=next_price,
                envelope=envelope,
                tick=tick,
                gate=gate,
                armed=armed,
                now=clock(),
                # Passed straight through. A reprice of an OPEN needs its own
                # review -- the invalidation rule names price -- and
                # ``authorize_reprice`` refuses an opening reprice with no
                # verifier rather than proceeding on the previous approval. A
                # *closing* reprice needs neither and gets neither.
                verifier=verifier,
                context=approval_context,
            )
        except RefusedError as exc:
            return done(
                RepriceStop.REFUSED, cancelled=cancelled, detail=f"replace refused: {exc.message}"
            )

        # Recorded before the send, deliberately outside the try below: a
        # failure to persist must stop the ladder rather than be absorbed into
        # a "the replacement could not be sent" report, because those are
        # opposite states -- one has nothing at the broker, the other might.
        record_submission(repriced.intent)

        try:
            current = place_combo(
                ib,
                repriced.intent,
                authorization=repriced.authorization,
                account=account,
                timeout=ladder.attempt_timeout,
                poll_seconds=ladder.poll_seconds,
                sink=sink,
            )
        except Exception as exc:  # noqa: BLE001 - a failed replace reports, it does not crash
            return done(
                RepriceStop.REFUSED,
                cancelled=cancelled,
                detail=f"the replacement could not be sent: {type(exc).__name__}: {exc}",
            )

        # Journalled the moment the send returns. ``gate_daily_count`` counts
        # ``order_placed`` records, so a transmission that never lands in the
        # journal is a transmission the session's order cap cannot see -- and
        # four invisible orders per ladder is a cap that does not bind.
        record_transmission(current)

        current_intent = repriced.intent
        current_auth = repriced.authorization
        prices.append(next_price)
        attempts += 1


def _is_finished(result: TransmitResult) -> bool:
    """Whether the broker will send nothing further about this order.

    A **timeout** is deliberately not finished. It says we stopped waiting, not
    that the order stopped working, and reading it as terminal is what would
    leave the resting order this module exists to retract.
    """
    snapshot = result.snapshot
    if snapshot is None:
        return False
    return snapshot.is_terminal


def _leave_alone(result: TransmitResult) -> bool:
    """Whether the ladder must not touch this order.

    Two separate reasons, and they are separate on purpose: the order is over,
    **or** some quantity is already in the market. The second is not a state --
    a partial fill is still ``is_working`` -- and treating "working" as
    sufficient reason to cancel and replace is how a half-filled spread becomes
    one and a half positions.
    """
    return _is_finished(result) or result.has_position


def _cancel(
    ib: Any,
    current: TransmitResult,
    *,
    gate: SafetyGate,
    armed: bool,
    now: dt.datetime,
    ladder: RepriceLadder,
    quantity: int,
    strategy_id: UUID,
    closing: bool,
    sink: Any,
    reason: str,
) -> tuple[bool, TransmitResult, str]:
    """Pull the working order and **prove it died**.

    Returns ``(cancelled, latest, failure)``, where ``cancelled`` is true only
    when the broker reported a terminal state. Anything else -- a timeout, a
    disconnect, ``PendingCancel`` -- is a failure string, because the order may
    still be working and the caller's next move is to send another one.

    A refusal here -- the kill switch above all -- is returned rather than
    raised, because the ladder's caller is mid-pass with a live order and needs
    a report it can act on rather than an exception unwinding through the
    transmit path.
    """
    try:
        authorization = authorize_cancel(
            strategy_id, gate=gate, armed=armed, now=now, reason=reason
        )
    except Exception as exc:  # noqa: BLE001 - halted, unarmed, or refused
        return False, current, f"cancel refused: {exc}"

    try:
        cancelled = cancel_combo(
            ib,
            current.trade,
            authorization=authorization,
            closing=closing,
            quantity=quantity,
            timeout=ladder.cancel_timeout,
            poll_seconds=ladder.poll_seconds,
            sink=sink,
        )
    except Exception as exc:  # noqa: BLE001 - adapter boundary
        return False, current, f"the cancellation could not be sent: {exc}"

    # **Sending a cancel is not the same as the order being gone.** The request
    # can be accepted and the order keep working; ``PendingCancel`` is a working
    # state; a timeout means we stopped waiting, not that the broker stopped.
    # Only a terminal snapshot is evidence the order died.
    #
    # This check is the whole reason the cancel comes first. Without it, a
    # timed-out cancellation returned success and the ladder went straight on to
    # ``place_combo`` -- two live orders for one approved structure, created by
    # the code whose entire job was to prevent exactly that. Demonstrated, not
    # theorised: ``['cancelOrder', 'placeOrder', 'cancelOrder']`` against an
    # order still reporting ``Submitted``.
    snapshot = cancelled.snapshot
    if snapshot is None or not snapshot.is_terminal:
        state = snapshot.state.value if snapshot else "no broker response"
        return (
            False,
            cancelled,
            f"the cancellation did not complete ({state}); the order may still be "
            "working, so nothing further will be sent for it",
        )

    return True, cancelled, ""
