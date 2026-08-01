"""The portfolio governor and the snapshot it decides against.

Every test here names a way the book could be over-committed while each
individual position still looked survivable. The governor's whole reason to
exist is that per-candidate checks cannot see the other five trades, so the
cases that matter are the ones where one check refuses and the other five pass.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import uuid4

import pytest

from engine.errors import InvalidPortfolioStateError
from engine.options.domain import (
    OptionLegIntent,
    OptionRight,
    OptionStrategyIntent,
    OrderAction,
    PriceEffect,
    StrategyAction,
    StrategyType,
)
from engine.options.execution import MarginAssessment
from engine.options.governor import (
    CHECK_CORRELATION_CONCENTRATION,
    CHECK_INCREMENTAL_BPR,
    CHECK_PORTFOLIO_STATE,
    CHECK_SECTOR_CONCENTRATION,
    CHECK_TOTAL_BPR,
    CHECK_UNDERLYING_CONCENTRATION,
    REQUIRED_GOVERNOR_CHECKS,
    GovernorRefusalReason,
    GovernorVerdict,
    PortfolioGovernor,
)
from engine.options.policy import RiskPolicy
from engine.options.portfolio import PortfolioSnapshot, PositionExposure
from engine.options.risk import CheckResult, required_buying_power

D = Decimal
NOW = dt.datetime(2026, 7, 29, 13, 0, tzinfo=dt.timezone.utc)

# The default policy's caps, restated so a test reads as arithmetic rather than
# as a lookup. Against a net liquidation of 100000 these are the money figures.
NET_LIQ = "100000"
INCREMENTAL_CAP = D("5000")  # 0.05
TOTAL_CAP = D("35000")  # 0.35
UNDERLYING_CAP = D("10000")  # 0.10
SECTOR_CAP = D("15000")  # 0.15
CORRELATION_CAP = D("20000")  # 0.20

#: Every refusal code any test in this module actually observed. Read by
#: :class:`TestRefusalReasonCoverage` as a cross-check that the codes asserted
#: are real enum values and not typed-out strings that drifted.
PRODUCED_REASON_CODES: set[str] = set()


# ---------------------------------------------------------------------------
# Builders -- no fixtures, so every test states its own whole world
# ---------------------------------------------------------------------------


def spread(underlying: str = "SPY", credit: str = "1.50") -> OptionStrategyIntent:
    legs = (
        OptionLegIntent(
            con_id=1001,
            symbol=underlying,
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
            symbol=underlying,
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
        underlying=underlying,
        quantity=1,
        legs=legs,
        expiration=dt.date(2026, 9, 18),
        limit_price=D(credit),
        price_effect=PriceEffect.CREDIT,
        maximum_loss_per_contract=loss,
        configuration_version="test",
        created_at=NOW,
    )


def pos(underlying: str, bpr: str, maximum_loss: str | None = None) -> PositionExposure:
    return PositionExposure(
        underlying=underlying,
        buying_power_reserved=D(bpr),
        maximum_loss=D(maximum_loss if maximum_loss is not None else bpr),
    )


def snap(
    *,
    net_liquidation: str = NET_LIQ,
    positions: tuple[PositionExposure, ...] = (),
    reported: str | None = None,
    as_of: dt.datetime | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        as_of=as_of if as_of is not None else NOW - dt.timedelta(seconds=10),
        net_liquidation=D(net_liquidation),
        positions=positions,
        reported_buying_power_reserved=D(reported) if reported is not None else None,
    )


def margin(
    initial: str | None = "500",
    maintenance: str | None = "500",
    *,
    accepted: bool = True,
    rejection_reason: str | None = None,
) -> MarginAssessment:
    return MarginAssessment(
        accepted=accepted,
        observed_at=NOW,
        initial_margin_change=D(initial) if initial is not None else None,
        maintenance_margin_change=D(maintenance) if maintenance is not None else None,
        rejection_reason=rejection_reason,
    )


def governor(policy: RiskPolicy | None = None) -> PortfolioGovernor:
    return PortfolioGovernor(policy if policy is not None else RiskPolicy())


def code_for(verdict: GovernorVerdict, check: str) -> str | None:
    """The refusal code one check produced, recorded for the coverage test."""
    reason_code = verdict.result_for(check).reason_code
    if reason_code is not None:
        PRODUCED_REASON_CODES.add(reason_code)
    return reason_code


def passing(check: str) -> CheckResult:
    return CheckResult(check=check, approved=True, detail="ok")


def complete_results() -> tuple[CheckResult, ...]:
    return tuple(passing(name) for name in REQUIRED_GOVERNOR_CHECKS)


# ===========================================================================
# PositionExposure construction
# ===========================================================================


class TestPositionExposureConstruction:
    def test_a_well_formed_position_is_accepted(self) -> None:
        exposure = pos("SPY", "500", "500")
        assert exposure.buying_power_reserved == D("500")
        assert exposure.normalized_underlying == "SPY"

    def test_negative_buying_power_reserved_is_refused(self) -> None:
        """A negative reservation would subtract from a concentration bucket and
        hide real exposure sitting next to it."""
        with pytest.raises(InvalidPortfolioStateError) as exc:
            PositionExposure(
                underlying="SPY",
                buying_power_reserved=D("-1"),
                maximum_loss=D("500"),
            )
        assert "buying_power_reserved" in str(exc.value)

    def test_negative_maximum_loss_is_refused(self) -> None:
        with pytest.raises(InvalidPortfolioStateError):
            PositionExposure(
                underlying="SPY",
                buying_power_reserved=D("500"),
                maximum_loss=D("-1"),
            )

    def test_non_decimal_amount_is_refused(self) -> None:
        """A float here is a broken adapter, not a rounding nuisance."""
        with pytest.raises(InvalidPortfolioStateError) as exc:
            PositionExposure(
                underlying="SPY",
                buying_power_reserved=500.0,  # type: ignore[arg-type]
                maximum_loss=D("500"),
            )
        assert "Decimal" in str(exc.value)

    def test_empty_underlying_is_refused(self) -> None:
        with pytest.raises(InvalidPortfolioStateError):
            PositionExposure(
                underlying="   ",
                buying_power_reserved=D("500"),
                maximum_loss=D("500"),
            )

    def test_non_uuid_strategy_id_is_refused(self) -> None:
        with pytest.raises(InvalidPortfolioStateError):
            PositionExposure(
                underlying="SPY",
                buying_power_reserved=D("500"),
                maximum_loss=D("500"),
                strategy_id="not-a-uuid",  # type: ignore[arg-type]
            )

    def test_zero_buying_power_reserved_is_allowed(self) -> None:
        """Zero is a real reservation for a position the broker holds nothing
        against; only negatives are nonsense."""
        assert pos("SPY", "0").buying_power_reserved == D("0")


# ===========================================================================
# PortfolioSnapshot construction
# ===========================================================================


class TestPortfolioSnapshotConstruction:
    def test_a_timezone_aware_snapshot_is_accepted(self) -> None:
        snapshot = snap()
        assert snapshot.as_of.tzinfo is not None
        assert snapshot.net_liquidation == D(NET_LIQ)

    def test_naive_as_of_is_refused(self) -> None:
        """A naive timestamp cannot be compared to the decision time, so
        staleness could never be established and every snapshot would look
        fresh."""
        with pytest.raises(InvalidPortfolioStateError) as exc:
            PortfolioSnapshot(
                as_of=dt.datetime(2026, 7, 29, 13, 0),
                net_liquidation=D(NET_LIQ),
            )
        assert "timezone-aware" in str(exc.value)

    def test_non_datetime_as_of_is_refused(self) -> None:
        with pytest.raises(InvalidPortfolioStateError):
            PortfolioSnapshot(
                as_of="2026-07-29T13:00:00Z",  # type: ignore[arg-type]
                net_liquidation=D(NET_LIQ),
            )

    def test_zero_net_liquidation_is_refused(self) -> None:
        """Every portfolio cap is a fraction of net liquidation. Zero makes every
        cap zero, and the refusals it produces would name the wrong cause."""
        with pytest.raises(InvalidPortfolioStateError) as exc:
            PortfolioSnapshot(as_of=NOW, net_liquidation=D("0"))
        assert "greater than zero" in str(exc.value)

    def test_negative_net_liquidation_is_refused(self) -> None:
        with pytest.raises(InvalidPortfolioStateError) as exc:
            PortfolioSnapshot(as_of=NOW, net_liquidation=D("-1"))
        assert "negative" in str(exc.value)

    def test_non_decimal_net_liquidation_is_refused(self) -> None:
        with pytest.raises(InvalidPortfolioStateError) as exc:
            PortfolioSnapshot(as_of=NOW, net_liquidation=100000.0)  # type: ignore[arg-type]
        assert "Decimal" in str(exc.value)

    def test_negative_reported_buying_power_reserved_is_refused(self) -> None:
        with pytest.raises(InvalidPortfolioStateError) as exc:
            PortfolioSnapshot(
                as_of=NOW,
                net_liquidation=D(NET_LIQ),
                reported_buying_power_reserved=D("-1"),
            )
        assert "reported_buying_power_reserved" in str(exc.value)

    def test_a_non_position_in_positions_is_refused(self) -> None:
        """A dict that happens to have the right keys is not a validated
        exposure, and the governor would sum attributes that do not exist."""
        with pytest.raises(InvalidPortfolioStateError) as exc:
            PortfolioSnapshot(
                as_of=NOW,
                net_liquidation=D(NET_LIQ),
                positions=({"underlying": "SPY"},),  # type: ignore[arg-type]
            )
        assert "PositionExposure" in str(exc.value)

    def test_positions_as_a_list_is_refused(self) -> None:
        """A mutable positions container would let the book change under a
        frozen snapshot that a decision was already recorded against."""
        with pytest.raises(InvalidPortfolioStateError) as exc:
            PortfolioSnapshot(
                as_of=NOW,
                net_liquidation=D(NET_LIQ),
                positions=[pos("SPY", "500")],  # type: ignore[arg-type]
            )
        assert "tuple" in str(exc.value)

    def test_age_at_reads_no_clock(self) -> None:
        snapshot = snap(as_of=NOW - dt.timedelta(seconds=42))
        assert snapshot.age_at(NOW) == dt.timedelta(seconds=42)


# ===========================================================================
# The conservative total: max(derived, reported)
# ===========================================================================


class TestTotalBuyingPowerReserved:
    def test_reported_wins_when_it_is_larger(self) -> None:
        """The broker knows about positions the engine did not open. Trusting
        the derived sum would make them invisible to every concentration cap."""
        snapshot = snap(positions=(pos("SPY", "1000"),), reported="9000")
        assert snapshot.derived_buying_power_reserved == D("1000")
        assert snapshot.total_buying_power_reserved == D("9000")

    def test_derived_wins_when_it_is_larger(self) -> None:
        """A broker total below the engine's own sum means the engine holds a
        stale or double-counted view; the larger figure is the safe one."""
        snapshot = snap(
            positions=(pos("SPY", "1000"), pos("AAPL", "2000")), reported="500"
        )
        assert snapshot.derived_buying_power_reserved == D("3000")
        assert snapshot.total_buying_power_reserved == D("3000")

    def test_absent_report_falls_back_to_derived(self) -> None:
        snapshot = snap(positions=(pos("SPY", "1000"), pos("AAPL", "2000")))
        assert snapshot.reported_buying_power_reserved is None
        assert snapshot.total_buying_power_reserved == D("3000")

    def test_equal_figures_agree(self) -> None:
        snapshot = snap(positions=(pos("SPY", "1000"),), reported="1000")
        assert snapshot.total_buying_power_reserved == D("1000")

    def test_no_positions_and_no_report_is_zero(self) -> None:
        assert snap().total_buying_power_reserved == D("0")

    def test_a_reported_excess_reaches_only_the_total_check(self) -> None:
        """Documents actual behaviour, which is narrower than the module
        docstring's rationale claims.

        ``portfolio.py:11-15`` justifies the max() rule by saying the derived sum
        alone "would let positions the engine did not open be invisible to the
        concentration caps". The max() is only consulted by
        ``total_buying_power_reserved``; ``buying_power_for_underlying`` and
        ``buying_power_where`` (``portfolio.py:152-177``) iterate ``positions``
        only, and those are what the underlying, sector and correlation checks
        read (``governor.py:356`` and ``governor.py:429``). So a broker total the
        engine cannot explain still bounds the book's *total* commitment but
        remains invisible to all three concentration buckets.
        """
        snapshot = snap(positions=(pos("SPY", "100"),), reported="30000")
        assert snapshot.total_buying_power_reserved == D("30000")
        assert snapshot.buying_power_for_underlying("SPY") == D("100")
        assert snapshot.buying_power_where(frozenset({"SPY", "AAPL", "MSFT"})) == D("100")

        verdict = governor().evaluate(
            spread("SPY"), snapshot=snapshot, margin=margin(), decision_time=NOW
        )
        assert verdict.result_for(CHECK_TOTAL_BPR).observed == D("30500")
        assert verdict.result_for(CHECK_UNDERLYING_CONCENTRATION).observed == D("600")
        assert verdict.result_for(CHECK_SECTOR_CONCENTRATION).observed == D("600")
        assert verdict.result_for(CHECK_CORRELATION_CONCENTRATION).observed == D("600")
        assert verdict.approved is True

    def test_the_maximum_is_what_the_governor_accumulates(self) -> None:
        """The rule is load-bearing end to end: a reported total the engine
        cannot explain must still consume the total-BPR headroom."""
        snapshot = snap(positions=(pos("SPY", "100"),), reported="34600")
        verdict = governor().evaluate(
            spread(),
            snapshot=snapshot,
            margin=margin(),
            decision_time=NOW,
        )
        assert code_for(verdict, CHECK_TOTAL_BPR) == (
            GovernorRefusalReason.TOTAL_BPR_EXCEEDED.value
        )


# ===========================================================================
# Bucket aggregation
# ===========================================================================


class TestBuyingPowerAggregation:
    def test_for_underlying_sums_only_that_symbol(self) -> None:
        snapshot = snap(
            positions=(pos("SPY", "1000"), pos("AAPL", "2000"), pos("SPY", "500"))
        )
        assert snapshot.buying_power_for_underlying("SPY") == D("1500")
        assert snapshot.buying_power_for_underlying("AAPL") == D("2000")

    def test_for_underlying_is_case_and_whitespace_insensitive(self) -> None:
        """IBKR symbol casing is not guaranteed, and a case mismatch would put a
        position in no bucket at all."""
        snapshot = snap(positions=(pos(" spy ", "1000"), pos("SPY", "500")))
        assert snapshot.buying_power_for_underlying("spy") == D("1500")
        assert snapshot.buying_power_for_underlying("  SpY  ") == D("1500")

    def test_for_underlying_is_zero_for_an_unheld_symbol(self) -> None:
        assert snap(positions=(pos("SPY", "1000"),)).buying_power_for_underlying(
            "MSFT"
        ) == D("0")

    def test_where_sums_across_a_symbol_set(self) -> None:
        snapshot = snap(
            positions=(pos("SPY", "1000"), pos("AAPL", "2000"), pos("MSFT", "3000"))
        )
        assert snapshot.buying_power_where(frozenset({"AAPL", "MSFT"})) == D("5000")

    def test_where_matches_normalized_position_symbols(self) -> None:
        """The set is already normalized by the caller; the positions must be
        normalized on this side or a lowercase fill escapes its sector."""
        snapshot = snap(positions=(pos("aapl", "2000"), pos(" msft", "3000")))
        assert snapshot.buying_power_where(frozenset({"AAPL", "MSFT"})) == D("5000")

    def test_where_is_zero_for_an_empty_set(self) -> None:
        snapshot = snap(positions=(pos("SPY", "1000"),))
        assert snapshot.buying_power_where(frozenset()) == D("0")

    def test_underlyings_is_the_normalized_symbol_set(self) -> None:
        snapshot = snap(positions=(pos("spy", "1000"), pos("SPY", "500"), pos("aapl", "1")))
        assert snapshot.underlyings == frozenset({"SPY", "AAPL"})


# ===========================================================================
# The fail-closed portfolio-state precondition
# ===========================================================================


class TestPortfolioStateCheck:
    def test_missing_snapshot_refuses_portfolio_state_unavailable(self) -> None:
        """An unknown book is the strongest reason to refuse, not a reason to
        skip the portfolio checks."""
        verdict = governor().evaluate(
            spread(), snapshot=None, margin=margin(), decision_time=NOW
        )
        assert code_for(verdict, CHECK_PORTFOLIO_STATE) == (
            GovernorRefusalReason.PORTFOLIO_STATE_UNAVAILABLE.value
        )

    def test_missing_snapshot_also_refuses_every_bpr_and_concentration_check(
        self,
    ) -> None:
        verdict = governor().evaluate(
            spread(), snapshot=None, margin=margin(), decision_time=NOW
        )
        expected = GovernorRefusalReason.PORTFOLIO_STATE_UNAVAILABLE.value
        for check in (
            CHECK_INCREMENTAL_BPR,
            CHECK_TOTAL_BPR,
            CHECK_UNDERLYING_CONCENTRATION,
            CHECK_SECTOR_CONCENTRATION,
            CHECK_CORRELATION_CONCENTRATION,
        ):
            assert code_for(verdict, check) == expected

    def test_old_snapshot_refuses_portfolio_state_stale(self) -> None:
        """Sizing against a minute-old book is sizing against a book that may
        already hold the position being considered."""
        verdict = governor().evaluate(
            spread(),
            snapshot=snap(as_of=NOW - dt.timedelta(seconds=61)),
            margin=margin(),
            decision_time=NOW,
        )
        assert code_for(verdict, CHECK_PORTFOLIO_STATE) == (
            GovernorRefusalReason.PORTFOLIO_STATE_STALE.value
        )

    def test_age_exactly_at_the_limit_is_accepted(self) -> None:
        """The comparison is strictly greater-than; a snapshot exactly at the
        limit is still usable."""
        policy = RiskPolicy()
        verdict = governor(policy).evaluate(
            spread(),
            snapshot=snap(as_of=NOW - policy.portfolio_snapshot_maximum_age),
            margin=margin(),
            decision_time=NOW,
        )
        assert verdict.result_for(CHECK_PORTFOLIO_STATE).approved

    def test_one_microsecond_over_the_limit_is_refused(self) -> None:
        policy = RiskPolicy()
        over = policy.portfolio_snapshot_maximum_age + dt.timedelta(microseconds=1)
        verdict = governor(policy).evaluate(
            spread(),
            snapshot=snap(as_of=NOW - over),
            margin=margin(),
            decision_time=NOW,
        )
        assert code_for(verdict, CHECK_PORTFOLIO_STATE) == (
            GovernorRefusalReason.PORTFOLIO_STATE_STALE.value
        )

    def test_unclassified_open_position_refuses_portfolio_position_unclassified(
        self,
    ) -> None:
        """A position in no sector bucket is invisible to every concentration cap
        while still consuming real buying power, so the whole evaluation is
        refused rather than reporting headroom that does not exist."""
        verdict = governor().evaluate(
            spread(),
            snapshot=snap(positions=(pos("SPY", "500"), pos("XYZ", "500"))),
            margin=margin(),
            decision_time=NOW,
        )
        result = verdict.result_for(CHECK_PORTFOLIO_STATE)
        assert code_for(verdict, CHECK_PORTFOLIO_STATE) == (
            GovernorRefusalReason.PORTFOLIO_POSITION_UNCLASSIFIED.value
        )
        assert "XYZ" in result.detail

    def test_a_position_classified_in_only_one_map_is_still_unclassified(self) -> None:
        """Sector and correlation group are both required; one without the other
        leaves the other bucket unbounded."""
        policy = RiskPolicy(
            sectors=(("SPY", "BROAD_MARKET"), ("XYZ", "TECHNOLOGY")),
            correlation_groups=(("SPY", "US_LARGE_CAP"),),
        )
        verdict = governor(policy).evaluate(
            spread(),
            snapshot=snap(positions=(pos("XYZ", "500"),)),
            margin=margin(),
            decision_time=NOW,
        )
        assert code_for(verdict, CHECK_PORTFOLIO_STATE) == (
            GovernorRefusalReason.PORTFOLIO_POSITION_UNCLASSIFIED.value
        )

    def test_a_fully_classified_fresh_snapshot_passes(self) -> None:
        verdict = governor().evaluate(
            spread(),
            snapshot=snap(positions=(pos("SPY", "500"), pos("AAPL", "500"))),
            margin=margin(),
            decision_time=NOW,
        )
        assert verdict.result_for(CHECK_PORTFOLIO_STATE).approved


# ===========================================================================
# The broker's number, or nothing
# ===========================================================================


class TestCandidateBuyingPowerUnknown:
    def test_absent_margin_refuses_candidate_bpr_unknown(self) -> None:
        """No what-if means no idea what the position reserves, and an unknown
        reservation is not a small one."""
        verdict = governor().evaluate(
            spread(), snapshot=snap(), margin=None, decision_time=NOW
        )
        assert code_for(verdict, CHECK_INCREMENTAL_BPR) == (
            GovernorRefusalReason.CANDIDATE_BPR_UNKNOWN.value
        )

    def test_a_rejected_what_if_is_a_bpr_unknown(self) -> None:
        verdict = governor().evaluate(
            spread(),
            snapshot=snap(),
            margin=margin(accepted=False, rejection_reason="riskless combination"),
            decision_time=NOW,
        )
        assert code_for(verdict, CHECK_TOTAL_BPR) == (
            GovernorRefusalReason.CANDIDATE_BPR_UNKNOWN.value
        )

    def test_an_accepted_what_if_missing_a_margin_field_is_a_bpr_unknown(self) -> None:
        """A partial OrderState is not a zero-margin success."""
        verdict = governor().evaluate(
            spread(),
            snapshot=snap(),
            margin=margin(initial="500", maintenance=None),
            decision_time=NOW,
        )
        assert code_for(verdict, CHECK_UNDERLYING_CONCENTRATION) == (
            GovernorRefusalReason.CANDIDATE_BPR_UNKNOWN.value
        )

    def test_unknown_bpr_refuses_all_four_money_checks(self) -> None:
        verdict = governor().evaluate(
            spread(), snapshot=snap(), margin=None, decision_time=NOW
        )
        expected = GovernorRefusalReason.CANDIDATE_BPR_UNKNOWN.value
        for check in (
            CHECK_INCREMENTAL_BPR,
            CHECK_TOTAL_BPR,
            CHECK_UNDERLYING_CONCENTRATION,
            CHECK_SECTOR_CONCENTRATION,
            CHECK_CORRELATION_CONCENTRATION,
        ):
            assert code_for(verdict, check) == expected
        assert verdict.result_for(CHECK_PORTFOLIO_STATE).approved

    def test_the_governor_sizes_on_the_larger_of_the_two_margin_figures(self) -> None:
        """Same function as the candidate check, so the two layers cannot
        disagree about what one position reserves."""
        assessment = margin(initial="600", maintenance="500")
        assert required_buying_power(assessment) == D("600")
        verdict = governor().evaluate(
            spread(), snapshot=snap(), margin=assessment, decision_time=NOW
        )
        assert verdict.result_for(CHECK_INCREMENTAL_BPR).observed == D("600")


# ===========================================================================
# Buying-power caps
# ===========================================================================


class TestBuyingPowerCaps:
    def test_oversized_position_refuses_incremental_bpr_exceeded(self) -> None:
        """One trade taking more than the per-position share of the account is a
        sizing error no per-candidate cap in dollars would catch."""
        verdict = governor().evaluate(
            spread(),
            snapshot=snap(),
            margin=margin("5000.01", "5000.01"),
            decision_time=NOW,
        )
        assert code_for(verdict, CHECK_INCREMENTAL_BPR) == (
            GovernorRefusalReason.INCREMENTAL_BPR_EXCEEDED.value
        )
        assert verdict.result_for(CHECK_INCREMENTAL_BPR).limit == INCREMENTAL_CAP

    def test_incremental_exactly_at_the_cap_is_approved(self) -> None:
        verdict = governor().evaluate(
            spread(),
            snapshot=snap(),
            margin=margin("5000", "5000"),
            decision_time=NOW,
        )
        assert verdict.result_for(CHECK_INCREMENTAL_BPR).approved

    def test_full_book_refuses_total_bpr_exceeded(self) -> None:
        """The whole book's committed buying power is the thing no per-candidate
        check can see."""
        verdict = governor().evaluate(
            spread(),
            snapshot=snap(reported="34600"),
            margin=margin(),
            decision_time=NOW,
        )
        assert code_for(verdict, CHECK_TOTAL_BPR) == (
            GovernorRefusalReason.TOTAL_BPR_EXCEEDED.value
        )
        assert verdict.result_for(CHECK_TOTAL_BPR).limit == TOTAL_CAP

    def test_total_exactly_at_the_cap_is_approved(self) -> None:
        verdict = governor().evaluate(
            spread(),
            snapshot=snap(reported="34500"),
            margin=margin(),
            decision_time=NOW,
        )
        result = verdict.result_for(CHECK_TOTAL_BPR)
        assert result.observed == TOTAL_CAP
        assert result.approved

    def test_total_one_cent_over_the_cap_is_refused(self) -> None:
        verdict = governor().evaluate(
            spread(),
            snapshot=snap(reported="34500.01"),
            margin=margin(),
            decision_time=NOW,
        )
        result = verdict.result_for(CHECK_TOTAL_BPR)
        assert result.observed == D("35000.01")
        assert code_for(verdict, CHECK_TOTAL_BPR) == (
            GovernorRefusalReason.TOTAL_BPR_EXCEEDED.value
        )


# ===========================================================================
# Concentration -- name, sector, correlation group
# ===========================================================================


class TestUnderlyingConcentration:
    def test_stacked_name_refuses_underlying_concentration_exceeded(self) -> None:
        """Four small spreads on one ticker is one large position wearing four
        strategy ids."""
        verdict = governor().evaluate(
            spread("SPY"),
            snapshot=snap(positions=(pos("SPY", "9800"),)),
            margin=margin(),
            decision_time=NOW,
        )
        assert code_for(verdict, CHECK_UNDERLYING_CONCENTRATION) == (
            GovernorRefusalReason.UNDERLYING_CONCENTRATION_EXCEEDED.value
        )
        assert verdict.result_for(CHECK_UNDERLYING_CONCENTRATION).limit == UNDERLYING_CAP

    def test_only_the_underlying_check_refuses_when_only_that_bucket_is_full(
        self,
    ) -> None:
        verdict = governor().evaluate(
            spread("SPY"),
            snapshot=snap(positions=(pos("SPY", "9800"),)),
            margin=margin(),
            decision_time=NOW,
        )
        assert verdict.reason_codes == (
            GovernorRefusalReason.UNDERLYING_CONCENTRATION_EXCEEDED.value,
        )

    def test_underlying_exactly_at_the_cap_is_approved(self) -> None:
        verdict = governor().evaluate(
            spread("SPY"),
            snapshot=snap(positions=(pos("SPY", "9500"),)),
            margin=margin(),
            decision_time=NOW,
        )
        result = verdict.result_for(CHECK_UNDERLYING_CONCENTRATION)
        assert result.observed == UNDERLYING_CAP
        assert result.approved

    def test_existing_exposure_is_matched_case_insensitively(self) -> None:
        """A lowercase fill must not escape its own name's cap."""
        verdict = governor().evaluate(
            spread("spy"),
            snapshot=snap(positions=(pos("spy", "9800"),)),
            margin=margin(),
            decision_time=NOW,
        )
        assert verdict.underlying == "SPY"
        assert code_for(verdict, CHECK_UNDERLYING_CONCENTRATION) == (
            GovernorRefusalReason.UNDERLYING_CONCENTRATION_EXCEEDED.value
        )


