"""IV Rank, chain selection, combo construction and the what-if normalizer.

These are the three capabilities that work without a market-data subscription,
so they are the parts that can be built and proven now.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import uuid4

import pytest

from engine.options.chain import (
    ContractStatus,
    narrow_strikes,
    select_expiration,
)
from engine.options.domain import (
    OptionLegIntent,
    OptionRight,
    OptionStrategyIntent,
    OrderAction,
    PriceEffect,
    StrategyAction,
    StrategyType,
)
from engine.options.execution import IB_UNSET, build_combo, what_if
from engine.options.ivrank import (
    METHODOLOGY_VERSION,
    SOURCE_IBKR_OPTION_IV,
    IVObservation,
    build_iv_rank,
    calculate_iv_rank,
    observations_from_bars,
)
from engine.options.scan import ScanReport, SelectionMethod

D = Decimal
TODAY = dt.date(2026, 7, 29)
NOW = dt.datetime(2026, 7, 29, 13, 0, tzinfo=dt.timezone.utc)


def ivs(values: list[str], *, start: dt.date = dt.date(2025, 8, 1)) -> list[IVObservation]:
    return [
        IVObservation(on=start + dt.timedelta(days=i), implied_volatility=D(v))
        for i, v in enumerate(values)
    ]


# ===========================================================================
# IV Rank
# ===========================================================================


class TestIVRankFormula:
    def test_midpoint_of_the_range_is_fifty(self) -> None:
        assert calculate_iv_rank(D("0.20"), D("0.10"), D("0.30")) == D("50")

    def test_at_the_low_is_zero(self) -> None:
        assert calculate_iv_rank(D("0.10"), D("0.10"), D("0.30")) == D("0")

    def test_at_the_high_is_one_hundred(self) -> None:
        assert calculate_iv_rank(D("0.30"), D("0.10"), D("0.30")) == D("100")

    def test_flat_range_is_undefined_not_zero(self) -> None:
        """A year of identical IV has no rank. Zero would read as 'very low'."""
        assert calculate_iv_rank(D("0.20"), D("0.20"), D("0.20")) is None

    def test_inverted_range_is_undefined(self) -> None:
        assert calculate_iv_rank(D("0.20"), D("0.30"), D("0.10")) is None

    def test_nonpositive_current_is_undefined(self) -> None:
        assert calculate_iv_rank(D("0"), D("0.10"), D("0.30")) is None

    def test_result_is_clamped(self) -> None:
        assert calculate_iv_rank(D("0.50"), D("0.10"), D("0.30")) == D("100")
        assert calculate_iv_rank(D("0.05"), D("0.10"), D("0.30")) == D("0")


class TestIVRankPipeline:
    def test_computes_from_a_full_year(self) -> None:
        values = [f"0.{10 + (i % 21):02d}" for i in range(252)]
        metric = build_iv_rank("SPY", ivs(values), calculated_at=NOW)
        assert metric.iv_rank is not None
        assert metric.observation_count == 252
        assert metric.is_usable

    def test_records_source_and_methodology(self) -> None:
        """So a cached or journalled metric can be checked, not assumed."""
        metric = build_iv_rank("SPY", ivs([f"0.{10 + i % 21:02d}" for i in range(252)]),
                               calculated_at=NOW)
        assert metric.source == SOURCE_IBKR_OPTION_IV
        assert metric.methodology_version == METHODOLOGY_VERSION

    def test_current_is_the_last_observation_of_the_same_series(self) -> None:
        values = [f"0.{10 + i % 21:02d}" for i in range(252)]
        metric = build_iv_rank("SPY", ivs(values), calculated_at=NOW)
        assert metric.current_iv == D(values[-1])

    def test_too_few_observations_degrades(self) -> None:
        metric = build_iv_rank("SPY", ivs(["0.20", "0.25"]), calculated_at=NOW)
        assert metric.iv_rank is None
        assert metric.degraded_reason is not None
        assert not metric.is_usable

    def test_no_observations_degrades(self) -> None:
        metric = build_iv_rank("SPY", [], calculated_at=NOW)
        assert metric.iv_rank is None
        assert metric.observation_count == 0

    def test_flat_history_degrades_with_a_reason(self) -> None:
        metric = build_iv_rank("SPY", ivs(["0.20"] * 100), calculated_at=NOW)
        assert metric.iv_rank is None
        assert "flat" in (metric.degraded_reason or "")

    def test_window_keeps_the_most_recent(self) -> None:
        values = [f"0.{10 + i % 21:02d}" for i in range(400)]
        metric = build_iv_rank("SPY", ivs(values), calculated_at=NOW, window=252)
        assert metric.observation_count == 252
        assert metric.current_iv == D(values[-1])

    def test_meets_fails_closed_when_unavailable(self) -> None:
        """An unusable metric must never satisfy the entry filter."""
        metric = build_iv_rank("SPY", [], calculated_at=NOW)
        assert not metric.meets(D("50"))
        assert not metric.meets(D("0"))

    def test_meets_is_inclusive_at_the_threshold(self) -> None:
        values = ["0.10"] * 100 + ["0.30"] * 100 + ["0.20"]
        metric = build_iv_rank("SPY", ivs(values), calculated_at=NOW)
        assert metric.iv_rank == D("50")
        assert metric.meets(D("50"))


class TestObservationsFromBars:
    def test_rejects_nonpositive_and_nonfinite(self) -> None:
        class Bar:
            def __init__(self, date, close):
                self.date = date
                self.close = close

        bars = [
            Bar(dt.date(2026, 1, 2), 0.20),
            Bar(dt.date(2026, 1, 3), 0.0),
            Bar(dt.date(2026, 1, 4), float("nan")),
            Bar(dt.date(2026, 1, 5), -1.0),
            Bar(dt.date(2026, 1, 6), IB_UNSET),
            Bar(dt.date(2026, 1, 7), 0.25),
        ]
        observations = observations_from_bars(bars)
        assert [str(o.implied_volatility) for o in observations] == ["0.2", "0.25"]

    def test_accepts_ibkr_string_dates(self) -> None:
        class Bar:
            date = "20260102"
            close = 0.2

        assert observations_from_bars([Bar()])[0].on == dt.date(2026, 1, 2)

    def test_sorts_by_date(self) -> None:
        class Bar:
            def __init__(self, date, close):
                self.date = date
                self.close = close

        bars = [Bar(dt.date(2026, 3, 1), 0.3), Bar(dt.date(2026, 1, 1), 0.1)]
        assert [o.on for o in observations_from_bars(bars)] == [
            dt.date(2026, 1, 1),
            dt.date(2026, 3, 1),
        ]


# ===========================================================================
# Expiry and strike selection
# ===========================================================================


def expiry_at(days: int) -> str:
    return (TODAY + dt.timedelta(days=days)).strftime("%Y%m%d")


class TestExpirySelection:
    def test_picks_nearest_to_target(self) -> None:
        chosen = select_expiration(
            [expiry_at(38), expiry_at(46), expiry_at(53)], today=TODAY
        )
        assert chosen is not None
        assert chosen.dte == 46

    def test_ignores_expirations_outside_the_window(self) -> None:
        chosen = select_expiration(
            [expiry_at(7), expiry_at(30), expiry_at(44), expiry_at(90)], today=TODAY
        )
        assert chosen is not None
        assert chosen.dte == 44

    def test_returns_none_when_nothing_is_in_the_window(self) -> None:
        assert select_expiration([expiry_at(7), expiry_at(120)], today=TODAY) is None

    def test_tie_breaks_toward_the_longer_dated(self) -> None:
        """At equal distance from 45, the further expiry decays more slowly and
        leaves more room before the 21-DTE management point."""
        chosen = select_expiration([expiry_at(40), expiry_at(50)], today=TODAY)
        assert chosen is not None
        assert chosen.dte == 50

    def test_malformed_expirations_are_skipped_not_fatal(self) -> None:
        chosen = select_expiration(["not-a-date", "", expiry_at(45)], today=TODAY)
        assert chosen is not None
        assert chosen.dte == 45

    def test_counts_are_reported(self) -> None:
        chosen = select_expiration(
            [expiry_at(7), expiry_at(40), expiry_at(50)], today=TODAY
        )
        assert chosen is not None
        assert chosen.considered == 3
        assert chosen.in_window == 2


class TestNarrowStrikes:
    def test_centres_on_the_reference_price(self) -> None:
        strikes = [D(str(s)) for s in range(400, 601, 5)]
        window = narrow_strikes(strikes, reference_price=D("500"), width=6)
        assert D("500") in window
        assert len(window) <= 8

    def test_falls_back_to_the_median_without_a_price(self) -> None:
        strikes = [D(str(s)) for s in range(400, 601, 5)]
        window = narrow_strikes(strikes, reference_price=None, width=4)
        assert window
        assert D("500") in window

    def test_empty_in_empty_out(self) -> None:
        assert narrow_strikes([], reference_price=D("500"), width=4) == []


# ===========================================================================
# Combo construction and the what-if
# ===========================================================================


def spread(credit: str = "1.50") -> OptionStrategyIntent:
    legs = (
        OptionLegIntent(
            con_id=1001,
            symbol="SPY",
            expiration=dt.date(2026, 9, 18),
            strike=D("500"),
            right=OptionRight.PUT,
            action=OrderAction.SELL,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
        OptionLegIntent(
            con_id=1002,
            symbol="SPY",
            expiration=dt.date(2026, 9, 18),
            strike=D("495"),
            right=OptionRight.PUT,
            action=OrderAction.BUY,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
    )
    loss = (D("5") - D(credit)) * 100
    return OptionStrategyIntent(
        strategy_id=uuid4(),
        strategy_type=StrategyType.PUT_CREDIT_SPREAD,
        strategy_action=StrategyAction.OPEN,
        underlying="SPY",
        quantity=1,
        legs=legs,
        expiration=dt.date(2026, 9, 18),
        limit_price=D(credit),
        price_effect=PriceEffect.CREDIT,
        maximum_loss_per_contract=loss,
        configuration_version="test",
        created_at=NOW,
    )


class TestBuildCombo:
    def test_a_credit_is_a_buy_at_a_negative_limit(self) -> None:
        """SELL at a positive price inverts the leg actions and IBKR rejects it
        with error 201, 'riskless combination'."""
        _, order = build_combo(spread("1.50"))
        assert order.action == "BUY"
        assert order.lmtPrice == -1.5

    def test_tif_is_set_explicitly(self) -> None:
        """Left blank, TWS fills it from a preset and error 10349 ends the
        request, so whatIfOrder returns [] instead of an OrderState."""
        _, order = build_combo(spread())
        assert order.tif == "DAY"

    def test_leg_actions_describe_the_position_wanted(self) -> None:
        bag, _ = build_combo(spread())
        assert [leg.action for leg in bag.comboLegs] == ["SELL", "BUY"]
        assert [leg.conId for leg in bag.comboLegs] == [1001, 1002]

    def test_the_bag_carries_the_underlying_and_currency(self) -> None:
        bag, _ = build_combo(spread())
        assert bag.symbol == "SPY"
        assert bag.currency == "USD"

    def test_the_strategy_id_is_on_the_order_for_reconciliation(self) -> None:
        intent = spread()
        _, order = build_combo(intent)
        assert order.orderRef == str(intent.strategy_id)


class FakeState:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeIB:
    def __init__(self, state):
        self.state = state
        self.what_ifs: list[tuple] = []

    def whatIfOrder(self, contract, order):  # noqa: N802
        self.what_ifs.append((contract, order))
        return self.state


class TestWhatIf:
    def test_normalizes_a_good_response(self) -> None:
        ib = FakeIB(
            FakeState(
                initMarginChange=500.0,
                maintMarginChange=500.0,
                equityWithLoanChange=-2.0,
                commission=1.3,
            )
        )
        result = what_if(ib, spread(), observed_at=NOW)
        assert result.accepted
        assert result.initial_margin_change == D("500.0")
        assert result.has_required_fields

    def test_dbl_max_commission_is_absent_not_a_number(self) -> None:
        """IBKR's 'does not apply' marker turned up as the commission on the
        very first options what-if. It is finite."""
        ib = FakeIB(
            FakeState(
                initMarginChange=500.0,
                maintMarginChange=500.0,
                equityWithLoanChange=0.0,
                commission=IB_UNSET,
            )
        )
        result = what_if(ib, spread(), observed_at=NOW)
        assert result.commission is None
        assert result.accepted

    def test_empty_response_is_a_rejection(self) -> None:
        result = what_if(FakeIB([]), spread(), observed_at=NOW)
        assert not result.accepted
        assert "no order state" in (result.rejection_reason or "")

    def test_missing_margin_field_is_a_rejection_not_a_zero(self) -> None:
        """An unknown margin impact assumed negligible is how an account gets a
        position it cannot carry."""
        ib = FakeIB(FakeState(initMarginChange=500.0, commission=1.0))
        result = what_if(ib, spread(), observed_at=NOW)
        assert not result.accepted
        assert "omitted a required margin field" in (result.rejection_reason or "")

    def test_nan_margin_is_a_rejection(self) -> None:
        ib = FakeIB(
            FakeState(
                initMarginChange=float("nan"),
                maintMarginChange=500.0,
                commission=1.0,
            )
        )
        assert not what_if(ib, spread(), observed_at=NOW).accepted

    def test_warning_text_is_carried_through(self) -> None:
        ib = FakeIB(
            FakeState(
                initMarginChange=500.0,
                maintMarginChange=500.0,
                commission=1.0,
                warningText="account is close to margin",
            )
        )
        assert what_if(ib, spread(), observed_at=NOW).warning_text is not None

    def test_what_if_does_not_place_an_order(self) -> None:
        ib = FakeIB(FakeState(initMarginChange=500.0, maintMarginChange=500.0))
        what_if(ib, spread(), observed_at=NOW)
        assert not hasattr(ib, "placed")
        assert len(ib.what_ifs) == 1


# ===========================================================================
# Scan report honesty
# ===========================================================================


class TestScanReport:
    def test_shadow_selection_is_never_tradeable(self) -> None:
        report = ScanReport(symbol="SPY", started_at=NOW)
        report.selection_method = SelectionMethod.SHADOW_STRIKE_OFFSET
        assert report.tradeable is False

    def test_describe_states_the_selection_method(self) -> None:
        report = ScanReport(symbol="SPY", started_at=NOW)
        rendered = report.describe()
        assert "SHADOW_STRIKE_OFFSET" in rendered
        assert "TRADEABLE        NO" in rendered

    def test_record_is_json_safe(self) -> None:
        import json

        report = ScanReport(symbol="SPY", started_at=NOW, finished_at=NOW)
        report.iv_rank = build_iv_rank("SPY", ivs(["0.2"] * 100), calculated_at=NOW)
        encoded = json.dumps(report.to_record())
        assert "SHADOW_STRIKE_OFFSET" in encoded

    def test_blockers_are_surfaced(self) -> None:
        report = ScanReport(symbol="SPY", started_at=NOW)
        report.blockers.append("IV Rank 26 is below the 50 entry filter")
        assert "IV Rank 26" in report.describe()


class TestContractStatus:
    def test_qualifying_is_not_eligibility(self) -> None:
        """A contract does not become tradeable by qualifying successfully."""
        assert ContractStatus.QUALIFIED is not ContractStatus.STRATEGY_ELIGIBLE
        assert [s.value for s in ContractStatus] == [
            "DISCOVERED",
            "QUALIFIED",
            "QUOTED",
            "GREEKS_VALID",
            "STRATEGY_ELIGIBLE",
        ]
