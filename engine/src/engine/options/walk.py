"""Bounded price discovery: give ground toward the natural, on a clock, or stop.

The engine's first live order priced at the midpoint, sat ``Submitted`` for 101
minutes of liquid regular-session trading, and never filled. Nothing was broken.
A limit at the mid is a bid for a price the spread does not trade at, and leaving
it there is not patience -- it is a decision to collect no evidence. Without
bounded price discovery, running more orders concurrently produces more
*working* orders and no fills, and therefore no partial-fill, no commission and
no close-cycle evidence either.

So: four attempts, thirty seconds each, roughly two minutes end to end.

    attempt 1   the midpoint
    attempt 2   one third of the way to the natural
    attempt 3   two thirds
    attempt 4   the natural, or the economic floor if that binds first
    then        cancel, and release the reserved risk

The credit moves monotonically **downward**. :mod:`engine.options.pricing` owns
that arithmetic and proves the sequence decreases; this module owns everything
the arithmetic cannot see.

**Five properties this module exists to hold, each of which is a way the obvious
implementation goes wrong:**

*Quotes are re-read every attempt.* Repricing off the book that justified the
last attempt is repricing into a market that has moved. A stale natural is worse
than no natural, because it looks like a measurement.

*The envelope is anchored to the authorized intent, once.* Recomputing
``envelope_for`` against the current rung would let the bound follow the walk
down, so every price would be trivially inside a band that had chased it there.
:func:`engine.options.proof.envelope_for` is called exactly once, at the top.

*Risk is re-verified at the new credit, every attempt.* **This is the subtlest
failure in the whole lane.** Maximum loss for a vertical is
``(width - credit) x multiplier``: giving up credit does not merely earn less, it
*raises the maximum loss*, and it raises broker margin and stress loss with it. A
walk that checks risk once at the midpoint and then re-prices four times has
walked straight out of the approved risk budget while every individual step
looked harmless. So each rung builds a new intent and runs it through the same
:func:`~engine.options.risk.assess_candidate` and the same
:class:`~engine.options.governor.PortfolioGovernor` the original entry went
through -- not a cheaper re-derivation of them, which would be a second copy of
the gates and would prove something about the copy.

*A partial fill is handled before the cancellation, not after.* The unfilled
quantity is what gets replaced. Re-sending the full size onto a one-of-three fill
opens two contracts nobody approved, and it is the same class of error as
:meth:`OptionStrategyIntent.closing_intent`'s removed default.

*A fill arriving during cancellation is a position, not a cancelled order.* The
walk keeps observing while it waits for the cancellation to confirm, and it will
not send the next rung until the previous order is confirmed terminal. An
unconfirmed cancellation ends the walk as ``UNCERTAIN`` -- because the one thing
worse than an unfilled spread is two of them.

**What this module does not own.** It never cancels an order itself. Cancellation
is a :class:`OrderCancellationPort`, a Protocol with no implementation here, so
the code that actually reaches the broker's cancel call lives in one place owned
by one lane and this module can be tested against a fake without a socket. The
assumption, stated plainly: an implementation of that port is responsible for
issuing the cancel and for reporting the order's state honestly, including
reporting a fill it observes while cancelling. If it lies about termination, this
module will send the next rung on top of a working order -- there is no way to
check that claim from here, which is exactly why it is a narrow seam and not an
inline call.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable
from uuid import UUID

from ..errors import EngineError, RefusedError
from ..safety import SafetyGate
from .domain import (
    OptionStrategyIntent,
    PriceEffect,
    StrategyAction,
    compute_maximum_loss_per_contract,
)
from .execution import MarginAssessment
from .governor import GovernorVerdict, PortfolioGovernor
from .marketdata import Liveness
from .orderstate import BrokerOrderSnapshot
from .policy import RiskPolicy
from .portfolio import PortfolioSnapshot, PositionExposure
from .pricing import PriceLadder, build_ladder, midpoint_credit, tick_regime_for
from .proof import PriceEnvelope, envelope_for, vertical_width
from .risk import CandidateRiskAssessment, assess_candidate
from .transmit import authorize_open, place_combo

__all__ = [
    "OrderCancellationPort",
    "RiskReverifier",
    "Reverification",
    "PolicyReverifier",
    "WalkPolicy",
    "WalkState",
    "WalkRefusal",
    "WalkAttempt",
    "WalkOutcome",
    "PriceWalk",
    "reprice",
    "DEFAULT_DWELL",
    "DEFAULT_MINIMUM_CREDIT",
    "DEFAULT_MINIMUM_CREDIT_FRACTION_OF_WIDTH",
    "DEFAULT_CANCELLATION_TIMEOUT",
]

ZERO = Decimal("0")

#: Thirty seconds a rung, four rungs: about two minutes of discovery. Long
#: enough that a resting order is genuinely exposed to the book, short enough
#: that the whole walk finishes inside a single market regime.
DEFAULT_DWELL = dt.timedelta(seconds=30)

#: The absolute credit below which this is no longer the trade that was
#: approved. A one-cent credit on a 1-wide spread is not a cheaper version of a
#: twenty-cent credit; it is a 99-to-1 risk-reward, which is a different trade
#: wearing the same strikes.
DEFAULT_MINIMUM_CREDIT = Decimal("0.05")

#: ...and the same idea expressed against the structure rather than the dollar,
#: so the floor scales with a 5-wide the way it does with a 1-wide. The larger of
#: the two applies.
DEFAULT_MINIMUM_CREDIT_FRACTION_OF_WIDTH = Decimal("0.10")

#: How long to wait for a cancellation to be confirmed before giving up on the
#: whole walk. Deliberately shorter than the dwell: an unconfirmed cancellation
#: is not something to be patient about, it is something to stop on.
DEFAULT_CANCELLATION_TIMEOUT = dt.timedelta(seconds=20)

DEFAULT_CANCELLATION_POLL = dt.timedelta(seconds=1)


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------


@runtime_checkable
class OrderCancellationPort(Protocol):
    """Pull a working order, and say honestly what happened to it.

    Two methods rather than one, because "wait for confirmed cancellation" is
    unimplementable without a way to observe. A single ``cancel()`` returning a
    boolean would have to do its own waiting, which puts the walk's timeout
    policy inside the adapter where it cannot be tested.

    Neither method appears in this package's transmitting surface: the call that
    actually reaches the broker lives in the implementation, which another lane
    owns. See the module docstring for the trust assumption that seam carries.
    """

    def request_cancellation(
        self, *, strategy_id: UUID, order_id: int | None, perm_id: int | None
    ) -> None:
        """Ask the broker to pull the order. Returns before it is confirmed."""
        ...

    def observe_order(
        self, *, strategy_id: UUID, order_id: int | None, perm_id: int | None
    ) -> BrokerOrderSnapshot | None:
        """The order's current state, or ``None`` if it cannot be found.

        ``None`` is not "cancelled". An order the broker cannot tell us about is
        the uncertain case, and the walk stops on it.
        """
        ...


@dataclass(frozen=True)
class Reverification:
    """Whether this exact structure, at this exact credit, is still approved.

    ``approved`` is not a field a caller may set independently of the evidence:
    construction refuses an approval that is missing either verdict or that
    carries one which did not itself approve. Otherwise a reverifier could report
    ``approved=True`` alongside a refusing assessment and the walk would believe
    it, which is the whole failure this class exists to make unrepresentable.
    """

    approved: bool
    risk: CandidateRiskAssessment | None = None
    governor: GovernorVerdict | None = None
    margin: MarginAssessment | None = None
    refusals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.approved, bool):
            raise ValueError("approved must be a bool")
        if not self.approved:
            if not self.refusals:
                raise ValueError("a refused reverification must say why")
            return
        if self.risk is None or self.governor is None:
            raise ValueError(
                "an approved reverification must carry both a risk assessment and "
                "a governor verdict"
            )
        if not self.risk.approved or not self.governor.approved:
            raise ValueError(
                "an approved reverification cannot carry a verdict that refused"
            )
        if self.refusals:
            raise ValueError("an approved reverification must not carry refusals")

    @property
    def reason_codes(self) -> tuple[str, ...]:
        codes: list[str] = []
        if self.risk is not None:
            codes.extend(self.risk.reason_codes)
        if self.governor is not None:
            codes.extend(self.governor.reason_codes)
        return tuple(codes)


@runtime_checkable
class RiskReverifier(Protocol):
    """Re-run every gate against a repriced structure.

    A Protocol rather than a concrete dependency so the walk can be driven in a
    test by a reverifier that refuses on the third rung, which is the only
    practical way to prove the walk stops when the risk budget is breached.
    :class:`PolicyReverifier` is the real one.
    """

    def reverify(
        self,
        intent: OptionStrategyIntent,
        *,
        quotes: Any,
        now: dt.datetime,
    ) -> Reverification:
        ...


@dataclass
class PolicyReverifier:
    """The live reverifier: the same gates the original entry passed, again.

    Deliberately a composition of existing functions and not one line of new risk
    arithmetic. The number that must not drift is ``maximum_loss``, and it is
    recomputed by :class:`~engine.options.domain.OptionStrategyIntent` itself at
    construction; everything downstream of that reads it rather than deriving it
    a second way.

    The broker what-if is re-asked at every rung on purpose. Margin on a defined
    risk spread tracks ``width - credit``, so a walk that reuses the midpoint's
    what-if is comparing the *old* margin against the cap while sending the new
    credit -- passing a check it never actually ran.
    """

    policy: RiskPolicy
    what_if: Any
    portfolio: Any = None
    exposures: Callable[[], Sequence[PositionExposure]] | None = None

    def reverify(
        self,
        intent: OptionStrategyIntent,
        *,
        quotes: Any,
        now: dt.datetime,
    ) -> Reverification:
        try:
            margin = self.what_if.what_if(intent, observed_at=now)
        except Exception as exc:  # noqa: BLE001 - adapter boundary; fail closed
            return Reverification(
                approved=False,
                refusals=(
                    f"the broker what-if failed at the new credit "
                    f"{intent.limit_price}: {type(exc).__name__}: {exc}",
                ),
            )

        book: PortfolioSnapshot | None = None
        if self.portfolio is not None:
            try:
                book = self.portfolio.snapshot(as_of=now)
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                return Reverification(
                    approved=False,
                    refusals=(
                        f"the portfolio could not be read at the new credit: "
                        f"{type(exc).__name__}: {exc}",
                    ),
                )
        if book is not None and self.exposures is not None:
            book = PortfolioSnapshot(
                as_of=book.as_of,
                net_liquidation=book.net_liquidation,
                positions=self.exposures(),
                reported_buying_power_reserved=book.reported_buying_power_reserved,
            )

        risk = assess_candidate(
            intent,
            policy=self.policy,
            quotes=quotes,
            margin=margin,
            underlying_price=quotes.underlying.mid if quotes is not None else None,
            net_liquidation=book.net_liquidation if book is not None else None,
            evaluated_at=now,
        )
        governor = PortfolioGovernor(self.policy).evaluate(
            intent, snapshot=book, margin=margin, decision_time=now
        )
        if risk.approved and governor.approved:
            return Reverification(
                approved=True, risk=risk, governor=governor, margin=margin
            )

        refusals = tuple(
            [f"candidate risk: {r.detail}" for r in risk.refusals]
            + [f"portfolio governor: {r.detail}" for r in governor.refusals]
        )
        return Reverification(
            approved=False,
            risk=risk,
            governor=governor,
            margin=margin,
            refusals=refusals or ("the reverification refused without a reason",),
        )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkPolicy:
    """The shape of one walk. Not read from the environment.

    Kept out of :class:`~engine.options.policy.RiskPolicy` on purpose. That class
    is the account's risk budget and every field in it caps something; none of
    these cap anything. A dwell of sixty seconds is not riskier than thirty, it
    is slower, and mixing the two kinds of number in one object makes "may the
    environment widen this" an unanswerable question per field.
    """

    dwell: dt.timedelta = DEFAULT_DWELL
    minimum_credit: Decimal = DEFAULT_MINIMUM_CREDIT
    minimum_credit_fraction_of_width: Decimal = (
        DEFAULT_MINIMUM_CREDIT_FRACTION_OF_WIDTH
    )
    cancellation_timeout: dt.timedelta = DEFAULT_CANCELLATION_TIMEOUT
    cancellation_poll: dt.timedelta = DEFAULT_CANCELLATION_POLL
    maximum_attempts: int = 4

    def __post_init__(self) -> None:
        if self.maximum_attempts < 1:
            raise ValueError("a walk must make at least one attempt")
        if self.dwell.total_seconds() <= 0:
            raise ValueError("the dwell must be positive")
        if self.cancellation_timeout.total_seconds() <= 0:
            raise ValueError("the cancellation timeout must be positive")
        if self.cancellation_poll.total_seconds() <= 0:
            raise ValueError("the cancellation poll interval must be positive")
        if self.minimum_credit <= ZERO:
            raise ValueError("the minimum credit must be positive")
        if not (ZERO <= self.minimum_credit_fraction_of_width < Decimal("1")):
            raise ValueError(
                "the minimum-credit fraction of width must be in [0, 1)"
            )

    def floor_for(self, intent: OptionStrategyIntent) -> Decimal:
        """The economic floor for this structure: the larger of the two rules.

        A structure whose width cannot be read -- anything that is not a single
        vertical -- falls back to the absolute floor alone rather than to no
        floor. An unreadable width is a reason to be more careful, not less.
        """
        width = vertical_width(intent)
        if width is None:
            return self.minimum_credit
        return max(self.minimum_credit, width * self.minimum_credit_fraction_of_width)


# ---------------------------------------------------------------------------
# Repricing
# ---------------------------------------------------------------------------


def reprice(
    intent: OptionStrategyIntent,
    *,
    credit: Decimal,
    quantity: int,
    created_at: dt.datetime,
) -> OptionStrategyIntent:
    """The same logical entry, at a new credit and possibly a smaller size.

    **The lineage is the ``strategy_id``, and it is preserved.** Every rung of a
    walk is one decision to open one position; giving each attempt a fresh id
    would scatter a single logical entry across four unrelated records and leave
    the position store unable to say how many contracts it had asked for.

    **The structure digest is not preserved, and must not be.** It is derived,
    never carried: ``limit_price``, ``maximum_loss_per_contract`` and ``quantity``
    are all inputs to
    :func:`engine.options.transmit.structure_digest`, so a repriced intent
    fingerprints differently by construction and the check at
    ``transmit.py:404`` will reject an authorization minted for the previous
    rung. That is the intended behaviour: each rung is separately authorized
    against its own numbers.

    ``maximum_loss_per_contract`` is recomputed rather than scaled, and the
    domain recomputes it *again* at construction and refuses a mismatch. Two
    independent computations of the number that decides whether this is still an
    approved trade, because it is the number a repricing walk silently ruins.
    """
    if intent.strategy_action is not StrategyAction.OPEN:
        raise RefusedError(
            f"only an opening intent is repriced by the walk, got "
            f"{intent.strategy_action.value}"
        )
    if intent.price_effect is not PriceEffect.CREDIT:
        raise RefusedError("the price walk reprices credit structures only")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise RefusedError(f"quantity must be a positive int, got {quantity!r}")
    if quantity > intent.quantity:
        raise RefusedError(
            f"cannot reprice {quantity} contracts of a {intent.quantity}-contract "
            "entry",
            hint="a walk replaces the unfilled remainder and never grows it",
        )

    maximum_loss = compute_maximum_loss_per_contract(
        strategy_type=intent.strategy_type,
        legs=intent.legs,
        credit=credit,
        multiplier=intent.multiplier,
    )
    return OptionStrategyIntent(
        strategy_id=intent.strategy_id,
        strategy_type=intent.strategy_type,
        strategy_action=StrategyAction.OPEN,
        underlying=intent.underlying,
        quantity=quantity,
        legs=intent.legs,
        expiration=intent.expiration,
        limit_price=credit,
        price_effect=PriceEffect.CREDIT,
        maximum_loss_per_contract=maximum_loss,
        configuration_version=intent.configuration_version,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


class WalkState(str, Enum):
    """How a walk ended. Five outcomes, because they need five responses."""

    #: Every contract asked for is in the market.
    FILLED = "WALK_FILLED"
    #: Some contracts filled; the rest were cancelled and released.
    PARTIALLY_FILLED = "WALK_PARTIALLY_FILLED"
    #: Four rungs offered, none filled, order cancelled, risk released.
    EXHAUSTED = "WALK_EXHAUSTED"
    #: A gate refused mid-walk. Any working order was cancelled first.
    REFUSED = "WALK_REFUSED"
    #: A cancellation was never confirmed. **Nothing further was sent.**
    UNCERTAIN = "WALK_UNCERTAIN"


class WalkRefusal(str, Enum):
    """Machine-readable causes, so a journal line can be branched on."""

    NO_QUOTES = "WALK_NO_LIVE_QUOTES"
    NOT_LIVE = "WALK_QUOTES_NOT_UNIFORMLY_LIVE"
    UNPRICEABLE = "WALK_BOOK_UNPRICEABLE"
    OUTSIDE_ENVELOPE = "WALK_OUTSIDE_PRICE_ENVELOPE"
    RISK_AT_NEW_CREDIT = "WALK_RISK_REFUSED_AT_NEW_CREDIT"
    STRUCTURE_INVALID = "WALK_STRUCTURE_INVALID_AT_NEW_CREDIT"
    NOT_AUTHORIZED = "WALK_AUTHORIZATION_REFUSED"
    SEND_FAILED = "WALK_SEND_FAILED"
    CANCELLATION_UNCONFIRMED = "WALK_CANCELLATION_UNCONFIRMED"
    LADDER_EXHAUSTED = "WALK_LADDER_EXHAUSTED"


@dataclass(frozen=True)
class WalkAttempt:
    """One rung: what was offered, at what size, and what came back."""

    index: int
    credit: Decimal | None
    quantity: int | None
    sent: bool
    filled_here: Decimal = ZERO
    snapshot: BrokerOrderSnapshot | None = None
    refusal: WalkRefusal | None = None
    detail: str = ""

    def describe(self) -> str:
        if not self.sent:
            return (
                f"  {self.index}  NOT SENT  credit={self.credit}  "
                f"[{self.refusal.value if self.refusal else '-'}] {self.detail}"
            )
        state = self.snapshot.state.value if self.snapshot else "no response"
        return (
            f"  {self.index}  credit={self.credit} x{self.quantity}  "
            f"{state}  filled_here={self.filled_here}"
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "credit": str(self.credit) if self.credit is not None else None,
            "quantity": self.quantity,
            "sent": self.sent,
            "filled_here": str(self.filled_here),
            "snapshot": self.snapshot.to_record() if self.snapshot else None,
            "refusal": self.refusal.value if self.refusal else None,
            "detail": self.detail or None,
        }


@dataclass(frozen=True)
class WalkOutcome:
    """What a whole walk did, in the form the journal and the store both need."""

    strategy_id: UUID
    state: WalkState
    ordered: int
    filled: Decimal
    attempts: tuple[WalkAttempt, ...]
    envelope: PriceEnvelope | None = None
    ladder: PriceLadder | None = None
    released: Decimal = ZERO
    detail: str = ""

    @property
    def remaining(self) -> Decimal:
        return Decimal(self.ordered) - self.filled

    @property
    def has_position(self) -> bool:
        """Some quantity reached the market. What the position store keys off."""
        return self.filled > ZERO

    @property
    def credits(self) -> tuple[Decimal, ...]:
        """The credit sequence actually offered, in order."""
        return tuple(a.credit for a in self.attempts if a.sent and a.credit is not None)

    def describe(self) -> str:
        lines = [
            f"PRICE WALK  {self.state.value}  {self.strategy_id}",
            f"  ordered {self.ordered}, filled {self.filled}, "
            f"remaining {self.remaining}",
        ]
        if self.ladder is not None:
            lines.append(f"  {self.ladder.describe()}")
        if self.envelope is not None:
            lines.append(f"  envelope: {self.envelope.describe()}")
        lines.extend(a.describe() for a in self.attempts)
        if self.detail:
            lines.append(f"  {self.detail}")
        return "\n".join(lines)

    def to_record(self) -> dict[str, Any]:
        return {
            "strategy_id": str(self.strategy_id),
            "state": self.state.value,
            "ordered": self.ordered,
            "filled": str(self.filled),
            "remaining": str(self.remaining),
            "released": str(self.released),
            "envelope": self.envelope.to_record() if self.envelope else None,
            "ladder": self.ladder.to_record() if self.ladder else None,
            "attempts": [a.to_record() for a in self.attempts],
            "detail": self.detail or None,
        }


@dataclass
class _Working:
    """The order currently resting in the book, and how much of it we have
    already counted. ``credited`` is what stops a fill being added twice when
    the same cumulative ``filled`` is observed on two consecutive polls."""

    intent: OptionStrategyIntent
    order_id: int | None
    perm_id: int | None
    credited: Decimal = ZERO
    snapshot: BrokerOrderSnapshot | None = None


@dataclass(frozen=True)
class _Settlement:
    confirmed: bool
    newly_filled: Decimal
    snapshot: BrokerOrderSnapshot | None


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


@dataclass
class PriceWalk:
    """Four bounded attempts to buy the same structure a little more cheaply.

    Every dependency is a seam, and every seam exists because the alternative is
    a test that cannot be written: the clock so a two-minute walk runs in
    microseconds, the cancellation port so no socket is needed, the reverifier so
    a refusal can be induced at rung three specifically.

    The only thing that is *not* injectable is the order in which the steps run.
    That is the whole design, so it lives in one method and is asserted by tests
    rather than configured.
    """

    ib: Any
    market_data: Any
    cancellation: OrderCancellationPort
    reverifier: RiskReverifier
    gate: SafetyGate
    policy: WalkPolicy = field(default_factory=WalkPolicy)
    sink: Any = None
    account: str = ""
    clock: Callable[[], dt.datetime] | None = None
    pause: Callable[[float], None] | None = None
    emit: Callable[[str], None] | None = None
    on_fill: Callable[[UUID, Decimal, BrokerOrderSnapshot], None] | None = None
    on_release: Callable[[UUID, Decimal], None] | None = None

    # -- small helpers ----------------------------------------------------

    def _now(self) -> dt.datetime:
        if self.clock is not None:
            return self.clock()
        return dt.datetime.now(dt.timezone.utc)

    def _sleep(self, seconds: float) -> None:
        """Yield for ``seconds``, preferring the broker's own sleep.

        ``ib.sleep`` runs the ib_async event loop while it waits;
        ``time.sleep`` blocks the socket, so callbacks that arrive during the
        pause are not processed until it ends -- which for a poll loop waiting on
        exactly those callbacks is a deadlock wearing a timeout's clothes.
        """
        if self.pause is not None:
            self.pause(seconds)
            return
        broker_sleep = getattr(self.ib, "sleep", None)
        if callable(broker_sleep):
            broker_sleep(seconds)
            return
        time.sleep(seconds)  # pragma: no cover - no fake reaches this

    def _say(self, line: str) -> None:
        if self.emit is not None:
            self.emit(line)

    def _fresh_quotes(
        self, intent: OptionStrategyIntent
    ) -> tuple[Any, WalkRefusal | None, str]:
        """Step 1: a live quote for every leg, or a refusal.

        Uniform liveness is re-checked here and not assumed from the entry
        snapshot. A subscription can be downgraded to delayed between two reads,
        and repricing off a delayed book is how the walk would give ground to a
        market that is not the one it is trading in.
        """
        try:
            snapshot = self.market_data.strategy_quotes(
                underlying_symbol=intent.underlying,
                con_ids=[leg.con_id for leg in intent.legs],
            )
        except Exception as exc:  # noqa: BLE001 - adapter boundary; fail closed
            return (
                None,
                WalkRefusal.NO_QUOTES,
                f"the book could not be re-read: {type(exc).__name__}: {exc}",
            )
        if snapshot is None:
            return None, WalkRefusal.NO_QUOTES, "no quote snapshot for the legs"
        livenesses = {quote.provenance.liveness for quote in snapshot.legs}
        if livenesses != {Liveness.LIVE}:
            return (
                snapshot,
                WalkRefusal.NOT_LIVE,
                "the re-read of the book is not uniformly live "
                f"({sorted(state.value for state in livenesses)})",
            )
        return snapshot, None, ""

    def _count(self, working: _Working, snapshot: BrokerOrderSnapshot | None) -> Decimal:
        """Fold a fresh observation into the running fill total.

        ``filled`` on a snapshot is cumulative for that order, so the delta is
        what matters. Counting the cumulative figure on every poll is how a
        one-lot fill becomes six.
        """
        if snapshot is None:
            return ZERO
        working.snapshot = snapshot
        if snapshot.filled <= working.credited:
            return ZERO
        delta = snapshot.filled - working.credited
        working.credited = snapshot.filled
        if self.on_fill is not None:
            self.on_fill(working.intent.strategy_id, delta, snapshot)
        return delta

    def _observe(self, working: _Working) -> Decimal:
        try:
            snapshot = self.cancellation.observe_order(
                strategy_id=working.intent.strategy_id,
                order_id=working.order_id,
                perm_id=working.perm_id,
            )
        except Exception:  # noqa: BLE001 - an unreadable order is not a fill
            return ZERO
        return self._count(working, snapshot)

    def _settle(self, working: _Working) -> _Settlement:
        """Steps 6 and 7: cancel, then wait until the order is confirmed done.

        Fills are harvested *throughout*, not merely at the end. A fill that
        arrives while the cancellation is in flight is a real position -- IBKR
        will report the order ``Cancelled`` with a positive ``filled``, and a
        walk that read only the final status would record a live spread as a
        cancelled order.

        Returns ``confirmed=False`` on timeout, and the caller must then send
        nothing further. An order we cannot prove is dead may still be working,
        and replacing it would put two spreads in the book where one was
        approved.
        """
        newly = ZERO
        try:
            self.cancellation.request_cancellation(
                strategy_id=working.intent.strategy_id,
                order_id=working.order_id,
                perm_id=working.perm_id,
            )
        except Exception as exc:  # noqa: BLE001 - a failed cancel is uncertainty
            self._say(f"  cancellation request failed: {type(exc).__name__}: {exc}")
            return _Settlement(False, newly, working.snapshot)

        deadline = self._now() + self.policy.cancellation_timeout
        poll = self.policy.cancellation_poll.total_seconds()
        while True:
            try:
                snapshot = self.cancellation.observe_order(
                    strategy_id=working.intent.strategy_id,
                    order_id=working.order_id,
                    perm_id=working.perm_id,
                )
            except Exception:  # noqa: BLE001
                snapshot = None
            newly += self._count(working, snapshot)
            if snapshot is not None and snapshot.is_terminal:
                return _Settlement(True, newly, snapshot)
            if self._now() >= deadline:
                return _Settlement(False, newly, working.snapshot)
            self._sleep(poll)

    # -- the walk ---------------------------------------------------------

    def run(
        self, intent: OptionStrategyIntent, *, armed: bool = True
    ) -> WalkOutcome:
        """Walk the price down until it fills, a gate refuses, or the rungs end.

        The step order inside the loop is the contract, and it is this:

        1. harvest any fill on the working order **before** anything else;
        2. re-read the book, live, for every leg;
        3. recompute the natural, the midpoint and the tick-quantized ladder;
        4. check the rung against the anchored authorization envelope;
        5. reprice the intent and **re-verify max loss, margin and stress at the
           new credit**;
        6. harvest again, then request the cancellation of the prior order;
        7. wait for that cancellation to be confirmed, harvesting throughout, and
           re-verify once more if the remaining quantity moved in the meantime;
        8. authorize the new intent -- fresh digest -- and send it.

        Steps 1 and 6 both harvest on purpose. Step 1 catches a fill from the
        dwell, so the rung is priced for the right size; step 6 catches one that
        arrived while step 5 was talking to the broker, so the order that is
        about to be cancelled is not carrying an unrecorded position.
        """
        envelope = envelope_for(intent)
        floor = self.policy.floor_for(intent)
        regime = tick_regime_for(intent.underlying)
        ordered = intent.quantity
        filled = ZERO
        attempts: list[WalkAttempt] = []
        ladder: PriceLadder | None = None
        working: _Working | None = None
        state: WalkState | None = None
        detail = ""

        self._say(f"PRICE WALK  {intent.describe()}")
        self._say(f"  envelope: {envelope.describe()}  economic floor {floor}")

        def finish(
            final: WalkState, why: str = "", released: Decimal = ZERO
        ) -> WalkOutcome:
            return WalkOutcome(
                strategy_id=intent.strategy_id,
                state=final,
                ordered=ordered,
                filled=filled,
                attempts=tuple(attempts),
                envelope=envelope,
                ladder=ladder,
                released=released,
                detail=why,
            )

        for index in range(self.policy.maximum_attempts):
            rung = index + 1

            # -- 1. harvest first, so this rung is priced for the right size --
            if working is not None:
                filled += self._observe(working)
                if filled >= Decimal(ordered):
                    return finish(WalkState.FILLED)

            remaining = int(Decimal(ordered) - filled)
            now = self._now()

            # -- 2. fresh live quotes for every leg -----------------------
            quotes, refusal, why = self._fresh_quotes(intent)
            if refusal is not None:
                attempts.append(
                    WalkAttempt(rung, None, remaining, False, refusal=refusal, detail=why)
                )
                state, detail = WalkState.REFUSED, why
                break

            # -- 3. has the book left the authorized band entirely? -------
            # Checked against the recomputed *midpoint*, before the ladder is
            # built, because the ladder clamps into the envelope by construction
            # and would therefore never report that the market had left it. A
            # bound that silently rescues every price it is asked about is not
            # reporting anything. Bounded on both sides: a book that has moved in
            # our favour is still a book that has moved, and the risk figures
            # were computed against the old one.
            observed = midpoint_credit(intent, quotes)
            if observed is None:
                why = "the book is unpriceable: a leg has no two-sided market"
                attempts.append(
                    WalkAttempt(
                        rung, None, remaining, False,
                        refusal=WalkRefusal.UNPRICEABLE, detail=why,
                    )
                )
                state, detail = WalkState.REFUSED, why
                break
            if not envelope.contains(observed):
                why = (
                    f"the book has moved outside the authorized envelope: the "
                    f"midpoint is now {observed}, not between {envelope.minimum} "
                    f"and {envelope.maximum}"
                )
                attempts.append(
                    WalkAttempt(
                        rung, observed, remaining, False,
                        refusal=WalkRefusal.OUTSIDE_ENVELOPE, detail=why,
                    )
                )
                state, detail = WalkState.REFUSED, why
                break

            # -- 4. natural, midpoint, tick grid --------------------------
            ladder = build_ladder(
                intent,
                quotes,
                envelope=envelope,
                minimum_credit=floor,
                regime=regime,
            )
            if ladder is None:
                why = (
                    "the book will not support a walk: a leg is unpriced, the "
                    "market is crossed, or the envelope floor has risen above "
                    "its ceiling"
                )
                attempts.append(
                    WalkAttempt(
                        rung, None, remaining, False,
                        refusal=WalkRefusal.UNPRICEABLE, detail=why,
                    )
                )
                state, detail = WalkState.REFUSED, why
                break
            if index >= ladder.attempts:
                # Quantization collapsed the remaining rungs onto prices already
                # offered. There is nothing left to try that is not a re-send of
                # the current price at the cost of queue priority.
                why = (
                    f"the ladder has {ladder.attempts} distinct rung(s) on the "
                    f"{ladder.regime.name} grid; nothing further to offer"
                )
                attempts.append(
                    WalkAttempt(
                        rung, None, remaining, False,
                        refusal=WalkRefusal.LADDER_EXHAUSTED, detail=why,
                    )
                )
                state, detail = WalkState.EXHAUSTED, why
                break
            credit = ladder.rungs[index]

            # The rung itself, belt and braces. The ladder clamps into the band,
            # so this can only fire if the clamp is ever broken -- which is
            # exactly when a silent failure would be most expensive.
            if not envelope.contains(credit):
                why = (
                    f"rung {rung} at {credit} is outside the authorized envelope "
                    f"[{envelope.minimum}, {envelope.maximum}]"
                )
                attempts.append(
                    WalkAttempt(
                        rung, credit, remaining, False,
                        refusal=WalkRefusal.OUTSIDE_ENVELOPE, detail=why,
                    )
                )
                state, detail = WalkState.REFUSED, why
                break

            # -- 5. reprice, and re-verify risk AT THE NEW CREDIT ---------
            outcome = self._price_and_verify(intent, credit, remaining, now, quotes)
            if isinstance(outcome, WalkAttempt):
                attempts.append(_with_index(outcome, rung))
                state, detail = WalkState.REFUSED, outcome.detail
                break
            candidate, verdict = outcome

            # -- 6 & 7. cancel the prior order and wait for confirmation ---
            if working is not None:
                filled += self._observe(working)
                if filled >= Decimal(ordered):
                    return finish(WalkState.FILLED)
                settlement = self._settle(working)
                filled += settlement.newly_filled
                if filled >= Decimal(ordered):
                    return finish(WalkState.FILLED)
                if not settlement.confirmed:
                    why = (
                        f"the cancellation of order {working.order_id} was not "
                        f"confirmed within {self.policy.cancellation_timeout}; "
                        "refusing to send another rung on top of it"
                    )
                    attempts.append(
                        WalkAttempt(
                            rung, credit, remaining, False,
                            refusal=WalkRefusal.CANCELLATION_UNCONFIRMED,
                            detail=why,
                            snapshot=settlement.snapshot,
                        )
                    )
                    return finish(
                        WalkState.UNCERTAIN
                        if filled == ZERO
                        else WalkState.PARTIALLY_FILLED,
                        why,
                    )
                working = None

                # A fill landed between step 5 and here. The size changed, so the
                # structure changed, so the verdict is for a different order.
                settled_remaining = int(Decimal(ordered) - filled)
                if settled_remaining != remaining:
                    remaining = settled_remaining
                    outcome = self._price_and_verify(
                        intent, credit, remaining, self._now(), quotes
                    )
                    if isinstance(outcome, WalkAttempt):
                        attempts.append(_with_index(outcome, rung))
                        return finish(
                            WalkState.PARTIALLY_FILLED
                            if filled > ZERO
                            else WalkState.REFUSED,
                            outcome.detail,
                        )
                    candidate, verdict = outcome

            # -- 8. authorize the NEW intent and send ---------------------
            try:
                authorization = authorize_open(
                    candidate,
                    gate=self.gate,
                    risk=verdict.risk,
                    governor=verdict.governor,
                    armed=armed,
                    now=self._now(),
                )
            except EngineError as exc:
                # Includes HaltedError. A kill switch thrown mid-walk ends the
                # walk here and the resting order is cancelled below, which is
                # the behaviour the operator asked for: stop, and shed what is
                # not yet in the market.
                why = f"rung {rung} was not authorized: {exc}"
                attempts.append(
                    WalkAttempt(
                        rung, credit, remaining, False,
                        refusal=WalkRefusal.NOT_AUTHORIZED, detail=why,
                    )
                )
                state, detail = WalkState.REFUSED, why
                break

            self._say(f"  rung {rung}: credit {credit} x{remaining}")
            try:
                result = place_combo(
                    self.ib,
                    candidate,
                    authorization=authorization,
                    account=self.account,
                    timeout=self.policy.dwell.total_seconds(),
                    sink=self.sink,
                )
            except Exception as exc:  # noqa: BLE001 - a failed send is recorded
                why = f"rung {rung} could not be sent: {type(exc).__name__}: {exc}"
                attempts.append(
                    WalkAttempt(
                        rung, credit, remaining, False,
                        refusal=WalkRefusal.SEND_FAILED, detail=why,
                    )
                )
                state, detail = WalkState.REFUSED, why
                break

            working = _Working(
                intent=candidate,
                order_id=result.order_id,
                perm_id=result.perm_id,
            )
            here = self._count(working, result.snapshot)
            filled += here
            attempts.append(
                WalkAttempt(
                    index=rung,
                    credit=credit,
                    quantity=remaining,
                    sent=True,
                    filled_here=here,
                    snapshot=result.snapshot,
                )
            )
            if filled >= Decimal(ordered):
                return finish(WalkState.FILLED)
            if result.snapshot is not None and result.snapshot.is_terminal:
                # Rejected, or cancelled by the broker. There is nothing resting
                # to replace, so the next rung starts clean rather than trying to
                # cancel an order that is already dead.
                working = None

        else:
            state = WalkState.EXHAUSTED
            detail = f"all {self.policy.maximum_attempts} rungs offered without a fill"

        # -- the walk is over: pull whatever is still resting -------------
        released = ZERO
        if working is not None:
            filled += self._observe(working)
            if filled >= Decimal(ordered):
                return finish(WalkState.FILLED)
            settlement = self._settle(working)
            filled += settlement.newly_filled
            if filled >= Decimal(ordered):
                return finish(WalkState.FILLED)
            if not settlement.confirmed:
                return finish(
                    WalkState.UNCERTAIN if filled == ZERO else WalkState.PARTIALLY_FILLED,
                    "the final cancellation was never confirmed",
                )
            released = Decimal(ordered) - filled
            if self.on_release is not None and released > ZERO:
                self.on_release(intent.strategy_id, released)

        if filled > ZERO and filled < Decimal(ordered):
            state = WalkState.PARTIALLY_FILLED
        elif filled >= Decimal(ordered):
            state = WalkState.FILLED
        return finish(state or WalkState.EXHAUSTED, detail, released)

    # -- step 5, extracted so it can be run twice per rung ----------------

    def _price_and_verify(
        self,
        intent: OptionStrategyIntent,
        credit: Decimal,
        quantity: int,
        now: dt.datetime,
        quotes: Any,
    ) -> tuple[OptionStrategyIntent, Reverification] | WalkAttempt:
        """Build the repriced intent and run every gate against it.

        Returns the pair on approval and a refusal :class:`WalkAttempt` otherwise,
        rather than raising, because both outcomes are ordinary and the caller
        has to record either one. The ``index`` on a refusal is a placeholder the
        caller replaces -- this function does not know which rung it is.

        ``quotes`` is passed in rather than re-read. This runs at most twice per
        rung -- once at step 5 and again if a fill changed the size during the
        cancellation -- and the two calls must be priced against the *same* book,
        or the pair of verdicts describes two different markets.
        """
        try:
            candidate = reprice(
                intent, credit=credit, quantity=quantity, created_at=now
            )
        except EngineError as exc:
            why = (
                f"the structure is not valid at credit {credit} x{quantity}: "
                f"{getattr(exc, 'message', exc)}"
            )
            return WalkAttempt(
                0, credit, quantity, False,
                refusal=WalkRefusal.STRUCTURE_INVALID, detail=why,
            )

        try:
            verdict = self.reverifier.reverify(candidate, quotes=quotes, now=now)
        except Exception as exc:  # noqa: BLE001 - a broken reverifier refuses
            return WalkAttempt(
                0, credit, quantity, False,
                refusal=WalkRefusal.RISK_AT_NEW_CREDIT,
                detail=f"the reverification raised {type(exc).__name__}: {exc}",
            )
        if not verdict.approved:
            return WalkAttempt(
                0, credit, quantity, False,
                refusal=WalkRefusal.RISK_AT_NEW_CREDIT,
                detail=(
                    f"credit {credit} raises the risk beyond what was approved: "
                    + "; ".join(verdict.refusals)
                ),
            )
        return candidate, verdict


def _with_index(attempt: WalkAttempt, index: int) -> WalkAttempt:
    """Stamp the rung number onto a refusal built without one."""
    return WalkAttempt(
        index=index,
        credit=attempt.credit,
        quantity=attempt.quantity,
        sent=attempt.sent,
        filled_here=attempt.filled_here,
        snapshot=attempt.snapshot,
        refusal=attempt.refusal,
        detail=attempt.detail,
    )
