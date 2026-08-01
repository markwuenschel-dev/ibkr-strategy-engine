"""One pass of the strategy: reconcile, manage what is open, then look for one entry.

This is the loop that makes the engine a strategy rather than a collection of
checks. It is deliberately a **single pass** with an explicit ``now`` rather than
a ``while True`` with a ``sleep``: a function that does one pass and returns a
report can be tested, run from cron, run by hand, or wrapped in a scheduler
without any of those needing to know how the others work.

Three orderings here are load-bearing.

**Reconcile first, and let anything short of agreement stop entries but not
exits.** Reconciliation ends on one of four named outcomes -- RECONCILED,
DISAGREEMENT, UNAVAILABLE, CORRUPT -- and only the first opens new risk. There
is deliberately no fifth "we did not get round to asking" state: an unanswered
question used to leave the result absent, and the entry gate read that absence
as permission, so a broker that threw authorised exactly what a broker reporting
an empty account would have.

Managing existing positions is refused by none of the four, and must not be: the
reason the book is not understood might be precisely a position that needs
closing, and a reconciler that locks the exits would turn a bookkeeping problem
into a market problem.

**Manage before entering.** Closing frees buying power, and the governor sizes
the entry against what is actually reserved. Entering first would measure the
book in a state that is about to change and could refuse an entry that a
completed exit would have made room for -- or, worse, allow one it would not.

**Record the intent before transmitting, confirm after.** A crash between the
two leaves an ``OPENING`` record the reconciler resolves. The reverse order
leaves a live spread nothing knows about. See :mod:`engine.options.positions`.

Nothing here calls ``placeOrder``. Every transmission goes through
:func:`engine.options.transmit.place_combo`, which requires an authorization
token this module obtains from the real gates or not at all.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from enum import Enum
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable
from uuid import uuid4

from ..errors import EngineError, RefusedError
from ..journal import OrderJournal
from ..safety import SafetyGate
from .adapters import IBKRWhatIfAdapter, read_open_orders
from .chain import (
    QualifiedOption,
    discover_expirations,
    enumerate_strikes,
    narrow_strikes,
    qualify_strikes,
    select_expiration,
)
from .domain import OptionStrategyIntent
from .governor import GovernorVerdict, PortfolioGovernor
from .ivrank import IVRankMetric, build_iv_rank, observations_from_bars
from .lifecycle import (
    ManagementAction,
    ManagementDecision,
    PositionMark,
    closing_intent_for,
    decide_management_action,
)
from .marketdata import Liveness
from .marking import closing_midpoint_debit, confirmed_remaining_quantity
from .policy import RiskPolicy
from .portfolio import PortfolioSnapshot
from .ports import LiveMarketDataPort, PortfolioStatePort, StrategyQuoteSnapshot
from .positions import (
    OpenPosition,
    PositionStore,
    ReconciliationOutcome,
    ReconciliationReport,
)
from .proof import envelope_for
from .reprice import DEFAULT_LADDER, RepriceLadder, RepriceOutcome, work_order
from .risk import CandidateRiskAssessment, assess_candidate
from .sink import LifecycleRecorder
from .selection import (
    Bias,
    build_vertical,
    candidates_from_snapshot,
    rights_for,
    select_short_strike,
    select_vertical,
    target_delta_for,
)
from .approval import (
    ApprovalContext,
    AwaitingVerification,
    VerificationState,
    VerifierGate,
    packet_for,
)
from .regime import (
    REGIME_MODE_LIVE,
    RegimeDecision,
    VolatilityAssessment,
    VolatilityRegimePolicy,
    classify,
    regime_mode,
)
from .execution import COMBO_ORDER_TYPE, COMBO_TIME_IN_FORCE
from .transmit import (
    TransmitResult,
    authorize_close,
    authorize_open,
    place_combo,
    structure_digest,
)

__all__ = [
    "RunReport",
    "run_once",
    "mark_from_snapshot",
    "CONFIG_VERSION",
    "EntryPreflight",
    "EntryPricing",
]

class EntryPricing(str, Enum):
    """Where on the book an opening credit is asked for.

    MIDPOINT is fair value and the strategy's default. NATURAL is what the book
    pays now, and is for a bounded execution experiment that wants a fill rather
    than a good price.
    """

    MIDPOINT = "MIDPOINT"
    NATURAL = "NATURAL"


CONFIG_VERSION = "options-runner/1"

ZERO = Decimal("0")

#: A last check between "every gate approved" and "mint the authorization".
#:
#: It exists so a *bounded* caller -- currently only the execution proof -- can
#: add constraints the strategy policy has no field for, without either editing
#: the policy or reimplementing the pipeline. Returning a string refuses the
#: entry as an ordinary blocker; returning ``None`` allows it.
#:
#: Deliberately cannot *widen* anything. It runs after every risk and governor
#: check has already passed, so the only thing a preflight can do is refuse an
#: entry the engine was otherwise willing to make.
EntryPreflight = Callable[..., "str | None"]


@dataclass
class RunReport:
    """Everything one pass did, and everything it refused to do."""

    started_at: dt.datetime
    armed: bool
    symbol: str
    finished_at: dt.datetime | None = None
    reconciliation: ReconciliationReport | None = None
    #: Defaults to UNAVAILABLE, not to a permissive value. A report that has not
    #: reached the reconciler yet has not established anything, and the entry
    #: gate reads this field alone -- so the fail-closed default is what makes
    #: "the pass died before reconciling" refuse instead of authorise.
    reconciliation_outcome: ReconciliationOutcome = ReconciliationOutcome.UNAVAILABLE
    iv_rank: IVRankMetric | None = None
    #: The volatility-regime classification for this pass, always computed
    #: (cheap and pure once the IV metric exists). Whether it *gates* is the
    #: runner's ``regime_live`` decision; in shadow it is a recorded opinion.
    regime: RegimeDecision | None = None
    decisions: list[ManagementDecision] = field(default_factory=list)
    transmissions: list[TransmitResult] = field(default_factory=list)
    candidate: OptionStrategyIntent | None = None
    risk: CandidateRiskAssessment | None = None
    governor: GovernorVerdict | None = None
    portfolio: PortfolioSnapshot | None = None
    #: What the cancel/replace ladder did with an entry that would not fill.
    #: Absent on every pass where the order resolved on its own.
    reprice: RepriceOutcome | None = None
    entered: bool = False
    #: Where the proposed entry stands with the independent reviewer. Defaults
    #: to PROPOSED rather than to anything permissive, and is only ever moved to
    #: CONSUMED by an authorization that actually passed the gate.
    verification: VerificationState = VerificationState.PROPOSED
    #: The collab-kit handoff id the reviewer was asked on, when there is one.
    #: Carried so an operator can find the request without grepping the tree.
    verification_request: str = ""
    blockers: list[str] = field(default_factory=list)
    refusal_codes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [
            f"symbol           {self.symbol}",
            f"armed            {'YES' if self.armed else 'NO (dry run)'}",
            "",
            f"RECONCILIATION   {self.reconciliation_outcome.value}",
            self.reconciliation.describe()
            if self.reconciliation
            else "  the broker was not successfully asked; nothing was compared",
            "",
            f"MANAGEMENT       {len(self.decisions)} open position(s) evaluated",
        ]
        for decision in self.decisions:
            lines.append(
                f"  {decision.action.value:<20} [{decision.reason_code}] {decision.detail}"
            )
        if self.regime is not None:
            lines.append("")
            lines.append(f"VOLATILITY REGIME  {self.regime.describe()}")
        lines.append("")
        lines.append("ENTRY")
        if self.candidate is not None:
            lines.append(f"  {self.candidate.describe()}")
        else:
            lines.append("  no candidate built")
        lines.append("")
        lines.append(self.risk.describe() if self.risk else "CANDIDATE RISK   not evaluated")
        lines.append("")
        lines.append(
            self.governor.describe() if self.governor else "PORTFOLIO GOVERNOR   not evaluated"
        )
        lines.append("")
        lines.append(f"TRANSMITTED      {len(self.transmissions)} order(s)")
        for result in self.transmissions:
            lines.append(f"  {result.describe()}")
        if self.reprice is not None:
            lines.append(f"  worked           {self.reprice.describe()}")
        lines.append("")
        lines.append(f"ENTERED          {'YES' if self.entered else 'NO'}")
        for blocker in self.blockers:
            lines.append(f"  blocked by      {blocker}")
        for code in self.refusal_codes:
            lines.append(f"  refusal code    {code}")
        if self.errors:
            lines.append("")
            lines.append("ERRORS")
            lines.extend(f"  {e}" for e in self.errors)
        return "\n".join(lines)

    def to_record(self) -> dict[str, Any]:
        return {
            "event": "options_run",
            "symbol": self.symbol,
            "armed": self.armed,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "reconciliation": self.reconciliation.to_record() if self.reconciliation else None,
            "reconciliation_outcome": self.reconciliation_outcome.value,
            "iv_rank": self.iv_rank.to_record() if self.iv_rank else None,
            "regime": self.regime.to_record() if self.regime else None,
            "decisions": [d.to_record() for d in self.decisions],
            "transmissions": [t.to_record() for t in self.transmissions],
            "candidate": self.candidate.describe() if self.candidate else None,
            "risk": self.risk.to_record() if self.risk else None,
            "governor": self.governor.to_record() if self.governor else None,
            "reprice": self.reprice.to_record() if self.reprice else None,
            "entered": self.entered,
            "blockers": list(self.blockers),
            "refusal_codes": list(self.refusal_codes),
            "errors": list(self.errors),
        }


def mark_from_snapshot(
    position: OpenPosition, snapshot: StrategyQuoteSnapshot | None
) -> PositionMark | None:
    """What it would cost per share to buy this structure back right now.

    Built from mids, per share, to match :attr:`OpenPosition.filled_credit`'s
    unit. Buying back the legs that were sold costs their mid; selling the legs
    that were bought returns theirs -- so the net is short-mids minus long-mids,
    using the **opening** actions.

    ``is_live`` is set from the provenance the snapshot carries rather than
    assumed. A mark whose liveness could not be established still gets returned,
    because the lifecycle rules want to distinguish "no mark at all" from "a mark
    we may not act on" -- and only the second tells the operator the feed is up
    but unentitled.

    **The arithmetic is not repeated here.** It is
    :func:`engine.options.marking.closing_midpoint_debit`, and delegating rather
    than re-deriving is the same discipline
    :func:`engine.options.pricing.midpoint_credit` follows when it defers to
    ``proof.observed_credit``: two implementations of "what is this structure
    worth" can disagree while both look right in isolation, and the one that is
    wrong is whichever one an exit is priced against.

    Note this is the **midpoint**, which is a valuation and not an exit price.
    :func:`engine.options.marking.closing_natural_debit` is what a close can
    actually be done at, and it is what the marking report prices a proposal on.
    """
    if snapshot is None:
        return None

    total = closing_midpoint_debit(position.legs, snapshot)
    if total is None:
        return None

    if total < ZERO:
        # A negative buy-back price is a sign error or a crossed book, not a
        # free position. Refusing is safer than banking an imaginary profit.
        return None

    livenesses = {quote.provenance.liveness for quote in snapshot.legs}
    livenesses.add(snapshot.underlying.provenance.liveness)

    latest = max(
        (
            q.provenance.last_provider_event_at
            for q in snapshot.legs
            if q.provenance.last_provider_event_at is not None
        ),
        default=None,
    )
    if latest is None:
        return None

    return PositionMark(
        debit_to_close=total,
        as_of=latest,
        is_live=livenesses == {Liveness.LIVE},
    )


def _quotes_for(
    market_data: LiveMarketDataPort | None,
    *,
    symbol: str,
    con_ids: list[int],
    report: RunReport,
    label: str,
    require_two_sided: bool = False,
) -> StrategyQuoteSnapshot | None:
    """One snapshot, or ``None`` with the failure recorded.

    ``require_two_sided`` is passed through only when set, so ports that
    predate the parameter (every test fake, and any adapter not yet updated)
    keep working on the default path -- and a port that cannot honour the
    request fails loudly here rather than silently ignoring it.
    """
    if market_data is None or not con_ids:
        return None
    try:
        if require_two_sided:
            return market_data.strategy_quotes(
                underlying_symbol=symbol, con_ids=con_ids, require_two_sided=True
            )
        return market_data.strategy_quotes(underlying_symbol=symbol, con_ids=con_ids)
    except Exception as exc:  # noqa: BLE001 - adapter boundary; see scan.py
        report.errors.append(f"{label}: quotes unavailable: {type(exc).__name__}: {exc}")
        return None


def _manage_one(
    position: OpenPosition,
    *,
    ib: Any,
    gate: SafetyGate,
    store: PositionStore,
    recorder: LifecycleRecorder,
    journal: OrderJournal,
    policy: RiskPolicy,
    market_data: LiveMarketDataPort | None,
    armed: bool,
    now: dt.datetime,
    today: dt.date,
    account: str,
    configuration_version: str,
    report: RunReport,
) -> None:
    """Decide and, if the decision acts, send the exit."""
    # Two-sided demanded for the held position's own legs: a one-sided
    # snapshot here is why in-pass marking failed 7 of 10 passes on
    # 2026-07-31, and at <=21 DTE it turns the calendar exit into a per-pass
    # coin flip. The set is small (the structure's legs, not a chain window),
    # so the wait is bounded by contracts that are actually held.
    snapshot = _quotes_for(
        market_data,
        symbol=position.underlying,
        con_ids=[leg.con_id for leg in position.legs],
        report=report,
        label=f"manage {position.strategy_id}",
        require_two_sided=True,
    )
    mark = mark_from_snapshot(position, snapshot)
    decision = decide_management_action(
        position,
        policy=policy,
        mark=mark,
        now=now,
        today=today,
    )
    report.decisions.append(decision)

    if decision.action is ManagementAction.HOLD:
        return

    # Only the profit-target rule computes a limit; a defensive exit is priced
    # against the book at the time it is sent. So a DTE exit needs the current
    # mark, and without one there is no price to put on the order.
    #
    # Sending it as a market order instead is the obvious shortcut and is
    # rejected: combo markets are wide, and a market order on a spread nobody
    # can price is how a defensive exit turns into the worst fill of the day.
    # Refusing is honest -- the position stays open, loudly, with a blocker
    # naming the reason, which is a problem an operator can act on.
    limit_price = decision.target_debit
    if limit_price is None:
        if mark is None:
            report.blockers.append(
                f"{decision.action.value} is due for {position.strategy_id} but there "
                "is no mark to price the exit against; refusing to send an unpriced "
                "combo order"
            )
            return
        limit_price = mark.debit_to_close

    # A roll is a close plus a freshly validated open plus a link record. Only
    # the close half is built here; the open half re-enters through the ordinary
    # entry path on a later pass, so it is gated exactly like any other entry
    # rather than inheriting the closing order's lighter authorization.
    # Sized to what actually filled, never to what was ordered. A position that
    # partially filled holds fewer contracts than its intent says, and closing
    # the intent quantity sells contracts that were never bought -- turning a
    # defensive exit into an opening naked short, at the worst possible moment.
    closing = closing_intent_for(
        decision,
        position,
        strategy_id=uuid4(),
        created_at=now,
        configuration_version=configuration_version,
        limit_price=limit_price,
        # What the position is PROVEN to still hold, not what it once filled.
        #
        # ``manageable_quantity`` does not subtract a partial close. The
        # lifecycle holds a position while it is CLOSING, so a second close
        # cannot double the order -- but a CLOSE_FAILED after a partial fill
        # returns the position to OPEN, and it is re-decided with the full
        # original fill. Three filled, two already closed, and the exit is
        # sized three: two contracts sold that nobody holds.
        #
        # This is ledger C21 reached by a different road, and the reason the
        # number is trustworthy now is the C24 replay fix -- before it,
        # ``close_filled_quantity`` was silently reset to zero by any
        # transition that did not name it.
        quantity=confirmed_remaining_quantity(position),
    )

    try:
        authorization = authorize_close(closing, gate=gate, armed=armed, now=now)
    except RefusedError as exc:
        report.blockers.append(f"exit refused for {position.strategy_id}: {exc.message}")
        return

    store.record_close_submitted(
        position.strategy_id, at=now, target_debit=decision.target_debit
    )
    try:
        result = place_combo(
            ib, closing, authorization=authorization, account=account, sink=recorder
        )
    except Exception as exc:  # noqa: BLE001 - a failed send must be recorded, not raised
        store.record_close_failed(position.strategy_id, at=now, reason=str(exc))
        report.errors.append(f"close failed for {position.strategy_id}: {exc}")
        return

    report.transmissions.append(result)
    journal.record("order_placed", **result.to_record())

    # Persistence already happened, inside the polling loop, via the sink. What
    # is left here is reporting -- an unresolved exit is the more dangerous of
    # the two uncertainties, because the position may or may not still be held,
    # and the operator needs that said out loud rather than inferred from a log.
    if result.is_uncertain:
        report.blockers.append(
            f"the exit for {position.strategy_id} did not resolve "
            f"({result.state.value}); it must be reconciled before another is sent"
        )
    elif result.has_position and not result.is_filled:
        report.blockers.append(
            f"the exit for {position.strategy_id} filled only partially "
            f"({result.filled}); the remainder is still held"
        )


def _wall_clock() -> dt.datetime:
    """Real elapsed time, for the one bound that is about real elapsed time.

    Everything else in a pass is timestamped from the injected ``now``, which
    is a *logical* clock a caller may set to any instant. The reprice ladder's
    time-to-live is not a logical duration -- it is two minutes of an order
    resting in a live book -- so it is measured here and compared against
    itself.
    """
    return dt.datetime.now(dt.timezone.utc)


def _still_working(result: TransmitResult) -> bool:
    """Whether there is a live order left to work.

    Exactly the three states in which the broker has told us it holds the order
    and may still fill it. This is the state the live run ended in: ``Submitted``
    -> ACKNOWLEDGED, working, unfilled, and nothing able to retract it.

    ``TIMED_OUT`` and ``UNKNOWN`` are deliberately excluded even though both
    mean "not finished". Neither is a statement about the order -- one says we
    stopped waiting and the other says the socket dropped -- and in both cases
    the engine has never heard a status at all. Cancelling on that basis is not
    order management, it is guessing at a broker we have just concluded we
    cannot hear from. Those two stay where they were: recorded as uncertain,
    with entries blocked until reconciliation resolves them.

    A **partial fill** is excluded too, and that exclusion is load-bearing.
    ``PARTIALLY_FILLED`` is a working state, but contracts are already in the
    book: cancelling the remainder and replacing it at a lower credit would
    open a second position on top of a real one, at a price the first half was
    never sized against. A partial is a position to manage, not an order to
    work.
    """
    snapshot = result.snapshot
    if snapshot is None or result.trade is None:
        return False
    return snapshot.is_working and not snapshot.has_position


def _record_open_outcome(
    strategy_id: Any,
    result: TransmitResult,
    *,
    store: PositionStore,
    now: dt.datetime,
    report: RunReport,
) -> None:
    """Report what the broker did with an opening order.

    **Persistence already happened.** Every transition, including this final one,
    was written by the lifecycle sink from inside the polling loop as it
    occurred. This function's job is now only to set the report's flags and
    blockers -- writing here as well would append a duplicate of the last
    transition, which the sink's idempotency would mostly absorb but which would
    make the event log a less honest record of what was actually observed.

    The one exception is the filled-without-a-price case, which the sink cannot
    resolve on its own: it durably records the *quantity*, and only the runner
    knows that an unknown entry credit is a reason to stop opening new risk.
    """
    snapshot = result.snapshot

    if result.is_uncertain:
        reason = result.message or f"no resolution before timeout ({result.state.value})"
        report.blockers.append(
            f"the outcome of this order is unknown ({reason}); new entries are "
            "blocked until it is reconciled"
        )
        return

    if snapshot is not None and snapshot.has_position:
        credit = abs(snapshot.average_price) if snapshot.average_price else None

        if credit is None:
            # The contracts are durable already -- the sink wrote the quantity.
            # What is missing is the price, and inventing one from the limit
            # would put a number the broker never confirmed into every
            # downstream profit-target calculation. Marked uncertain so the
            # credit is reconciled before anything else opens.
            store.record_uncertain(
                strategy_id,
                at=now,
                reason=(
                    f"filled ({snapshot.filled}) but the broker reported no usable "
                    "fill price; the position exists and its entry credit is unknown"
                ),
                order_id=result.order_id,
                perm_id=result.perm_id,
            )
            report.blockers.append(
                "an order filled without a usable price; the position is real and "
                "its credit must be reconciled before new entries"
            )
            return

        report.entered = True
        if not result.is_filled:
            report.blockers.append(
                f"partial fill: {snapshot.filled} filled "
                f"({result.state.value}); the position is real and smaller than "
                "intended, and its exit will be sized to what filled"
            )
        return

    report.blockers.append(f"order did not fill: {result.state.value}")


#: What to tell the operator for each outcome that refuses an entry. Keyed by
#: outcome so the message names which half of the system to go and look at --
#: "disagree" means the broker, "replayed" means the store on disk.
_RECONCILIATION_BLOCKERS: dict[ReconciliationOutcome, str] = {
    ReconciliationOutcome.DISAGREEMENT: (
        "the position store and the broker disagree; refusing to open new risk "
        "until the book is understood"
    ),
    ReconciliationOutcome.UNAVAILABLE: (
        "the broker could not be asked what it holds, so the book is unverified; "
        "refusing to open new risk. An unanswered question is not an answer of no"
    ),
    ReconciliationOutcome.CORRUPT: (
        "the position store could not be replayed cleanly, so the book is only "
        "partly readable; refusing to open new risk"
    ),
}


def _open_orders(ib: Any, *, report: RunReport) -> Any:
    """:func:`~engine.options.adapters.read_open_orders`, with the failure named.

    A broker that raises is a broker that could not be asked, which is ``None``
    -- never an empty list. The distinction is the whole point: see the
    reconciler's ``broker_orders`` contract.
    """
    try:
        return read_open_orders(ib)
    except Exception as exc:  # noqa: BLE001 - adapter boundary
        report.errors.append(
            f"the broker's open orders could not be read: {type(exc).__name__}: {exc}"
        )
        return None


def _reconcile(
    broker: Any, store: PositionStore, *, now: dt.datetime, report: RunReport
) -> None:
    """Establish, explicitly, whether the book is understood.

    Sets ``report.reconciliation_outcome`` on **every** path, including the
    failing ones. That is the entire point of this function. The previous
    version wrapped the reconciler in a bare ``try`` and, when the broker threw,
    appended a line to ``report.errors`` and left ``report.reconciliation`` as
    ``None`` -- and the entry gate then asked "is there a report that disagrees?"
    which ``None`` answers with no. A broker that could not be asked authorised
    an entry, so a restart mid-position re-sent the same spread.

    The store is checked **before** the broker. If the log on disk cannot be
    replayed there is nothing trustworthy to compare against, and reporting that
    as CORRUPT sends the operator to the store rather than to TWS.
    """
    try:
        integrity = store.integrity_errors()
    except Exception as exc:  # noqa: BLE001 - an unreadable store refuses, it does not crash
        report.errors.append(
            f"the position store could not be read: {type(exc).__name__}: {exc}"
        )
        report.reconciliation_outcome = ReconciliationOutcome.CORRUPT
        return
    if integrity:
        report.errors.append(
            f"the position store could not be replayed cleanly: {list(integrity)}"
        )
        report.reconciliation_outcome = ReconciliationOutcome.CORRUPT
        return

    holdings = getattr(broker, "positions", None)
    if not callable(holdings):
        report.errors.append(
            "this broker cannot report positions, so the book was never compared"
        )
        report.reconciliation_outcome = ReconciliationOutcome.UNAVAILABLE
        return

    try:
        reported = holdings()
    except Exception as exc:  # noqa: BLE001 - adapter boundary
        report.errors.append(f"reconciliation failed: {type(exc).__name__}: {exc}")
        report.reconciliation_outcome = ReconciliationOutcome.UNAVAILABLE
        return

    if reported is None:
        # Not a flat account. A flat account answers with an empty sequence;
        # ``None`` is what a failed or disconnected query returns, and letting
        # the reconciler read it as "you hold nothing" is the same absence-as-
        # permission mistake one layer down -- it would agree with an empty
        # store and authorise an entry against a book nobody checked.
        report.errors.append(
            "the broker returned no position data, so the book was not compared"
        )
        report.reconciliation_outcome = ReconciliationOutcome.UNAVAILABLE
        return

    # A working order is not a position, so ``positions()`` cannot see it -- and
    # for as long as this was the only question asked, a combo the broker was
    # demonstrably working was reported as one the broker was not working. The
    # order-level query is what makes the report's claim true.
    try:
        result = store.reconcile_against_broker(
            reported,
            checked_at=now,
            broker_orders=_open_orders(getattr(broker, "ib", broker), report=report),
        )
    except Exception as exc:  # noqa: BLE001 - adapter boundary
        report.errors.append(f"reconciliation failed: {type(exc).__name__}: {exc}")
        report.reconciliation_outcome = ReconciliationOutcome.UNAVAILABLE
        return

    report.reconciliation = result
    report.reconciliation_outcome = ReconciliationOutcome.for_report(result)


def _underlying_reference_price(broker: Any, symbol: str) -> Decimal | None:
    """Spot, used only to centre the strike window.

    Deliberately forgiving: a broker without a ``quote`` method, a refused
    subscription, or a quote carrying no price all mean the same thing to the
    caller -- no reference -- and none of them should abort a pass, because the
    window has a (worse) fallback. What must not happen is this failing
    *silently*, so the caller records it rather than this returning a guess.
    """
    quote = getattr(broker, "quote", None)
    if not callable(quote):
        return None
    try:
        price = getattr(quote(symbol), "price", None)
    except Exception:  # noqa: BLE001 - a reference price is an optimisation
        return None
    if price is None:
        return None
    try:
        value = Decimal(str(price))
    except (ArithmeticError, ValueError):
        return None
    return value if value.is_finite() and value > ZERO else None


def _qualify_underlying(ib: Any, symbol: str, report: RunReport) -> Any | None:
    """Qualify the stock, or record the blocker and return ``None``."""
    from ib_async import Stock  # noqa: PLC0415 - optional dependency

    qualified = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
    if not qualified:
        report.blockers.append(f"IBKR did not qualify the underlying {symbol}")
        return None
    return qualified[0]


def _iv_metric_for(
    ib: Any, underlying: Any, symbol: str, *, now: dt.datetime
) -> IVRankMetric:
    """One year of daily implied volatility, reduced to the rank metric."""
    bars = ib.reqHistoricalData(
        underlying,
        endDateTime="",
        durationStr="1 Y",
        barSizeSetting="1 day",
        whatToShow="OPTION_IMPLIED_VOLATILITY",
        useRTH=True,
        formatDate=1,
    )
    return build_iv_rank(symbol, observations_from_bars(bars or []), calculated_at=now)


def _build_candidate(
    *,
    ib: Any,
    broker: Any,
    symbol: str,
    bias: Bias,
    policy: RiskPolicy,
    market_data: LiveMarketDataPort | None,
    now: dt.datetime,
    today: dt.date,
    target_dte: int,
    minimum_dte: int,
    maximum_dte: int,
    strike_window: int,
    configuration_version: str,
    entry_pricing: EntryPricing = EntryPricing.MIDPOINT,
    report: RunReport,
    underlying: Any | None = None,
    iv_metric: IVRankMetric | None = None,
) -> tuple[OptionStrategyIntent | None, StrategyQuoteSnapshot | None]:
    """Chain -> quotes -> delta selection -> a validated opening intent.

    ``underlying`` and ``iv_metric`` are accepted pre-computed because the
    regime gate needs the IV metric *before* the build (the allocation
    multiplier scales the sizing budget the build uses). When absent, both are
    derived here exactly as before, so a caller that has not adopted the
    regime path gets the previous behaviour byte for byte.
    """
    if underlying is None:
        underlying = _qualify_underlying(ib, symbol, report)
        if underlying is None:
            return None, None

    if iv_metric is None:
        iv_metric = _iv_metric_for(ib, underlying, symbol, now=now)
    report.iv_rank = iv_metric

    expiry = select_expiration(
        discover_expirations(ib, symbol, underlying.conId),
        today=today,
        target_dte=target_dte,
        minimum_dte=minimum_dte,
        maximum_dte=maximum_dte,
    )
    if expiry is None:
        report.blockers.append(f"no expiration between {minimum_dte} and {maximum_dte} DTE")
        return None, None

    right = rights_for(bias)[0]
    listed = enumerate_strikes(ib, symbol, expiry.expiry, right.value)
    # The window must be centred on spot, not on the middle of the listed
    # ladder. Those coincide only by accident, and when they diverge the engine
    # subscribes to a band of strikes that has no relationship to the ones it
    # intends to sell. Only narrowing depends on this price; selection stays
    # delta-based, so an approximate figure is fine and a missing one is
    # survivable -- but it is recorded, because a positional window is a
    # degraded window and a silent fallback would hide that.
    reference_price = _underlying_reference_price(broker, symbol)
    if reference_price is None:
        report.errors.append(
            f"no reference price for {symbol}; the strike window fell back to the "
            "middle of the listed ladder, which need not be near spot"
        )
    window = narrow_strikes(
        listed,
        reference_price=reference_price,
        width=strike_window,
        right=right.value,
    )
    contracts: list[QualifiedOption] = list(
        qualify_strikes(ib, symbol, expiry.expiry, window, right.value)
    )
    if not contracts:
        report.blockers.append("no contracts qualified for the chosen expiration")
        return None, None

    snapshot = _quotes_for(
        market_data,
        symbol=symbol,
        con_ids=[c.con_id for c in contracts],
        report=report,
        label="entry",
    )
    if snapshot is None:
        report.blockers.append(
            "no live quote snapshot for the chain, so strikes cannot be delta-selected"
        )
        return None, None

    universe = candidates_from_snapshot(contracts, snapshot)
    selection = select_vertical(
        universe,
        target_delta=target_delta_for(bias, policy),
        right=right,
        target_width=policy.target_width,
    )
    if selection is None:
        # Two very different market conditions reach this branch, and a single
        # message for both is the difference between "the feed is broken" and
        # "this expiry cannot build the structure". Re-running the first half of
        # the selection is cheap and pure, and it names which one happened.
        short = select_short_strike(
            universe, target_delta=target_delta_for(bias, policy), right=right
        )
        if short is None:
            report.blockers.append(
                f"no {right.value} strike in the window carries a usable delta "
                f"({len(universe)} contracts considered)"
            )
        else:
            report.blockers.append(
                f"no protective strike within reach of the {short.contract.strike} "
                f"short: the chain lists nothing between it and "
                f"{policy.target_width} away, so the only wing available would "
                "risk far more than the target width"
            )
        return None, snapshot

    # Price the structure from the book rather than a fraction of the width.
    #
    # MIDPOINT is what the strategy uses: it is the fair value of the structure
    # and the price worth asking for when there is time to wait. NATURAL is what
    # the book will pay *right now* -- each short sold at its bid, each long
    # bought at its ask -- and it exists because the engine's first real order
    # asked the midpoint (0.20) against a natural of 0.18 and rested unfilled for
    # 160 minutes. Two cents, and it never traded.
    #
    # The choice belongs to the caller, not to this function: a strategy entry
    # and a bounded execution experiment want opposite answers, and hard-coding
    # either one makes the other unreachable.
    quotes = {q.con_id: q for q in snapshot.legs}
    short_quote = quotes.get(selection.short.con_id)
    long_quote = quotes.get(selection.long.con_id)
    if entry_pricing is EntryPricing.NATURAL:
        short_side = short_quote.bid if short_quote else None
        long_side = long_quote.ask if long_quote else None
    else:
        short_side = short_quote.mid if short_quote else None
        long_side = long_quote.mid if long_quote else None
    if short_side is None or long_side is None:
        report.blockers.append(
            "no two-sided market on the selected strikes"
            if entry_pricing is EntryPricing.MIDPOINT
            else "no bid on the short leg or no ask on the long leg, so the "
            "natural price cannot be computed"
        )
        return None, snapshot
    credit = (short_side - long_side).quantize(Decimal("0.01"))
    if credit <= ZERO or credit >= selection.width:
        report.blockers.append(
            f"credit {credit} is not a usable price for a {selection.width}-wide spread"
        )
        return None, snapshot

    intent = build_vertical(
        selection,
        credit=credit,
        policy=policy,
        configuration_version=configuration_version,
        created_at=now,
    )
    if intent is None:
        report.blockers.append(
            f"risk budget {policy.risk_budget_per_position} does not cover one contract"
        )
    return intent, snapshot


def _entry_evidence(
    report: "RunReport",
    *,
    margin: Any,
    snapshot: Any,
    intent: OptionStrategyIntent | None = None,
    minimum_iv_rank: Decimal | None = None,
) -> dict[str, Any]:
    """The section 3.1 evidence the reviewer needs and the digest cannot bind.

    Every field is pulled from what this pass actually observed rather than
    recomputed, so a reviewer recomputing from it is checking the engine's work
    instead of repeating it. Anything genuinely unavailable is left out entirely
    -- :meth:`VerificationPacket.render` renders an absent field as ``MISSING``,
    which is legible to the reviewer as grounds for UNAVAILABLE. Substituting a
    plausible default here would be the engine answering a question that was
    asked of the reviewer.

    ``intent`` narrows the per-leg fields to the structure actually proposed --
    the same narrowing :func:`engine.options.risk.execution_entitlement_set`
    applies, and for the same reason: the reviewer is judging this order, not
    every strike the scan happened to inspect. ``minimum_iv_rank`` is stated in
    the filter field because "ENFORCED" without a threshold reads as the
    default, and a proof run at a deliberately lower threshold that does not
    say so is asking the reviewer to approve under a false label -- the exact
    omission the 20260731T133434Z UNAVAILABLE named.
    """

    def observed(source: Any, check: str) -> str | None:
        if source is None:
            return None
        try:
            result = source.result_for(check)
        except (KeyError, AttributeError, ValueError):
            return None
        return None if result.observed is None else str(result.observed)

    def event_time(provenance: Any) -> str:
        at = getattr(provenance, "last_provider_event_at", None)
        return at.isoformat() if at is not None else "no provider event"

    evidence: dict[str, Any] = {
        "iv_rank": (
            str(report.iv_rank.iv_rank)
            if report.iv_rank is not None and report.iv_rank.iv_rank is not None
            else None
        ),
        "iv_rank_filter": (
            "BYPASSED"
            if "OPTIONS_IV_RANK_FILTER_BYPASSED" in report.refusal_codes
            else "ENFORCED"
            if minimum_iv_rank is None
            else f"ENFORCED at minimum {minimum_iv_rank}"
        ),
        "defined_max_loss": observed(report.risk, "defined_loss"),
        "stress_loss": observed(report.risk, "stress_loss"),
        "broker_what_if_margin": (
            str(margin.initial_margin_change)
            if margin is not None and margin.initial_margin_change is not None
            else None
        ),
        "portfolio_exposure_before": (
            str(snapshot_total(report.portfolio)) if report.portfolio else None
        ),
        "pending_reservations": (
            None
            if report.portfolio is None
            else f"reported {report.portfolio.reported_buying_power_reserved}"
            if report.portfolio.reported_buying_power_reserved is not None
            else f"derived {report.portfolio.total_buying_power_reserved}"
            if getattr(report.portfolio, "total_buying_power_reserved", None)
            is not None
            else None
        ),
        "sector_impact": observed(report.governor, "sector_concentration"),
        "correlation_impact": observed(report.governor, "correlation_concentration"),
        "portfolio_exposure_after": observed(report.governor, "total_bpr"),
        "volatility_regime": (
            report.regime.regime.value if report.regime is not None else None
        ),
        "allocation_multiplier": (
            str(report.regime.allocation) if report.regime is not None else None
        ),
        "regime_reasons": (
            " | ".join(report.regime.reasons)
            if report.regime is not None and report.regime.reasons
            else None
        ),
    }
    if snapshot is not None:
        under = getattr(snapshot, "underlying", None)
        wanted = {leg.con_id for leg in intent.legs} if intent is not None else None
        legs = [
            leg
            for leg in tuple(getattr(snapshot, "legs", ()) or ())
            if wanted is None or leg.con_id in wanted
        ]
        provenances: list[str] = []
        quote_times: list[str] = []
        if under is not None:
            provenances.append(f"underlying {under.provenance.describe()}")
            quote_times.append(f"underlying {event_time(under.provenance)}")
        for leg in legs:
            provenances.append(f"{leg.con_id} {leg.provenance.describe()}")
            quote_times.append(f"{leg.con_id} {event_time(leg.provenance)}")
        evidence["market_data_provenance"] = (
            "; ".join(provenances) if provenances else None
        )
        evidence["quote_timestamps"] = "; ".join(quote_times) if quote_times else None
        greek_times = [
            f"{leg.con_id} "
            + (
                f"delta {leg.greeks.delta} received {leg.greeks.received_at.isoformat()}"
                if leg.greeks is not None and leg.greeks.delta is not None
                else "no greeks reported"
            )
            for leg in legs
        ]
        evidence["greek_timestamps"] = "; ".join(greek_times) if greek_times else None
    return {name: value for name, value in evidence.items() if value is not None}


def snapshot_total(snapshot: Any) -> Any:
    return getattr(snapshot, "total_buying_power_reserved", None)


def run_once(
    broker: Any,
    *,
    gate: SafetyGate,
    journal: OrderJournal,
    store: PositionStore,
    policy: RiskPolicy,
    armed: bool = False,
    symbol: str = "SPY",
    bias: Bias = Bias.BULLISH,
    market_data: LiveMarketDataPort | None = None,
    portfolio: PortfolioStatePort | None = None,
    now: dt.datetime | None = None,
    today: dt.date | None = None,
    target_dte: int = 45,
    minimum_dte: int = 35,
    maximum_dte: int = 55,
    minimum_iv_rank: Decimal = Decimal("50"),
    regime_policy: VolatilityRegimePolicy | None = None,
    #: ``None`` reads IBKR_OPTIONS_REGIME_MODE (default shadow). Shadow keeps
    #: the ``minimum_iv_rank`` wall authoritative and records the regime
    #: decision beside it; live replaces the wall with the tiered gate and
    #: scales the sizing budget by the tier's allocation.
    regime_live: bool | None = None,
    strike_window: int = 24,
    entry_pricing: EntryPricing = EntryPricing.MIDPOINT,
    account: str = "",
    configuration_version: str = CONFIG_VERSION,
    enforce_iv_rank: bool = True,
    entry_preflight: EntryPreflight | None = None,
    sink: Any = None,
    reprice: RepriceLadder | None = DEFAULT_LADDER,
    verifier: VerifierGate | None = None,
    approval_context: ApprovalContext | None = None,
) -> RunReport:
    """Reconcile, manage every open position, then consider one new entry.

    Returns a report rather than raising, except for the kill switch -- a halted
    engine stops before it does anything at all, and that is the one condition
    that should reach the caller as an exception rather than a line in a report.

    The last four arguments exist for the execution proof and default to exactly
    what the strategy path has always done, so a caller that ignores them gets
    the previous behaviour byte for byte.

    ``enforce_iv_rank`` is the only one that can *loosen* anything, and it
    loosens precisely one thing: an opinion about whether now is a good moment
    to sell premium. It is not a safety property, and no other filter is
    reachable through it. Every gate that answers "is this order survivable"
    runs unconditionally below, whatever this is set to.

    ``entry_preflight`` runs after every risk and governor check has passed and
    before the authorization token is minted, and can only refuse. ``sink``
    replaces the default :class:`~engine.options.sink.LifecycleRecorder` -- a
    caller that supplies one is responsible for it still reaching the store,
    which is why the proof wraps rather than replaces the recorder.

    ``reprice`` bounds what happens to an entry the broker accepts and does not
    fill. It defaults to :data:`engine.options.reprice.DEFAULT_LADDER` because
    the alternative is what the first live order actually did: rest in the book
    unfilled, with the pass reporting it as unresolved and refusing every later
    entry, and nothing in the process able to clear it. Passing ``None``
    restores that behaviour exactly and is not recommended for an unattended
    run. The ladder can only lower the credit, only inside the envelope the
    risk gates approved, at most four times, and it ends by cancelling.
    """
    # A pinned clock (tests, replays) stays pinned through the binding
    # revalidation; a live run re-reads the wall clock there, because the
    # whole point of revalidating is "as of now", not "as of pass start".
    pinned_clock = now is not None
    now = now or dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    today = today or now.date()
    report = RunReport(started_at=now, armed=armed, symbol=symbol)
    ib = broker.ib

    gate.assert_not_halted()

    # Seeded from the store, so a recorder built after a restart already knows
    # what the previous process persisted and will not re-append transitions it
    # can already see on disk.
    recorder: Any = LifecycleRecorder(store) if sink is None else sink

    try:
        # -- 1. reconcile -------------------------------------------------
        _reconcile(broker, store, now=now, report=report)

        # -- 2. manage what is already open --------------------------------
        # Runs under EVERY reconciliation outcome, not just the good one: the
        # disagreement may be exactly the position that needs closing, and a
        # broker that could not answer a positions query can still accept a
        # closing order. Blocking exits here would turn a bookkeeping problem
        # into a market problem and trap the position.
        for position in store.open_positions():
            _manage_one(
                position,
                ib=ib,
                gate=gate,
                store=store,
                recorder=recorder,
                journal=journal,
                policy=policy,
                market_data=market_data,
                armed=armed,
                now=now,
                today=today,
                account=account,
                configuration_version=configuration_version,
                report=report,
            )

        # -- 3. entry ------------------------------------------------------
        # An order whose outcome is unknown may be resting in the book. Opening
        # another on top of it is how one intended position becomes two, so this
        # blocks entries -- and only entries. Management above has already run,
        # because reconciling and exiting are exactly what resolves the state.
        unresolved = [p for p in store.open_positions() if p.is_uncertain]
        if unresolved:
            report.blockers.append(
                f"{len(unresolved)} order(s) with an unresolved outcome "
                f"({[str(p.strategy_id) for p in unresolved]}); refusing to open "
                "new risk until they are reconciled"
            )
            report.refusal_codes.append("RUNNER_UNRESOLVED_ORDER")
            return report

        # Only RECONCILED authorises an entry. DISAGREEMENT, UNAVAILABLE and
        # CORRUPT all refuse -- and all three reach this line *after* management
        # has run, which is the asymmetry the module docstring describes.
        outcome = report.reconciliation_outcome
        if not outcome.may_open_new_risk:
            report.blockers.append(_RECONCILIATION_BLOCKERS[outcome])
            report.refusal_codes.append(f"RUNNER_RECONCILIATION_{outcome.value}")
            return report

        # -- 3. regime first, because its allocation scales the build --------
        # The IV metric is hoisted ahead of the candidate build so the tier
        # can be placed before sizing. Always classified, even in shadow: the
        # decision is recorded in the report, the journal and the packet
        # evidence either way, which is what makes the later live flip a
        # reviewed config change instead of a code change.
        live_regime = (
            regime_mode() == REGIME_MODE_LIVE if regime_live is None else regime_live
        )
        resolved_regime = regime_policy or VolatilityRegimePolicy.from_env()

        underlying_contract = _qualify_underlying(ib, symbol, report)
        if underlying_contract is None:
            return report
        iv_metric = _iv_metric_for(ib, underlying_contract, symbol, now=now)
        report.iv_rank = iv_metric
        report.regime = classify(
            VolatilityAssessment(
                symbol=symbol,
                iv_rank=iv_metric.iv_rank if iv_metric.is_usable else None,
                iv_percentile=iv_metric.iv_percentile,
                current_iv=iv_metric.current_iv,
            ),
            resolved_regime,
        )

        effective_policy = policy
        if live_regime:
            if not report.regime.permits_entry:
                report.blockers.append(
                    f"volatility regime: {report.regime.reasons[0]}"
                )
                report.refusal_codes.append(report.regime.refusal_code)
                return report
            effective_policy = dataclasses.replace(
                policy,
                risk_budget_per_position=(
                    policy.risk_budget_per_position * report.regime.allocation
                ),
            )

        candidate, snapshot = _build_candidate(
            ib=ib,
            broker=broker,
            symbol=symbol,
            bias=bias,
            policy=effective_policy,
            market_data=market_data,
            now=now,
            today=today,
            target_dte=target_dte,
            minimum_dte=minimum_dte,
            maximum_dte=maximum_dte,
            strike_window=strike_window,
            configuration_version=configuration_version,
            entry_pricing=entry_pricing,
            report=report,
            underlying=underlying_contract,
            iv_metric=iv_metric,
        )
        report.candidate = candidate
        if candidate is None:
            return report

        # The one filter a bounded caller may switch off, and only because it is
        # an opinion about timing rather than a statement about survivability.
        # The refusal code is still recorded when it is off, so a proof run's
        # journal says plainly that the filter would have refused. Authoritative
        # only while the regime gate is in shadow; live mode replaced it above.
        if not live_regime and (
            report.iv_rank is None or not report.iv_rank.meets(minimum_iv_rank)
        ):
            actual = (
                report.iv_rank.iv_rank
                if report.iv_rank and report.iv_rank.iv_rank is not None
                else "unavailable"
            )
            if enforce_iv_rank:
                report.blockers.append(
                    f"IV Rank {actual} is below the {minimum_iv_rank} entry filter"
                )
            else:
                report.refusal_codes.append("OPTIONS_IV_RANK_FILTER_BYPASSED")

        margin = IBKRWhatIfAdapter(ib).what_if(candidate, observed_at=now)

        snapshot_state: PortfolioSnapshot | None = None
        if portfolio is not None:
            try:
                snapshot_state = portfolio.snapshot(as_of=now)
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                report.errors.append(f"portfolio unavailable: {type(exc).__name__}: {exc}")
        if snapshot_state is not None:
            # The governor's concentration buckets are only meaningful once the
            # engine's own open structures are in them. This is what closes G1.
            snapshot_state = PortfolioSnapshot(
                as_of=snapshot_state.as_of,
                net_liquidation=snapshot_state.net_liquidation,
                positions=store.exposures(),
                reported_buying_power_reserved=snapshot_state.reported_buying_power_reserved,
            )
        report.portfolio = snapshot_state

        report.risk = assess_candidate(
            candidate,
            policy=policy,
            quotes=snapshot,
            margin=margin,
            underlying_price=(
                snapshot.underlying.mid if snapshot is not None else None
            ),
            net_liquidation=(
                snapshot_state.net_liquidation if snapshot_state else None
            ),
            evaluated_at=now,
        )
        report.governor = PortfolioGovernor(policy).evaluate(
            candidate, snapshot=snapshot_state, margin=margin, decision_time=now
        )
        report.refusal_codes.extend(report.risk.reason_codes)
        report.refusal_codes.extend(report.governor.reason_codes)
        for refusal in report.risk.refusals:
            report.blockers.append(f"candidate risk: {refusal.detail}")
        for refusal in report.governor.refusals:
            report.blockers.append(f"portfolio governor: {refusal.detail}")

        if report.blockers:
            return report

        # -- 3b. the caller's own last word --------------------------------
        # After every engine gate, before the token exists. A refusal here means
        # no TransmitAuthorization is ever minted for this candidate, rather than
        # one being minted and then not used.
        if entry_preflight is not None:
            try:
                refusal = entry_preflight(
                    intent=candidate,
                    snapshot=snapshot,
                    market_data=market_data,
                    policy=policy,
                    now=now,
                    armed=armed,
                )
            except Exception as exc:  # noqa: BLE001 - a broken preflight refuses
                refusal = f"the entry preflight raised {type(exc).__name__}: {exc}"
            if refusal:
                report.blockers.append(f"entry preflight: {refusal}")
                report.refusal_codes.append("OPTIONS_ENTRY_PREFLIGHT_REFUSED")
                return report

        # -- 4. propose to the reviewer, authorize, transmit ----------------
        # The verifier is required for an entry and optional for the pass: a
        # run that only reconciles and manages must not need a reviewer, and a
        # run that wants to *open* must not proceed without one. Fail-closed, so
        # the missing-verifier case blocks rather than defaults to permitted.
        if verifier is None or approval_context is None:
            report.blockers.append(
                "entry refused: no independent verifier gate is configured for this "
                "pass, so no opening trade can be authorized"
            )
            report.refusal_codes.append("OPTIONS_VERIFIER_NOT_CONFIGURED")
            return report

        # -- 3c. binding revalidation (2026-08-01 audit) ---------------------
        # A candidate may have been nominated from cached discovery, and even
        # a same-pass build can be minutes old after slow management or a
        # working-order walk. Immediately before the packet Grok reviews --
        # and therefore immediately before authorize_open, which follows in
        # the same breath and re-derives the spec digest from these same
        # objects -- every binding fact is re-established from the market as
        # it is NOW: fresh selected-leg quotes with a two-sided book demanded,
        # fresh what-if margin, fresh portfolio snapshot, fresh risk and
        # governor verdicts. Anything that moved moves the digest with it, so
        # an already-reviewed packet stops matching and needs a new review --
        # the invalidation rule working, not failing.
        binding_now = (
            now if pinned_clock
            else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        )
        binding_quotes = _quotes_for(
            market_data,
            symbol=symbol,
            con_ids=[leg.con_id for leg in candidate.legs],
            report=report,
            label="binding revalidation",
            require_two_sided=True,
        )
        if binding_quotes is None:
            report.blockers.append(
                "binding revalidation: the selected legs could not be re-quoted, "
                "so no packet may bind them"
            )
            report.refusal_codes.append("OPTIONS_BINDING_UNQUOTABLE")
            return report
        margin = IBKRWhatIfAdapter(ib).what_if(candidate, observed_at=binding_now)
        if portfolio is not None:
            try:
                fresh_state = portfolio.snapshot(as_of=binding_now)
                snapshot_state = PortfolioSnapshot(
                    as_of=fresh_state.as_of,
                    net_liquidation=fresh_state.net_liquidation,
                    positions=store.exposures(),
                    reported_buying_power_reserved=(
                        fresh_state.reported_buying_power_reserved
                    ),
                )
                report.portfolio = snapshot_state
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                report.blockers.append(
                    f"binding revalidation: portfolio unavailable: "
                    f"{type(exc).__name__}: {exc}"
                )
                report.refusal_codes.append("OPTIONS_BINDING_UNQUOTABLE")
                return report
        report.risk = assess_candidate(
            candidate,
            policy=policy,
            quotes=binding_quotes,
            margin=margin,
            underlying_price=binding_quotes.underlying.mid,
            net_liquidation=(
                snapshot_state.net_liquidation if snapshot_state else None
            ),
            evaluated_at=binding_now,
            quoted_window=len(snapshot.legs) if snapshot is not None else None,
        )
        report.governor = PortfolioGovernor(policy).evaluate(
            candidate,
            snapshot=snapshot_state,
            margin=margin,
            decision_time=binding_now,
        )
        binding_refusals = [
            *(f"binding risk: {r.detail}" for r in report.risk.refusals),
            *(f"binding governor: {r.detail}" for r in report.governor.refusals),
        ]
        if binding_refusals:
            report.blockers.extend(binding_refusals)
            report.refusal_codes.extend(report.risk.reason_codes)
            report.refusal_codes.extend(report.governor.reason_codes)
            return report

        packet = packet_for(
            candidate,
            structure_digest=structure_digest(candidate),
            risk=report.risk,
            governor=report.governor,
            context=approval_context,
            order_type=COMBO_ORDER_TYPE,
            time_in_force=COMBO_TIME_IN_FORCE,
            now=binding_now,
            evidence=_entry_evidence(
                report,
                margin=margin,
                snapshot=binding_quotes,
                intent=candidate,
                minimum_iv_rank=minimum_iv_rank,
            ),
        )

        try:
            authorization = authorize_open(
                candidate,
                gate=gate,
                risk=report.risk,
                governor=report.governor,
                armed=armed,
                now=now,
                verifier=verifier,
                packet=packet,
            )
        except AwaitingVerification as exc:
            # Not a refusal of the candidate -- a statement that the answer has
            # not arrived. Recorded and returned, never waited on: everything
            # else this pass did (reconciliation, management, exits) already
            # happened above, and the next pass picks the answer up.
            report.verification = VerificationState.AWAITING_VERIFICATION
            report.verification_request = exc.request_id
            report.blockers.append(f"entry awaiting verification: {exc.message}")
            report.refusal_codes.append("OPTIONS_AWAITING_VERIFICATION")
            return report
        except RefusedError as exc:
            report.verification = VerificationState.REFUSED
            report.blockers.append(f"entry refused: {exc.message}")
            return report
        report.verification = VerificationState.CONSUMED

        bpr = (
            margin.initial_margin_change
            if margin.initial_margin_change is not None
            else ZERO
        )
        # Recorded BEFORE the send. A crash after this leaves an OPENING record
        # the reconciler can resolve; the reverse leaves a live spread nothing
        # knows about.
        store.record_open_submitted(candidate, at=now, buying_power_reserved=bpr)

        try:
            result = place_combo(
                ib,
                candidate,
                authorization=authorization,
                account=account,
                sink=recorder,
            )
        except Exception as exc:  # noqa: BLE001 - a failed send must be recorded
            store.record_open_failed(candidate.strategy_id, at=now, reason=str(exc))
            report.errors.append(f"entry transmission failed: {exc}")
            return report

        report.transmissions.append(result)
        journal.record("order_placed", **result.to_record())

        # -- 5. work the order, if the broker took it and did not fill it ----
        # Runs before the outcome is judged, because until the ladder has
        # finished there is no final outcome to judge: an order still working
        # is not an order whose result is known, and the previous code called
        # that "unknown" and stopped -- leaving the order resting.
        if reprice is not None and _still_working(result):
            outcome = work_order(
                ib,
                candidate,
                result,
                authorization=authorization,
                gate=gate,
                armed=armed,
                # The ladder reprices an OPEN, and a reprice is a new opening
                # order under the invalidation rule. It gets the same verifier
                # the entry did, so every rung is reviewed rather than riding on
                # the approval that covered the first price.
                verifier=verifier,
                approval_context=approval_context,
                # Both ends of the deadline come from the same clock. ``now``
                # is the pass's *logical* time and may be injected -- measuring
                # a two-minute wall-clock deadline from it made the ladder
                # expire before its first rung whenever the two disagreed.
                started_at=_wall_clock(),
                clock=_wall_clock,
                # A cancelled rung is a real OPEN_FAILED -- nothing is in the
                # market -- and replaying that retires the position AND releases
                # its buying-power reservation. So the replacement needs its own
                # submission record, carrying the same reservation, written
                # before it is sent, exactly as the first one was above.
                # Without it the governor would size the next decision against a
                # book that believes this capital is free while a real order
                # rests in the market.
                record_submission=lambda replacement: store.record_open_submitted(
                    replacement,
                    at=dt.datetime.now(dt.timezone.utc),
                    buying_power_reserved=bpr,
                ),
                # And the order journal, because gate_daily_count() counts
                # ``order_placed`` records. Every real transmission is one.
                record_transmission=lambda sent: journal.record(
                    "order_placed", **sent.to_record()
                ),
                ladder=reprice,
                envelope=envelope_for(candidate),
                account=account,
                sink=recorder,
            )
            report.reprice = outcome
            # A summary of the ladder, under its own event name. The individual
            # sends were already journalled as ``order_placed`` by
            # ``record_transmission`` above -- this must NOT be another one, or
            # the daily count would double-count every rung.
            journal.record("order_worked", **outcome.to_record())
            if outcome.final is not None and outcome.final is not result:
                # Only a *different* order is a second transmission. A ladder
                # that refused before it sent anything hands back the same
                # result it was given, and reporting that twice would make the
                # pass look like it placed two orders.
                report.transmissions.append(outcome.final)
                result = outcome.final
            if outcome.detail:
                report.blockers.append(f"entry order worked: {outcome.describe()}")

        _record_open_outcome(candidate.strategy_id, result, store=store, now=now, report=report)

    except EngineError:
        raise
    except Exception as exc:  # noqa: BLE001 - a pass reports, it does not crash
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.blockers.append("run did not complete")
    finally:
        report.finished_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    return report
