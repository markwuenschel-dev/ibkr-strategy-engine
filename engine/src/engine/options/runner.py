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

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from ..errors import EngineError, RefusedError
from ..journal import OrderJournal
from ..safety import SafetyGate
from .adapters import IBKRWhatIfAdapter
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
from .policy import RiskPolicy
from .portfolio import PortfolioSnapshot
from .ports import LiveMarketDataPort, PortfolioStatePort, StrategyQuoteSnapshot
from .positions import (
    OpenPosition,
    PositionStore,
    ReconciliationOutcome,
    ReconciliationReport,
)
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
from .transmit import TransmitResult, authorize_close, authorize_open, place_combo

__all__ = ["RunReport", "run_once", "mark_from_snapshot", "CONFIG_VERSION"]

CONFIG_VERSION = "options-runner/1"

ZERO = Decimal("0")


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
    decisions: list[ManagementDecision] = field(default_factory=list)
    transmissions: list[TransmitResult] = field(default_factory=list)
    candidate: OptionStrategyIntent | None = None
    risk: CandidateRiskAssessment | None = None
    governor: GovernorVerdict | None = None
    portfolio: PortfolioSnapshot | None = None
    entered: bool = False
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
            "decisions": [d.to_record() for d in self.decisions],
            "transmissions": [t.to_record() for t in self.transmissions],
            "candidate": self.candidate.describe() if self.candidate else None,
            "risk": self.risk.to_record() if self.risk else None,
            "governor": self.governor.to_record() if self.governor else None,
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
    """
    if snapshot is None:
        return None

    quotes = {quote.con_id: quote for quote in snapshot.legs}
    total = ZERO
    for leg in position.legs:
        quote = quotes.get(leg.con_id)
        if quote is None:
            return None
        mid = quote.mid
        if mid is None or mid < ZERO:
            return None
        total += mid * leg.ratio if leg.is_short else -mid * leg.ratio

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
) -> StrategyQuoteSnapshot | None:
    if market_data is None or not con_ids:
        return None
    try:
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
    report: RunReport,
) -> None:
    """Decide and, if the decision acts, send the exit."""
    snapshot = _quotes_for(
        market_data,
        symbol=position.underlying,
        con_ids=[leg.con_id for leg in position.legs],
        report=report,
        label=f"manage {position.strategy_id}",
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
        configuration_version=CONFIG_VERSION,
        limit_price=limit_price,
        quantity=position.manageable_quantity,
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

    try:
        result = store.reconcile_against_broker(reported, checked_at=now)
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
    report: RunReport,
) -> tuple[OptionStrategyIntent | None, StrategyQuoteSnapshot | None]:
    """Chain -> quotes -> delta selection -> a validated opening intent."""
    from ib_async import Stock  # noqa: PLC0415 - optional dependency

    qualified_underlying = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
    if not qualified_underlying:
        report.blockers.append(f"IBKR did not qualify the underlying {symbol}")
        return None, None
    underlying = qualified_underlying[0]

    bars = ib.reqHistoricalData(
        underlying,
        endDateTime="",
        durationStr="1 Y",
        barSizeSetting="1 day",
        whatToShow="OPTION_IMPLIED_VOLATILITY",
        useRTH=True,
        formatDate=1,
    )
    report.iv_rank = build_iv_rank(
        symbol, observations_from_bars(bars or []), calculated_at=now
    )

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
    quotes = {q.con_id: q for q in snapshot.legs}
    short_mid = quotes[selection.short.con_id].mid if selection.short.con_id in quotes else None
    long_mid = quotes[selection.long.con_id].mid if selection.long.con_id in quotes else None
    if short_mid is None or long_mid is None:
        report.blockers.append("no two-sided market on the selected strikes")
        return None, snapshot
    credit = (short_mid - long_mid).quantize(Decimal("0.01"))
    if credit <= ZERO or credit >= selection.width:
        report.blockers.append(
            f"credit {credit} is not a usable price for a {selection.width}-wide spread"
        )
        return None, snapshot

    intent = build_vertical(
        selection,
        credit=credit,
        policy=policy,
        configuration_version=CONFIG_VERSION,
        created_at=now,
    )
    if intent is None:
        report.blockers.append(
            f"risk budget {policy.risk_budget_per_position} does not cover one contract"
        )
    return intent, snapshot


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
    strike_window: int = 24,
    account: str = "",
) -> RunReport:
    """Reconcile, manage every open position, then consider one new entry.

    Returns a report rather than raising, except for the kill switch -- a halted
    engine stops before it does anything at all, and that is the one condition
    that should reach the caller as an exception rather than a line in a report.
    """
    now = now or dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    today = today or now.date()
    report = RunReport(started_at=now, armed=armed, symbol=symbol)
    ib = broker.ib

    gate.assert_not_halted()

    # Seeded from the store, so a recorder built after a restart already knows
    # what the previous process persisted and will not re-append transitions it
    # can already see on disk.
    recorder = LifecycleRecorder(store)

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

        candidate, snapshot = _build_candidate(
            ib=ib,
            broker=broker,
            symbol=symbol,
            bias=bias,
            policy=policy,
            market_data=market_data,
            now=now,
            today=today,
            target_dte=target_dte,
            minimum_dte=minimum_dte,
            maximum_dte=maximum_dte,
            strike_window=strike_window,
            report=report,
        )
        report.candidate = candidate
        if candidate is None:
            return report

        if report.iv_rank is None or not report.iv_rank.meets(minimum_iv_rank):
            actual = (
                report.iv_rank.iv_rank
                if report.iv_rank and report.iv_rank.iv_rank is not None
                else "unavailable"
            )
            report.blockers.append(
                f"IV Rank {actual} is below the {minimum_iv_rank} entry filter"
            )

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

        # -- 4. authorize and transmit -------------------------------------
        try:
            authorization = authorize_open(
                candidate,
                gate=gate,
                risk=report.risk,
                governor=report.governor,
                armed=armed,
                now=now,
            )
        except RefusedError as exc:
            report.blockers.append(f"entry refused: {exc.message}")
            return report

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

        _record_open_outcome(candidate.strategy_id, result, store=store, now=now, report=report)

    except EngineError:
        raise
    except Exception as exc:  # noqa: BLE001 - a pass reports, it does not crash
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.blockers.append("run did not complete")
    finally:
        report.finished_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    return report