class TestSectorConcentration:
    def test_unclassified_candidate_refuses_sector_unclassified(self) -> None:
        """An unclassified symbol is one whose concentration nobody has bounded,
        which is not the same as one that is unconstrained."""
        verdict = governor().evaluate(
            spread("XYZ"), snapshot=snap(), margin=margin(), decision_time=NOW
        )
        assert code_for(verdict, CHECK_SECTOR_CONCENTRATION) == (
            GovernorRefusalReason.SECTOR_UNCLASSIFIED.value
        )

    def test_the_sector_check_refuses_before_it_needs_the_margin_number(self) -> None:
        """Classification is asked first, so an unclassified symbol is reported as
        unclassified rather than as an unknown reservation."""
        verdict = governor().evaluate(
            spread("XYZ"), snapshot=snap(), margin=None, decision_time=NOW
        )
        assert code_for(verdict, CHECK_SECTOR_CONCENTRATION) == (
            GovernorRefusalReason.SECTOR_UNCLASSIFIED.value
        )

    def test_full_sector_refuses_sector_concentration_exceeded(self) -> None:
        """Two names in one sector are one bet with two tickers; neither name's
        own cap can see the other."""
        verdict = governor().evaluate(
            spread("AAPL"),
            snapshot=snap(positions=(pos("AAPL", "7400"), pos("MSFT", "7400"))),
            margin=margin(),
            decision_time=NOW,
        )
        assert code_for(verdict, CHECK_SECTOR_CONCENTRATION) == (
            GovernorRefusalReason.SECTOR_CONCENTRATION_EXCEEDED.value
        )
        assert verdict.result_for(CHECK_SECTOR_CONCENTRATION).limit == SECTOR_CAP

    def test_only_the_sector_check_refuses_when_only_that_bucket_is_full(self) -> None:
        verdict = governor().evaluate(
            spread("AAPL"),
            snapshot=snap(positions=(pos("AAPL", "7400"), pos("MSFT", "7400"))),
            margin=margin(),
            decision_time=NOW,
        )
        assert verdict.reason_codes == (
            GovernorRefusalReason.SECTOR_CONCENTRATION_EXCEEDED.value,
        )

    def test_sector_exactly_at_the_cap_is_approved(self) -> None:
        verdict = governor().evaluate(
            spread("AAPL"),
            snapshot=snap(positions=(pos("AAPL", "7250"), pos("MSFT", "7250"))),
            margin=margin(),
            decision_time=NOW,
        )
        result = verdict.result_for(CHECK_SECTOR_CONCENTRATION)
        assert result.observed == SECTOR_CAP
        assert result.approved

    def test_sector_one_cent_over_the_cap_is_refused(self) -> None:
        verdict = governor().evaluate(
            spread("AAPL"),
            snapshot=snap(positions=(pos("AAPL", "7250.01"), pos("MSFT", "7250"))),
            margin=margin(),
            decision_time=NOW,
        )
        result = verdict.result_for(CHECK_SECTOR_CONCENTRATION)
        assert result.observed == D("15000.01")
        assert code_for(verdict, CHECK_SECTOR_CONCENTRATION) == (
            GovernorRefusalReason.SECTOR_CONCENTRATION_EXCEEDED.value
        )

    def test_a_different_sector_does_not_consume_the_bucket(self) -> None:
        """SPY is BROAD_MARKET, so it must not count against TECHNOLOGY."""
        verdict = governor().evaluate(
            spread("AAPL"),
            snapshot=snap(positions=(pos("SPY", "9000"), pos("MSFT", "5000"))),
            margin=margin(),
            decision_time=NOW,
        )
        result = verdict.result_for(CHECK_SECTOR_CONCENTRATION)
        assert result.observed == D("5500")
        assert result.approved


