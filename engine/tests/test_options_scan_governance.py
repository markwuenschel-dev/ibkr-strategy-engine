"""The scan's governance wiring: which verdicts reach the report, and what they gate.

Separate from ``test_options_scan.py``, which covers the chain, IV Rank and
what-if steps that predate this milestone.

One honest limitation is asserted here rather than worked around: the shadow scan
forces ``SelectionMethod.SHADOW_STRIKE_OFFSET``, so ``report.tradeable`` cannot
be ``True`` by any input. The tests therefore prove the *negative* interlocks --
that live data alone does not make a candidate tradeable, and that delayed data
cannot -- and assert the selection-method blocker explicitly so the day delta
selection lands, the test that pins it is already here and starts failing for the
right reason.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from engine.options.marketdata import (
    MarketDataProvenance,
    MarketDataType,
    OptionGreeks,
    OptionQuote,
    UnderlyingQuote,
)
from engine.options.policy import RiskPolicy
from engine.options.portfolio import PortfolioSnapshot, PositionExposure
from engine.options.ports import StrategyQuoteSnapshot
from engine.options.risk import (
    CHECK_BROKER_MARGIN,
    CHECK_DEFINED_LOSS,
    CHECK_MARKET_DATA_ENTITLEMENT,
    CHECK_STRESS_LOSS,
)
from engine.options.scan import SelectionMethod, run_scan

D = Decimal


# ---------------------------------------------------------------------------
# A broker fake complete enough to reach the risk and governor stages
# ---------------------------------------------------------------------------


class _Event:
    def __iadd__(self, _handler: object) -> "_Event":
        return self

    def __isub__(self, _handler: object) -> "_Event":
        return self


class _Contract:
    def __init__(self, *, con_id: int, strike: float, right: str) -> None:
        self.conId = con_id  # noqa: N815
        self.strike = strike
        self.right = right
        self.multiplier = "100"
        self.exchange = "SMART"
        self.tradingClass = "SPY"  # noqa: N815


class _Bar:
    def __init__(self, when: dt.date, close: float) -> None:
        self.date = when
        self.close = close


class _Chain:
    strikes = [float(s) for s in range(400, 601, 5)]

    def __init__(self) -> None:
        self.expirations = [
            (dt.date.today() + dt.timedelta(days=days)).strftime("%Y%m%d")
            for days in (14, 45, 80)
        ]


class _Detail:
    def __init__(self, strike: float) -> None:
        self.contract = _Contract(con_id=int(strike), strike=strike, right="P")


class _OrderState:
    initMarginChange = 500.0  # noqa: N815
    maintMarginChange = 500.0  # noqa: N815
    equityWithLoanChange = -2.0  # noqa: N815
    commission = 1.30
    warningText = ""  # noqa: N815


class _Quote:
    price = 500.0
    source = "delayed"


class FakeIB:
    def __init__(self, *, iv_high: bool = True) -> None:
        self.errorEvent = _Event()  # noqa: N815
        self.iv_high = iv_high

    def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
        out = []
        for index, contract in enumerate(contracts, start=1):
            if getattr(contract, "secType", "") == "STK":
                out.append(_Contract(con_id=9000, strike=0.0, right=""))
                continue
            out.append(
                _Contract(
                    con_id=1000 + index,
                    strike=float(getattr(contract, "strike", 0.0)),
                    right=getattr(contract, "right", "P"),
                )
            )
        return out

    def reqHistoricalData(self, *_a: Any, **_k: Any) -> list[Any]:  # noqa: N802
        start = dt.date(2025, 8, 1)
        if self.iv_high:
            # Ends near the top of its own range, so IV Rank clears 50.
            return [
                _Bar(start + dt.timedelta(days=i), 0.10 + 0.001 * i) for i in range(260)
            ]
        return [
            _Bar(start + dt.timedelta(days=i), 0.40 - 0.001 * i) for i in range(260)
        ]

    def reqSecDefOptParams(self, *_a: Any, **_k: Any) -> list[Any]:  # noqa: N802
        return [_Chain()]

    def reqContractDetails(self, _contract: Any) -> list[Any]:  # noqa: N802
        return [_Detail(float(s)) for s in range(400, 601, 5)]

    def whatIfOrder(self, _contract: Any, _order: Any) -> Any:  # noqa: N802
        return _OrderState()


class FakeBroker:
    def __init__(self, **kwargs: Any) -> None:
        self.ib = FakeIB(**kwargs)

    def quote(self, _symbol: str) -> _Quote:
        return _Quote()


# ---------------------------------------------------------------------------
# Fake ports
# ---------------------------------------------------------------------------


NOW_ISH = None  # ports stamp their own times relative to the scan's clock


def _provenance(
    generation: UUID, *, reported: MarketDataType, at: dt.datetime
) -> MarketDataProvenance:
    return MarketDataProvenance(
        requested_type=int(MarketDataType.LIVE),
        subscription_generation=generation,
        subscribed_at=at,
        reported_type=int(reported),
        callback_received=True,
        last_provider_event_at=at,
        last_local_receive_at=at,
    )


class FakeMarketDataPort:
    """Returns a coherent snapshot at whatever liveness the test asks for.

    Stamped at call time rather than at a fixed constant, because the scan reads
    a real clock for its decision time and a frozen timestamp would fail the
    staleness check for reasons the test is not about.
    """

    def __init__(self, *, reported: MarketDataType = MarketDataType.LIVE) -> None:
        self.reported = reported
        self.calls: list[tuple[str, tuple[int, ...]]] = []

    def strategy_quotes(
        self, *, underlying_symbol: str, con_ids: Any
    ) -> StrategyQuoteSnapshot:
        con_ids = tuple(int(c) for c in con_ids)
        self.calls.append((underlying_symbol, con_ids))
        at = dt.datetime.now(dt.timezone.utc)
        under_gen = uuid4()
        generations = [("underlying", under_gen)]
        legs = []
        for con_id in con_ids:
            gen = uuid4()
            legs.append(
                OptionQuote(
                    con_id=con_id,
                    provenance=_provenance(gen, reported=self.reported, at=at),
                    bid=D("1.40"),
                    ask=D("1.60"),
                    greeks=OptionGreeks(
                        received_at=at,
                        subscription_generation=gen,
                        delta=D("-0.16"),
                    ),
                )
            )
            generations.append((str(con_id), gen))
        return StrategyQuoteSnapshot(
            underlying=UnderlyingQuote(
                symbol=underlying_symbol,
                provenance=_provenance(under_gen, reported=self.reported, at=at),
                bid=D("499.90"),
                ask=D("500.10"),
            ),
            legs=tuple(legs),
            generations=tuple(generations),
        )


class FakePortfolioPort:
    def __init__(
        self,
        *,
        net_liquidation: str = "1000000",
        positions: tuple[PositionExposure, ...] = (),
        reported: str | None = None,
    ) -> None:
        self.net_liquidation = D(net_liquidation)
        self.positions = positions
        self.reported = D(reported) if reported is not None else None

    def snapshot(self, *, as_of: dt.datetime) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            as_of=as_of,
            net_liquidation=self.net_liquidation,
            positions=self.positions,
            reported_buying_power_reserved=self.reported,
        )


class ExplodingPortfolioPort:
    def snapshot(self, *, as_of: dt.datetime) -> PortfolioSnapshot:
        raise LookupError("the broker returned no NetLiquidation")


# ===========================================================================
# The report carries both verdicts
# ===========================================================================


class TestVerdictsReachTheReport:
    def test_a_scan_with_no_ports_still_evaluates_both_layers(self) -> None:
        """Fail-closed must mean 'refused with a reason', not 'not evaluated'."""
        report = run_scan(FakeBroker(), symbol="SPY")

        assert report.risk is not None
        assert report.governor is not None
        assert report.risk.approved is False
        assert report.governor.approved is False
        assert report.policy_version == RiskPolicy().version

    def test_all_four_candidate_checks_are_present(self) -> None:
        report = run_scan(FakeBroker(), symbol="SPY")
        assert report.risk is not None
        names = {result.check for result in report.risk.results}
        assert names == {
            CHECK_MARKET_DATA_ENTITLEMENT,
            CHECK_DEFINED_LOSS,
            CHECK_BROKER_MARGIN,
            CHECK_STRESS_LOSS,
        }

    def test_refusal_codes_are_recorded_on_the_report(self) -> None:
        report = run_scan(FakeBroker(), symbol="SPY")
        assert "OPTIONS_NO_MARKET_DATA_SNAPSHOT" in report.refusal_codes
        assert "GOVERNOR_PORTFOLIO_STATE_UNAVAILABLE" in report.refusal_codes

    def test_the_record_is_json_shaped(self) -> None:
        import json

        report = run_scan(FakeBroker(), symbol="SPY")
        json.dumps(report.to_record())  # must not raise
        record = report.to_record()
        assert record["risk"] is not None
        assert record["governor"] is not None
        assert record["policy_version"] == RiskPolicy().version

    def test_describe_mentions_both_layers(self) -> None:
        text = run_scan(FakeBroker(), symbol="SPY").describe()
        assert "CANDIDATE RISK" in text
        assert "PORTFOLIO GOVERNOR" in text


# ===========================================================================
# Market-data provenance decides the entitlement check
# ===========================================================================


class TestEntitlementWiring:
    def _entitlement(self, report: Any) -> Any:
        return report.risk.result_for(CHECK_MARKET_DATA_ENTITLEMENT)

    def test_a_live_port_makes_the_entitlement_check_pass(self) -> None:
        """Proves the gate is genuinely wired in rather than hardcoded to
        refuse -- without this, every other entitlement test would pass for the
        wrong reason."""
        port = FakeMarketDataPort(reported=MarketDataType.LIVE)
        report = run_scan(
            FakeBroker(),
            symbol="SPY",
            market_data=port,
            portfolio=FakePortfolioPort(),
        )
        assert self._entitlement(report).approved, self._entitlement(report).detail
        assert port.calls, "the scan never asked the market-data port for quotes"

    def test_the_port_is_asked_for_the_structure_that_was_built(self) -> None:
        """Quoting a different set of contracts than the ones in the candidate
        would gate the wrong thing."""
        port = FakeMarketDataPort()
        report = run_scan(FakeBroker(), symbol="SPY", market_data=port)
        assert report.candidate is not None
        symbol, con_ids = port.calls[0]
        assert symbol == "SPY"
        assert set(con_ids) == {leg.con_id for leg in report.candidate.legs}

    def test_delayed_data_refuses_the_entitlement_check(self) -> None:
        report = run_scan(
            FakeBroker(),
            symbol="SPY",
            market_data=FakeMarketDataPort(reported=MarketDataType.DELAYED),
            portfolio=FakePortfolioPort(),
        )
        result = self._entitlement(report)
        assert not result.approved
        assert result.reason_code == "OPTIONS_REALTIME_DATA_REQUIRED"
        assert "OPTIONS_REALTIME_DATA_REQUIRED" in report.refusal_codes

    def test_delayed_data_can_never_make_a_candidate_tradeable(self) -> None:
        """The milestone's headline requirement, asserted end to end.

        ``tradeable is False`` alone is deliberately NOT the assertion. While
        selection is forced to SHADOW_STRIKE_OFFSET that flag is False for every
        input, so it would still pass with the entitlement gate ripped out
        entirely. The discriminating assertions are the two below it: the risk
        assessment must be refused, and it must be refused *by the entitlement
        check, for the delayed-data reason*.
        """
        report = run_scan(
            FakeBroker(),
            symbol="SPY",
            policy=RiskPolicy(
                max_defined_loss_per_position=D("2000"),
                max_stress_loss_per_position=D("2000"),
            ),
            market_data=FakeMarketDataPort(reported=MarketDataType.DELAYED),
            portfolio=FakePortfolioPort(),
        )
        assert report.tradeable is False
        assert report.risk is not None
        assert report.risk.approved is False

        entitlement = report.risk.result_for(CHECK_MARKET_DATA_ENTITLEMENT)
        assert entitlement.approved is False
        assert entitlement.reason_code == "OPTIONS_REALTIME_DATA_REQUIRED"

        # And it is the ONLY thing refusing: with the caps raised, every other
        # check passes, so this test fails if the gate stops working rather
        # than passing on the strength of some unrelated refusal.
        assert report.risk.reason_codes == ("OPTIONS_REALTIME_DATA_REQUIRED",)


# ===========================================================================
# The governor gates the scan
# ===========================================================================


class TestGovernorWiring:
    def test_a_healthy_portfolio_approves_the_governor(self) -> None:
        report = run_scan(
            FakeBroker(),
            symbol="SPY",
            market_data=FakeMarketDataPort(),
            portfolio=FakePortfolioPort(),
        )
        assert report.governor is not None
        assert report.governor.approved, report.governor.describe()

    def test_a_concentrated_book_refuses(self) -> None:
        """SPY already consuming most of its per-underlying allowance means the
        next SPY position is refused however good the structure is."""
        crowded = FakePortfolioPort(
            net_liquidation="10000",
            positions=(
                PositionExposure(
                    underlying="SPY",
                    buying_power_reserved=D("900"),
                    maximum_loss=D("900"),
                ),
            ),
        )
        report = run_scan(
            FakeBroker(), symbol="SPY", market_data=FakeMarketDataPort(), portfolio=crowded
        )
        assert report.governor is not None
        assert not report.governor.approved
        assert "GOVERNOR_UNDERLYING_CONCENTRATION_EXCEEDED" in report.refusal_codes

    def test_a_broken_portfolio_adapter_fails_closed_and_says_so(self) -> None:
        """An adapter that raises must look different in the report from a port
        that was never supplied -- both refuse, and the operator can tell which."""
        report = run_scan(
            FakeBroker(),
            symbol="SPY",
            market_data=FakeMarketDataPort(),
            portfolio=ExplodingPortfolioPort(),
        )
        assert report.governor is not None
        assert not report.governor.approved
        assert "GOVERNOR_PORTFOLIO_STATE_UNAVAILABLE" in report.refusal_codes
        assert any("portfolio snapshot unavailable" in e for e in report.errors)

    def test_the_governor_sees_the_same_margin_the_candidate_check_did(self) -> None:
        """Both layers must agree on what one position reserves."""
        report = run_scan(
            FakeBroker(),
            symbol="SPY",
            market_data=FakeMarketDataPort(),
            portfolio=FakePortfolioPort(),
        )
        assert report.risk is not None and report.governor is not None
        candidate_side = report.risk.result_for(CHECK_BROKER_MARGIN).observed
        governor_side = report.governor.result_for("incremental_bpr").observed
        assert candidate_side == governor_side == D("500.0")


# ===========================================================================
# The tradeable interlock
# ===========================================================================


class TestTradeableInterlock:
    def test_the_shadow_scan_is_never_tradeable_even_when_everything_passes(
        self,
    ) -> None:
        """Live data, a healthy book, both layers approving -- and still NO,
        because the strikes were not delta-selected. When delta selection lands
        this test must be revisited deliberately, not discovered by surprise.

        The caps are raised for this test alone. The shadow selector builds a
        25-wide spread whose 1750 maximum loss is far over the conservative
        default of 500 -- refusing it is correct, and is asserted elsewhere. Here
        the point is that everything else passing is still not enough.
        """
        permissive = RiskPolicy(
            max_defined_loss_per_position=D("2000"),
            max_stress_loss_per_position=D("2000"),
        )
        report = run_scan(
            FakeBroker(),
            symbol="SPY",
            policy=permissive,
            market_data=FakeMarketDataPort(),
            portfolio=FakePortfolioPort(),
        )
        assert report.risk is not None and report.governor is not None
        assert report.risk.approved, report.risk.describe()
        assert report.governor.approved, report.governor.describe()

        assert report.selection_method is SelectionMethod.SHADOW_STRIKE_OFFSET
        assert report.tradeable is False
        assert any("delta-selected" in blocker for blocker in report.blockers)

    def test_a_governor_refusal_alone_blocks(self) -> None:
        report = run_scan(
            FakeBroker(),
            symbol="SPY",
            market_data=FakeMarketDataPort(),
            portfolio=None,
        )
        assert report.tradeable is False
        assert report.governor is not None and not report.governor.approved

    def test_the_cli_supplies_both_ports_and_the_policy(self) -> None:
        """A regression that unwired the adapters would leave the scan refusing
        with OPTIONS_NO_MARKET_DATA_SNAPSHOT forever. That is still safe -- but
        it would silently stop exercising the entitlement gate against the real
        broker, and the report would stop distinguishing 'we asked and the data
        was delayed' from 'we never asked'."""
        import ast
        import inspect
        import textwrap

        import engine.cli as cli_module

        tree = ast.parse(textwrap.dedent(inspect.getsource(cli_module.cmd_options_scan)))
        keywords = {
            keyword.arg
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
        }
        assert {"policy", "market_data", "portfolio"} <= keywords

        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "IBKRLiveMarketDataAdapter" in names
        assert "IBKRPortfolioStateAdapter" in names

    def test_policy_thresholds_reach_the_scan(self) -> None:
        """A configuration-driven cap must actually bind inside a real scan, not
        only in a unit test of the check that reads it."""
        strict = RiskPolicy(max_broker_margin_per_position=D("1"))
        report = run_scan(
            FakeBroker(),
            symbol="SPY",
            policy=strict,
            market_data=FakeMarketDataPort(),
            portfolio=FakePortfolioPort(),
        )
        assert "OPTIONS_BROKER_MARGIN_EXCEEDED" in report.refusal_codes
        assert report.tradeable is False
