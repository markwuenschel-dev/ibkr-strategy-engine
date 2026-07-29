"""The shadow scan: everything that works today, run end to end.

Three of the four capabilities proven against the delayed-only paper account
need no market-data subscription — chain enumeration, implied-volatility history
and the credit-spread what-if all returned real data. This command runs those,
in order, and produces genuine IBKR numbers for every step except one.

The exception is strike selection, which is delta-based and therefore blocked
until live greeks exist. Rather than skip it or fake it, the scan selects
strikes by a declared offset method, labels the result
:attr:`SelectionMethod.SHADOW_STRIKE_OFFSET`, and sets ``tradeable`` to False.

**Nothing produced here is a trade candidate.** ``tradeable`` is False unless
the strikes were delta-selected, the quotes were live, and IV Rank was usable —
and no code path in this module transmits anything regardless. The output is a
real margin figure for a real structure, which is what makes the rest of the
pipeline testable before the entitlement lands.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import uuid4

from .chain import (
    ExpirySelection,
    QualifiedOption,
    discover_expirations,
    enumerate_strikes,
    narrow_strikes,
    qualify_strikes,
    select_expiration,
)
from .domain import (
    OptionLegIntent,
    OptionRight,
    OptionStrategyIntent,
    OrderAction,
    PriceEffect,
    StrategyAction,
    StrategyType,
    compute_maximum_loss_per_contract,
)
from .execution import MarginAssessment, what_if
from .governor import GovernorVerdict, PortfolioGovernor
from .ivrank import IVRankMetric, build_iv_rank, observations_from_bars
from .policy import RiskPolicy
from .portfolio import PortfolioSnapshot
from .ports import LiveMarketDataPort, PortfolioStatePort, StrategyQuoteSnapshot
from .risk import CandidateRiskAssessment, assess_candidate

__all__ = ["SelectionMethod", "ScanReport", "run_scan", "CONFIG_VERSION"]

CONFIG_VERSION = "options-scan/1"


class SelectionMethod(str, Enum):
    """How the strikes were chosen. Only DELTA yields a trade candidate."""

    DELTA = "DELTA"
    SHADOW_STRIKE_OFFSET = "SHADOW_STRIKE_OFFSET"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


@dataclass
class ScanReport:
    symbol: str
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    account: str = ""
    underlying_price: Decimal | None = None
    underlying_price_source: str = "unknown"
    iv_rank: IVRankMetric | None = None
    expiry: ExpirySelection | None = None
    strikes_listed: int = 0
    strikes_qualified: int = 0
    selection_method: SelectionMethod = SelectionMethod.SHADOW_STRIKE_OFFSET
    candidate: OptionStrategyIntent | None = None
    margin: MarginAssessment | None = None
    risk: CandidateRiskAssessment | None = None
    governor: GovernorVerdict | None = None
    portfolio: PortfolioSnapshot | None = None
    policy_version: str = ""
    tradeable: bool = False
    blockers: list[str] = field(default_factory=list)
    refusal_codes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines: list[str] = []
        lines.append(f"symbol            {self.symbol}")
        lines.append(f"account           {self.account}")
        under = self.underlying_price if self.underlying_price is not None else "unavailable"
        lines.append(f"underlying        {under}  [{self.underlying_price_source}]")
        lines.append("")

        lines.append("IV RANK")
        lines.append(f"  {self.iv_rank.describe() if self.iv_rank else 'not computed'}")
        if self.iv_rank:
            lines.append(f"  source          {self.iv_rank.source}")
        lines.append("")

        lines.append("EXPIRY")
        lines.append(f"  {self.expiry.describe() if self.expiry else 'none in the 35-55 DTE window'}")
        lines.append(
            f"  strikes         {self.strikes_qualified} qualified "
            f"of {self.strikes_listed} listed for this expiration"
        )
        lines.append("")

        lines.append(f"STRUCTURE        selection method: {self.selection_method.value}")
        if self.candidate is not None:
            lines.append(f"  {self.candidate.describe()}")
            lines.append(f"  max loss/ct     {self.candidate.maximum_loss_per_contract}")
            lines.append(f"  total max loss  {self.candidate.total_maximum_loss}")
            lines.append(f"  credit          {self.candidate.total_credit}")
        else:
            lines.append("  no structure built")
        lines.append("")

        lines.append("BROKER WHAT-IF   (real IBKR margin; nothing was transmitted)")
        lines.append(self.margin.describe() if self.margin else "  not run")
        lines.append("")

        lines.append(self.risk.describe() if self.risk else "CANDIDATE RISK   not evaluated")
        lines.append("")

        lines.append(
            self.governor.describe() if self.governor else "PORTFOLIO GOVERNOR   not evaluated"
        )
        lines.append("")

        lines.append(f"TRADEABLE        {'YES' if self.tradeable else 'NO'}")
        for blocker in self.blockers:
            lines.append(f"  blocked by      {blocker}")
        for code in self.refusal_codes:
            lines.append(f"  refusal code    {code}")
        if self.errors:
            lines.append("")
            lines.append("BROKER MESSAGES")
            lines.extend(f"  {e}" for e in self.errors)
        return "\n".join(lines)

    def to_record(self) -> dict[str, Any]:
        return {
            "event": "options_scan",
            "symbol": self.symbol,
            "account": self.account,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "underlying_price": str(self.underlying_price)
            if self.underlying_price is not None
            else None,
            "underlying_price_source": self.underlying_price_source,
            "iv_rank": self.iv_rank.to_record() if self.iv_rank else None,
            "expiry": self.expiry.expiry if self.expiry else None,
            "dte": self.expiry.dte if self.expiry else None,
            "strikes_listed": self.strikes_listed,
            "strikes_qualified": self.strikes_qualified,
            "selection_method": self.selection_method.value,
            "candidate": self.candidate.describe() if self.candidate else None,
            "maximum_loss_per_contract": str(self.candidate.maximum_loss_per_contract)
            if self.candidate
            else None,
            "margin": self.margin.to_record() if self.margin else None,
            "risk": self.risk.to_record() if self.risk else None,
            "governor": self.governor.to_record() if self.governor else None,
            "policy_version": self.policy_version or None,
            "tradeable": self.tradeable,
            "blockers": list(self.blockers),
            "refusal_codes": list(self.refusal_codes),
            "errors": list(self.errors),
        }


def _shadow_spread(
    qualified: list[QualifiedOption],
    reference: Decimal | None,
    *,
    short_offset: int,
    width_steps: int,
) -> tuple[QualifiedOption, QualifiedOption] | None:
    """Pick a short and a protective long by position in the strike ladder.

    This is NOT strike selection. It exists so the what-if has a real, valid,
    defined-risk structure to price while delta is unavailable, and every report
    that uses it is labelled SHADOW_STRIKE_OFFSET and marked untradeable.
    """
    puts = [q for q in qualified if q.right.upper().startswith("P")]
    if len(puts) < 2:
        return None
    if reference is None:
        anchor = len(puts) // 2
    else:
        anchor = min(range(len(puts)), key=lambda i: abs(puts[i].strike - reference))

    short_index = anchor - short_offset
    long_index = short_index - width_steps
    if long_index < 0 or short_index <= 0 or short_index >= len(puts):
        return None
    short, long = puts[short_index], puts[long_index]
    if long.strike >= short.strike or short.multiplier != long.multiplier:
        return None
    return short, long


def _portfolio_snapshot(
    port: PortfolioStatePort | None,
    decision_time: dt.datetime,
    report: ScanReport,
) -> PortfolioSnapshot | None:
    """Read the book, or record why it could not be read and return ``None``.

    Swallowing the failure into ``None`` is safe *here specifically* because
    ``None`` is what the governor refuses on. The error text is still recorded,
    so an adapter that is broken looks different in the report from an adapter
    that was never supplied -- both refuse, and the operator can tell which.
    """
    if port is None:
        return None
    try:
        return port.snapshot(as_of=decision_time)
    # Deliberately broad. This is an adapter boundary: whatever ib_async raises
    # -- a timeout, an asyncio error, an attribute that moved between versions --
    # the correct outcome is a recorded refusal, never a crashed scan. Narrowing
    # this would turn an unfamiliar broker error into a traceback in place of a
    # report, and the refusal is identical either way.
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"portfolio snapshot unavailable: {type(exc).__name__}: {exc}")
        return None


def _strategy_quotes(
    port: LiveMarketDataPort | None,
    report: ScanReport,
) -> StrategyQuoteSnapshot | None:
    """Read live quotes for the built structure, or ``None``.

    Same contract as :func:`_portfolio_snapshot`: ``None`` is refused downstream
    by the entitlement check, so a failure here cannot become a pass. The
    candidate's own leg ids are used, so the quotes are for the structure that
    was actually built rather than for whatever was most recently looked at.
    """
    if port is None or report.candidate is None:
        return None
    try:
        return port.strategy_quotes(
            underlying_symbol=report.candidate.underlying,
            con_ids=[leg.con_id for leg in report.candidate.legs],
        )
    # Deliberately broad. This is an adapter boundary: whatever ib_async raises
    # -- a timeout, an asyncio error, an attribute that moved between versions --
    # the correct outcome is a recorded refusal, never a crashed scan. Narrowing
    # this would turn an unfamiliar broker error into a traceback in place of a
    # report, and the refusal is identical either way.
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"live quotes unavailable: {type(exc).__name__}: {exc}")
        return None


def run_scan(
    broker: Any,
    *,
    symbol: str = "SPY",
    target_dte: int = 45,
    minimum_dte: int = 35,
    maximum_dte: int = 55,
    minimum_iv_rank: Decimal = Decimal("50"),
    strike_window: int = 24,
    short_offset: int = 4,
    width_steps: int = 5,
    credit_fraction: Decimal = Decimal("0.30"),
    account: str = "",
    policy: RiskPolicy | None = None,
    market_data: LiveMarketDataPort | None = None,
    portfolio: PortfolioStatePort | None = None,
) -> ScanReport:
    """Run every step that works without a market-data subscription.

    ``policy`` defaults to :class:`~engine.options.policy.RiskPolicy` defaults.
    ``market_data`` and ``portfolio`` default to ``None``, and ``None`` is a
    refusal rather than a skip: with no live feed the entitlement check refuses
    with ``OPTIONS_NO_MARKET_DATA_SNAPSHOT``, and with no portfolio port every
    governor check refuses with ``GOVERNOR_PORTFOLIO_STATE_UNAVAILABLE``. A scan
    run with no ports is therefore a complete, correctly-refused scan -- which is
    exactly what the current entitlement allows and what the operator should see.
    """
    report = ScanReport(symbol=symbol, started_at=_utcnow(), account=account)
    active_policy = policy if policy is not None else RiskPolicy()
    report.policy_version = active_policy.version
    ib = broker.ib

    def on_error(req_id: int, code: int, message: str, *_: Any) -> None:
        report.errors.append(f"{code}: {message}")

    try:
        from ib_async import Stock  # noqa: PLC0415

        ib.errorEvent += on_error
        underlying = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))[0]

        # ---- underlying reference price (delayed is fine; it only narrows) --
        quote = broker.quote(symbol)
        if quote.price is not None:
            report.underlying_price = Decimal(str(quote.price))
        report.underlying_price_source = quote.source

        # ---- IV Rank, from IBKR's own implied-volatility history ------------
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
            symbol, observations_from_bars(bars or []), calculated_at=_utcnow()
        )

        # ---- expiry ---------------------------------------------------------
        expirations = discover_expirations(ib, symbol, underlying.conId)
        report.expiry = select_expiration(
            expirations,
            today=dt.date.today(),
            target_dte=target_dte,
            minimum_dte=minimum_dte,
            maximum_dte=maximum_dte,
        )
        if report.expiry is None:
            report.blockers.append(
                f"no expiration between {minimum_dte} and {maximum_dte} DTE"
            )
            return report

        # ---- strikes --------------------------------------------------------
        listed = enumerate_strikes(ib, symbol, report.expiry.expiry, "P")
        report.strikes_listed = len(listed)
        window = narrow_strikes(
            listed, reference_price=report.underlying_price, width=strike_window
        )
        qualified = qualify_strikes(ib, symbol, report.expiry.expiry, window, "P")
        report.strikes_qualified = len(qualified)

        # ---- structure ------------------------------------------------------
        # Delta is unavailable without live greeks, so strikes come from the
        # declared offset method and the report is marked untradeable.
        report.selection_method = SelectionMethod.SHADOW_STRIKE_OFFSET
        report.blockers.append(
            "strikes were not delta-selected (SHADOW_STRIKE_OFFSET); "
            "live option greeks are required for real selection"
        )

        pair = _shadow_spread(
            qualified,
            report.underlying_price,
            short_offset=short_offset,
            width_steps=width_steps,
        )
        if pair is None:
            report.blockers.append("could not assemble a defined-risk pair from the chain")
            return report

        short, long = pair
        width = short.strike - long.strike
        credit = (width * credit_fraction).quantize(Decimal("0.01"))
        if credit <= 0 or credit >= width:
            report.blockers.append(f"could not derive a valid credit for a {width}-wide spread")
            return report

        legs = (
            OptionLegIntent(
                con_id=short.con_id,
                symbol=symbol,
                expiration=short.expiration,
                strike=short.strike,
                right=OptionRight.PUT,
                action=OrderAction.SELL,
                ratio=1,
                multiplier=short.multiplier,
                exchange=short.exchange,
                trading_class=short.trading_class,
            ),
            OptionLegIntent(
                con_id=long.con_id,
                symbol=symbol,
                expiration=long.expiration,
                strike=long.strike,
                right=OptionRight.PUT,
                action=OrderAction.BUY,
                ratio=1,
                multiplier=long.multiplier,
                exchange=long.exchange,
                trading_class=long.trading_class,
            ),
        )
        max_loss = compute_maximum_loss_per_contract(
            strategy_type=StrategyType.PUT_CREDIT_SPREAD,
            legs=legs,
            credit=credit,
            multiplier=short.multiplier,
        )
        report.candidate = OptionStrategyIntent(
            strategy_id=uuid4(),
            strategy_type=StrategyType.PUT_CREDIT_SPREAD,
            strategy_action=StrategyAction.OPEN,
            underlying=symbol,
            quantity=1,
            legs=legs,
            expiration=short.expiration,
            limit_price=credit,
            price_effect=PriceEffect.CREDIT,
            maximum_loss_per_contract=max_loss,
            configuration_version=CONFIG_VERSION,
            created_at=_utcnow(),
        )

        # ---- broker what-if: real margin, nothing transmitted ---------------
        report.margin = what_if(ib, report.candidate, observed_at=_utcnow())

        # ---- portfolio state, for the governor -------------------------------
        decision_time = _utcnow()
        report.portfolio = _portfolio_snapshot(portfolio, decision_time, report)

        # ---- candidate risk: the four checks, none skippable ----------------
        # The entitlement gate lives inside assess_candidate as a required check,
        # so there is no arrangement of these lines that evaluates a candidate
        # without asking whether its market data was allowed to inform a decision.
        report.risk = assess_candidate(
            report.candidate,
            policy=active_policy,
            quotes=_strategy_quotes(market_data, report),
            margin=report.margin,
            underlying_price=report.underlying_price,
            net_liquidation=(
                report.portfolio.net_liquidation if report.portfolio else None
            ),
            evaluated_at=decision_time,
        )

        # ---- portfolio governor ---------------------------------------------
        # Runs here, before anything downstream could act on the candidate. When
        # delta selection lands, this same call moves inside the selection loop so
        # a refusal ("the technology sector is full") can redirect the search
        # rather than only veto its result.
        report.governor = PortfolioGovernor(active_policy).evaluate(
            report.candidate,
            snapshot=report.portfolio,
            margin=report.margin,
            decision_time=decision_time,
        )

        # ---- eligibility ----------------------------------------------------
        if report.iv_rank is None or not report.iv_rank.meets(minimum_iv_rank):
            actual = (
                report.iv_rank.iv_rank
                if report.iv_rank and report.iv_rank.iv_rank is not None
                else "unavailable"
            )
            report.blockers.append(f"IV Rank {actual} is below the {minimum_iv_rank} entry filter")

        for refusal in report.risk.refusals:
            report.blockers.append(f"candidate risk: {refusal.detail}")
        for refusal in report.governor.refusals:
            report.blockers.append(f"portfolio governor: {refusal.detail}")
        report.refusal_codes.extend(report.risk.reason_codes)
        report.refusal_codes.extend(report.governor.reason_codes)

        # Every conjunct is required. `risk.approved` alone already implies live,
        # current, same-generation data for the underlying and every leg, so a
        # delayed-data run cannot reach True here by any route -- but the
        # selection-method term is kept explicit because an offset-selected
        # structure is not this strategy even when its data is impeccable.
        report.tradeable = (
            report.selection_method is SelectionMethod.DELTA
            and report.risk.approved
            and report.governor.approved
            and not report.blockers
        )

    except Exception as exc:  # noqa: BLE001 - a scan reports, it does not crash
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.blockers.append("scan did not complete")
    finally:
        try:
            ib.errorEvent -= on_error
        except Exception:  # noqa: BLE001
            pass
        report.finished_at = _utcnow()

    return report