class TestCorrelationConcentration:
    def test_ungrouped_candidate_refuses_correlation_group_unclassified(self) -> None:
        """A symbol with a sector but no correlation group is still a symbol whose
        co-movement with the book nobody has bounded."""
        policy = RiskPolicy(
            sectors=(("SPY", "BROAD_MARKET"), ("XYZ", "TECHNOLOGY")),
            correlation_groups=(("SPY", "US_LARGE_CAP"),),
        )
        verdict = governor(policy).evaluate(
            spread("XYZ"), snapshot=snap(), margin=margin(), decision_time=NOW
        )
        assert verdict.result_for(CHECK_SECTOR_CONCENTRATION).approved
        assert code_for(verdict, CHECK_CORRELATION_CONCENTRATION) == (
            GovernorRefusalReason.CORRELATION_GROUP_UNCLASSIFIED.value
        )

    def test_a_wholly_unknown_candidate_is_unclassified_in_both_maps(self) -> None:
        verdict = governor().evaluate(
            spread("XYZ"), snapshot=snap(), margin=margin(), decision_time=NOW
        )
        assert set(verdict.reason_codes) == {
            GovernorRefusalReason.SECTOR_UNCLASSIFIED.value,
            GovernorRefusalReason.CORRELATION_GROUP_UNCLASSIFIED.value,
        }

    def test_full_group_refuses_correlation_concentration_exceeded(self) -> None:
        """Six correlated trades on six different tickers pass every per-position
        check ever written, which is exactly why this bucket exists."""
        verdict = governor().evaluate(
            spread("SPY"),
            snapshot=snap(
                positions=(
                    pos("SPY", "9000"),
                    pos("AAPL", "5400"),
                    pos("MSFT", "5400"),
                )
            ),
            margin=margin(),
            decision_time=NOW,
        )
        assert code_for(verdict, CHECK_CORRELATION_CONCENTRATION) == (
            GovernorRefusalReason.CORRELATION_CONCENTRATION_EXCEEDED.value
        )
        assert (
            verdict.result_for(CHECK_CORRELATION_CONCENTRATION).limit
            == CORRELATION_CAP
        )

    def test_only_the_correlation_check_refuses_when_only_that_bucket_is_full(
        self,
    ) -> None:
        verdict = governor().evaluate(
            spread("SPY"),
            snapshot=snap(
                positions=(
                    pos("SPY", "9000"),
                    pos("AAPL", "5400"),
                    pos("MSFT", "5400"),
                )
            ),
            margin=margin(),
            decision_time=NOW,
        )
        assert verdict.reason_codes == (
            GovernorRefusalReason.CORRELATION_CONCENTRATION_EXCEEDED.value,
        )

    def test_correlation_exactly_at_the_cap_is_approved(self) -> None:
        verdict = governor().evaluate(
            spread("SPY"),
            snapshot=snap(
                positions=(
                    pos("SPY", "9000"),
                    pos("AAPL", "5250"),
                    pos("MSFT", "5250"),
                )
            ),
            margin=margin(),
            decision_time=NOW,
        )
        result = verdict.result_for(CHECK_CORRELATION_CONCENTRATION)
        assert result.observed == CORRELATION_CAP
        assert result.approved


# ===========================================================================
# Approval, and the shape of the verdict
# ===========================================================================


class TestApproval:
    def test_a_clean_candidate_against_a_clean_book_is_approved(self) -> None:
        verdict = governor().evaluate(
            spread("SPY"),
            snapshot=snap(positions=(pos("AAPL", "1000"),), reported="1200"),
            margin=margin(),
            decision_time=NOW,
        )
        assert verdict.approved is True
        assert verdict.reason_codes == ()
        assert verdict.refusals == ()

    def test_an_approved_verdict_carries_every_required_check(self) -> None:
        verdict = governor().evaluate(
            spread("SPY"), snapshot=snap(), margin=margin(), decision_time=NOW
        )
        assert tuple(r.check for r in verdict.results) == REQUIRED_GOVERNOR_CHECKS
        assert verdict.approved is True

    def test_the_verdict_records_the_snapshot_it_decided_against(self) -> None:
        """A journal line has to be self-contained; the portfolio it was decided
        against has moved by the time anyone reads it."""
        snapshot = snap(positions=(pos("AAPL", "1000"),))
        verdict = governor().evaluate(
            spread("SPY"), snapshot=snapshot, margin=margin(), decision_time=NOW
        )
        assert verdict.snapshot is snapshot
        record = verdict.to_record()
        assert record["portfolio"] is not None
        assert record["approved"] is True
        assert record["policy_version"] == RiskPolicy().version

    def test_the_underlying_is_normalized_on_the_verdict(self) -> None:
        verdict = governor().evaluate(
            spread(" spy "), snapshot=snap(), margin=margin(), decision_time=NOW
        )
        assert verdict.underlying == "SPY"

    def test_describe_names_every_check_in_declared_order(self) -> None:
        verdict = governor().evaluate(
            spread("SPY"), snapshot=snap(), margin=margin(), decision_time=NOW
        )
        text = verdict.describe()
        assert "APPROVED" in text
        for name in REQUIRED_GOVERNOR_CHECKS:
            assert name in text


# ===========================================================================
# Nothing short-circuits
# ===========================================================================


class TestNoShortCircuit:
    def test_every_check_runs_even_with_no_snapshot_and_no_margin(self) -> None:
        """One report must name every problem, or the operator goes round the
        loop once per cause."""
        verdict = governor().evaluate(
            spread("SPY"), snapshot=None, margin=None, decision_time=NOW
        )
        assert len(verdict.results) == len(REQUIRED_GOVERNOR_CHECKS)
        assert {r.check for r in verdict.results} == set(REQUIRED_GOVERNOR_CHECKS)
        assert all(not r.approved for r in verdict.results)
        assert len(verdict.reason_codes) == len(REQUIRED_GOVERNOR_CHECKS)
        assert verdict.approved is False

    def test_two_independent_problems_are_both_reported(self) -> None:
        """Staleness and a full sector are different operator actions, so both
        have to appear in one pass."""
        verdict = governor().evaluate(
            spread("AAPL"),
            snapshot=snap(
                positions=(pos("AAPL", "7400"), pos("MSFT", "7400")),
                as_of=NOW - dt.timedelta(seconds=61),
            ),
            margin=margin(),
            decision_time=NOW,
        )
        assert set(verdict.reason_codes) == {
            GovernorRefusalReason.PORTFOLIO_STATE_STALE.value,
            GovernorRefusalReason.SECTOR_CONCENTRATION_EXCEEDED.value,
        }


# ===========================================================================
# The completeness invariant on GovernorVerdict
# ===========================================================================


class TestGovernorVerdictConstruction:
    def test_a_complete_verdict_builds(self) -> None:
        verdict = GovernorVerdict(
            underlying="SPY",
            evaluated_at=NOW,
            policy_version="test",
            results=complete_results(),
        )
        assert verdict.approved is True

    def test_a_missing_check_is_refused_and_named(self) -> None:
        """Approval-by-omission is what this invariant exists to make
        inexpressible."""
        results = tuple(
            r for r in complete_results() if r.check != CHECK_SECTOR_CONCENTRATION
        )
        with pytest.raises(ValueError) as exc:
            GovernorVerdict(
                underlying="SPY",
                evaluated_at=NOW,
                policy_version="test",
                results=results,
            )
        assert CHECK_SECTOR_CONCENTRATION in str(exc.value)
        assert "incomplete governor verdict" in str(exc.value)

    def test_every_required_check_is_individually_mandatory(self) -> None:
        for omitted in REQUIRED_GOVERNOR_CHECKS:
            results = tuple(r for r in complete_results() if r.check != omitted)
            with pytest.raises(ValueError) as exc:
                GovernorVerdict(
                    underlying="SPY",
                    evaluated_at=NOW,
                    policy_version="test",
                    results=results,
                )
            assert omitted in str(exc.value)

    def test_a_duplicated_check_is_refused(self) -> None:
        """Two verdicts for one check make ``approved`` depend on which was
        written first."""
        results = complete_results() + (passing(CHECK_TOTAL_BPR),)
        with pytest.raises(ValueError) as exc:
            GovernorVerdict(
                underlying="SPY",
                evaluated_at=NOW,
                policy_version="test",
                results=results,
            )
        assert "reported twice" in str(exc.value)

    def test_an_unknown_check_is_refused(self) -> None:
        """A check nobody added to REQUIRED_GOVERNOR_CHECKS is optional, and an
        optional safety check is not one."""
        results = complete_results() + (passing("vibes"),)
        with pytest.raises(ValueError) as exc:
            GovernorVerdict(
                underlying="SPY",
                evaluated_at=NOW,
                policy_version="test",
                results=results,
            )
        assert "vibes" in str(exc.value)

    def test_naive_evaluated_at_is_refused(self) -> None:
        with pytest.raises(ValueError) as exc:
            GovernorVerdict(
                underlying="SPY",
                evaluated_at=dt.datetime(2026, 7, 29, 13, 0),
                policy_version="test",
                results=complete_results(),
            )
        assert "timezone-aware" in str(exc.value)

    def test_results_as_a_list_is_refused(self) -> None:
        with pytest.raises(ValueError) as exc:
            GovernorVerdict(
                underlying="SPY",
                evaluated_at=NOW,
                policy_version="test",
                results=list(complete_results()),  # type: ignore[arg-type]
            )
        assert "tuple" in str(exc.value)

    def test_a_non_check_result_is_refused(self) -> None:
        with pytest.raises(ValueError) as exc:
            GovernorVerdict(
                underlying="SPY",
                evaluated_at=NOW,
                policy_version="test",
                results=complete_results() + ("total_bpr",),  # type: ignore[arg-type]
            )
        assert "CheckResult" in str(exc.value)

    def test_result_for_an_absent_check_raises(self) -> None:
        verdict = GovernorVerdict(
            underlying="SPY",
            evaluated_at=NOW,
            policy_version="test",
            results=complete_results(),
        )
        with pytest.raises(KeyError):
            verdict.result_for("vibes")


# ===========================================================================
# Taxonomy coverage -- a new refusal reason without a test fails the suite
# ===========================================================================


# Each entry runs the real governor and returns the code one check produced.
#
# This replaces an earlier version that matched enum members against *test method
# names*. That check was demonstrably hollow: an independent verifier injected a
# phantom member plus an empty ``def test_phantom_member(self): pass`` and the
# assertion passed. A name proves someone thought about a code; only executing a
# producer proves the code is reachable from production logic.


def _produce_portfolio_state_unavailable() -> str | None:
    verdict = governor().evaluate(
        spread(), snapshot=None, margin=margin(), decision_time=NOW
    )
    return code_for(verdict, CHECK_PORTFOLIO_STATE)


def _produce_portfolio_state_stale() -> str | None:
    verdict = governor().evaluate(
        spread(),
        snapshot=snap(as_of=NOW - dt.timedelta(seconds=120)),
        margin=margin(),
        decision_time=NOW,
    )
    return code_for(verdict, CHECK_PORTFOLIO_STATE)


def _produce_portfolio_position_unclassified() -> str | None:
    verdict = governor().evaluate(
        spread(),
        snapshot=snap(positions=(pos("TSLA", "100"),)),
        margin=margin(),
        decision_time=NOW,
    )
    return code_for(verdict, CHECK_PORTFOLIO_STATE)


def _produce_candidate_bpr_unknown() -> str | None:
    verdict = governor().evaluate(
        spread(), snapshot=snap(), margin=None, decision_time=NOW
    )
    return code_for(verdict, CHECK_INCREMENTAL_BPR)


def _produce_incremental_bpr_exceeded() -> str | None:
    verdict = governor().evaluate(
        spread(),
        snapshot=snap(),
        margin=margin("6000", "6000"),
        decision_time=NOW,
    )
    return code_for(verdict, CHECK_INCREMENTAL_BPR)


def _produce_total_bpr_exceeded() -> str | None:
    # Inflated through the broker-reported total rather than through positions,
    # so the concentration buckets stay empty and only the total cap can bind.
    verdict = governor().evaluate(
        spread(),
        snapshot=snap(reported="34800"),
        margin=margin(),
        decision_time=NOW,
    )
    return code_for(verdict, CHECK_TOTAL_BPR)


def _produce_underlying_concentration_exceeded() -> str | None:
    verdict = governor().evaluate(
        spread("SPY"),
        snapshot=snap(positions=(pos("SPY", "9800"),)),
        margin=margin(),
        decision_time=NOW,
    )
    return code_for(verdict, CHECK_UNDERLYING_CONCENTRATION)


def _produce_sector_unclassified() -> str | None:
    verdict = governor().evaluate(
        spread("TSLA"), snapshot=snap(), margin=margin(), decision_time=NOW
    )
    return code_for(verdict, CHECK_SECTOR_CONCENTRATION)


def _produce_sector_concentration_exceeded() -> str | None:
    # AAPL and MSFT share TECHNOLOGY: 7000 + 8000 + 500 = 15500 over the 15000
    # sector cap, while AAPL alone stays under the 10000 per-underlying cap.
    verdict = governor().evaluate(
        spread("AAPL"),
        snapshot=snap(positions=(pos("AAPL", "7000"), pos("MSFT", "8000"))),
        margin=margin(),
        decision_time=NOW,
    )
    return code_for(verdict, CHECK_SECTOR_CONCENTRATION)


def _produce_correlation_group_unclassified() -> str | None:
    # Classified for sector but not for correlation, so the sector check passes
    # and the correlation check is the one that refuses.
    policy = RiskPolicy(
        sectors=(("SPY", "BROAD_MARKET"), ("TSLA", "AUTOMOTIVE")),
        correlation_groups=(("SPY", "US_LARGE_CAP"),),
    )
    verdict = governor(policy).evaluate(
        spread("TSLA"), snapshot=snap(), margin=margin(), decision_time=NOW
    )
    return code_for(verdict, CHECK_CORRELATION_CONCENTRATION)


def _produce_correlation_concentration_exceeded() -> str | None:
    # All three defaults are US_LARGE_CAP: 9000 + 7000 + 5000 + 500 = 21500 over
    # the 20000 correlation cap, while no single underlying or sector bucket
    # reaches its own limit.
    verdict = governor().evaluate(
        spread("SPY"),
        snapshot=snap(
            positions=(pos("SPY", "9000"), pos("AAPL", "7000"), pos("MSFT", "5000"))
        ),
        margin=margin(),
        decision_time=NOW,
    )
    return code_for(verdict, CHECK_CORRELATION_CONCENTRATION)


GOVERNOR_PRODUCERS = {
    GovernorRefusalReason.PORTFOLIO_STATE_UNAVAILABLE: (
        _produce_portfolio_state_unavailable
    ),
    GovernorRefusalReason.PORTFOLIO_STATE_STALE: _produce_portfolio_state_stale,
    GovernorRefusalReason.PORTFOLIO_POSITION_UNCLASSIFIED: (
        _produce_portfolio_position_unclassified
    ),
    GovernorRefusalReason.CANDIDATE_BPR_UNKNOWN: _produce_candidate_bpr_unknown,
    GovernorRefusalReason.INCREMENTAL_BPR_EXCEEDED: _produce_incremental_bpr_exceeded,
    GovernorRefusalReason.TOTAL_BPR_EXCEEDED: _produce_total_bpr_exceeded,
    GovernorRefusalReason.UNDERLYING_CONCENTRATION_EXCEEDED: (
        _produce_underlying_concentration_exceeded
    ),
    GovernorRefusalReason.SECTOR_UNCLASSIFIED: _produce_sector_unclassified,
    GovernorRefusalReason.SECTOR_CONCENTRATION_EXCEEDED: (
        _produce_sector_concentration_exceeded
    ),
    GovernorRefusalReason.CORRELATION_GROUP_UNCLASSIFIED: (
        _produce_correlation_group_unclassified
    ),
    GovernorRefusalReason.CORRELATION_CONCENTRATION_EXCEEDED: (
        _produce_correlation_concentration_exceeded
    ),
}


class TestRefusalReasonCoverage:
    def test_the_producer_table_covers_the_whole_enum(self) -> None:
        """Adding a member to GovernorRefusalReason without a callable that
        actually produces it fails here. Set equality, not name matching -- an
        empty test named after the member must not be able to satisfy this."""
        assert set(GOVERNOR_PRODUCERS) == set(GovernorRefusalReason)

    @pytest.mark.parametrize(
        "reason", sorted(GovernorRefusalReason, key=lambda r: r.value)
    )
    def test_each_reason_is_actually_produced(
        self, reason: GovernorRefusalReason
    ) -> None:
        """Executes the producer against the real PortfolioGovernor and asserts
        the named check emitted exactly this code."""
        assert GOVERNOR_PRODUCERS[reason]() == reason.value

    def test_every_code_produced_is_a_declared_member(self) -> None:
        """Guards the other direction: an asserted code that is not a real enum
        value would be a typo the string comparison could never catch."""
        declared = {member.value for member in GovernorRefusalReason}
        assert PRODUCED_REASON_CODES <= declared
        assert PRODUCED_REASON_CODES, "no refusal code was observed at all"

    def test_every_code_is_prefixed_for_its_layer(self) -> None:
        for member in GovernorRefusalReason:
            assert member.value.startswith("GOVERNOR_")
            assert member.value == f"GOVERNOR_{member.name}"
