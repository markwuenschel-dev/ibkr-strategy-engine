"""Candidate-level risk checks: approvals, and every way one can refuse.

The coverage test at the bottom is the one that matters most. It maps every
member of :class:`RiskRefusalReason` to a callable that produces it and asserts
the mapping is exactly the enum -- so adding a refusal code without a test that
reaches it fails the suite rather than quietly shipping an unreachable branch.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID, uuid4

import pytest

from engine.errors import MarketDataRefusedError
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
from engine.options.marketdata import (
    MarketDataProvenance,
    MarketDataType,
    OptionGreeks,
    OptionQuote,
    RefusalReason,
    UnderlyingQuote,
)
from engine.options.policy import RiskPolicy
from engine.options.ports import StrategyQuoteSnapshot
from engine.options.risk import (
    CHECK_BROKER_MARGIN,
    CHECK_DEFINED_LOSS,
    CHECK_MARKET_DATA_ENTITLEMENT,
    CHECK_STRESS_LOSS,
    REQUIRED_CHECKS,
    CandidateRiskAssessment,
    CheckResult,
    RiskRefusalReason,
    assess_candidate,
    check_broker_margin,
    check_defined_loss,
    check_market_data_entitlement,
    check_stress_loss,
    required_buying_power,
    stress_loss,
    terminal_loss,
    terminal_profit_per_share,
)

D = Decimal
NOW = dt.datetime(2026, 7, 29, 13, 0, tzinfo=dt.timezone.utc)
EXPIRY = dt.date(2026, 9, 18)
SHORT_CON_ID = 1001
LONG_CON_ID = 1002

#: Large enough that the fraction-of-equity caps never bind in tests that are
#: about something else. Tests that need a fraction to bind say so locally.
BIG_ACCOUNT = D("1000000")


def spread(
    credit: str = "1.50", quantity: int = 1, underlying: str = "SPY"
) -> OptionStrategyIntent:
    """A 5-wide SPY put credit spread: short 500, long 495."""
    legs = (
        OptionLegIntent(
            con_id=SHORT_CON_ID,
            symbol=underlying,
            expiration=EXPIRY,
            strike=D("500"),
            right=OptionRight.PUT,
            action=OrderAction.SELL,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
        OptionLegIntent(
            con_id=LONG_CON_ID,
            symbol=underlying,
            expiration=EXPIRY,
            strike=D("495"),
            right=OptionRight.PUT,
            action=OrderAction.BUY,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
    )
    return OptionStrategyIntent(
        strategy_id=uuid4(),
        strategy_type=StrategyType.PUT_CREDIT_SPREAD,
        strategy_action=StrategyAction.OPEN,
        underlying=underlying,
        quantity=quantity,
        legs=legs,
        expiration=EXPIRY,
        limit_price=D(credit),
        price_effect=PriceEffect.CREDIT,
        maximum_loss_per_contract=(D("5") - D(credit)) * 100,
        configuration_version="test",
        created_at=NOW,
    )


def provenance(
    generation: UUID,
    *,
    reported: MarketDataType | None = MarketDataType.LIVE,
    callback: bool = True,
    provider_at: dt.datetime | None = NOW,
) -> MarketDataProvenance:
    return MarketDataProvenance(
        requested_type=int(MarketDataType.LIVE),
        subscription_generation=generation,
        subscribed_at=NOW,
        reported_type=int(reported) if reported is not None else None,
        callback_received=callback,
        last_provider_event_at=provider_at,
        last_local_receive_at=NOW,
    )


def greeks(generation: UUID, *, delta: str | None = "-0.16") -> OptionGreeks:
    return OptionGreeks(
        received_at=NOW,
        subscription_generation=generation,
        delta=D(delta) if delta is not None else None,
        implied_volatility=D("0.19"),
    )


def live_snapshot(
    *,
    reported: MarketDataType | None = MarketDataType.LIVE,
    callback: bool = True,
    provider_at: dt.datetime | None = NOW,
    with_greeks: bool = True,
    delta: str | None = "-0.16",
    declared_generations: dict[str, UUID] | None = None,
) -> StrategyQuoteSnapshot:
    """A snapshot that is fully live unless a knob is turned.

    ``declared_generations`` overrides what the snapshot claims is active, which
    is how a generation mismatch is staged without the quotes themselves being
    inconsistent.
    """
    under_gen, short_gen, long_gen = uuid4(), uuid4(), uuid4()
    legs = tuple(
        OptionQuote(
            con_id=con_id,
            provenance=provenance(
                gen, reported=reported, callback=callback, provider_at=provider_at
            ),
            bid=D("1.40"),
            ask=D("1.60"),
            greeks=greeks(gen, delta=delta) if with_greeks else None,
        )
        for con_id, gen in ((SHORT_CON_ID, short_gen), (LONG_CON_ID, long_gen))
    )
    declared = declared_generations or {
        "underlying": under_gen,
        str(SHORT_CON_ID): short_gen,
        str(LONG_CON_ID): long_gen,
    }
    return StrategyQuoteSnapshot(
        underlying=UnderlyingQuote(
            symbol="SPY",
            provenance=provenance(
                under_gen, reported=reported, callback=callback, provider_at=provider_at
            ),
            bid=D("499.90"),
            ask=D("500.10"),
        ),
        legs=legs,
        generations=tuple(declared.items()),
    )


def margin(
    *,
    accepted: bool = True,
    initial: str | None = "500",
    maintenance: str | None = "500",
    rejection: str | None = None,
) -> MarginAssessment:
    return MarginAssessment(
        accepted=accepted,
        observed_at=NOW,
        initial_margin_change=D(initial) if initial is not None else None,
        maintenance_margin_change=D(maintenance) if maintenance is not None else None,
        rejection_reason=rejection,
    )


def approving_assessment(**overrides: Any) -> CandidateRiskAssessment:
    kwargs: dict[str, Any] = {
        "policy": RiskPolicy(),
        "quotes": live_snapshot(),
        "margin": margin(),
        "underlying_price": D("500"),
        "net_liquidation": BIG_ACCOUNT,
        "evaluated_at": NOW,
    }
    kwargs.update(overrides)
    intent = kwargs.pop("intent", None) or spread()
    return assess_candidate(intent, **kwargs)


# ===========================================================================
# The stress model
# ===========================================================================


class TestStressArithmetic:
    def test_terminal_payoff_below_both_strikes_is_the_maximum_loss(self) -> None:
        """A 5-wide spread collecting 1.50 loses 3.50 per share once both legs
        are in the money, which is exactly its stated maximum."""
        intent = spread("1.50")
        assert terminal_profit_per_share(intent, D("400")) == D("-3.50")
        assert terminal_loss(intent, D("400")) == D("350.00")
        assert terminal_loss(intent, D("400")) == intent.total_maximum_loss

    def test_terminal_payoff_above_both_strikes_is_a_profit_not_a_negative_loss(
        self,
    ) -> None:
        """A profit must report as zero loss. Returning a negative would let a
        winning scenario offset a losing one inside an aggregate cap."""
        intent = spread("1.50")
        assert terminal_profit_per_share(intent, D("600")) == D("1.50")
        assert terminal_loss(intent, D("600")) == D("0")

    def test_a_partial_loss_between_the_strikes(self) -> None:
        """Between the short and long strikes only the short leg is in the
        money, so the loss is real but below the maximum."""
        intent = spread("1.50")
        # short 500 worth 3.00, long 495 worthless, credit 1.50 -> -1.50/share
        assert terminal_loss(intent, D("497")) == D("150.00")

    def test_stress_takes_the_worse_of_the_two_directions(self) -> None:
        """A put spread only loses on the way down; taking one direction would
        understate whichever wing happened not to be tested."""
        intent = spread("1.50")
        loss = stress_loss(intent, underlying_price=D("500"), move_fraction=D("0.15"))
        assert loss == D("350.00")

    def test_stress_scales_with_quantity(self) -> None:
        intent = spread("1.50", quantity=3)
        loss = stress_loss(intent, underlying_price=D("500"), move_fraction=D("0.15"))
        assert loss == D("1050.00")

    def test_a_small_move_can_produce_a_partial_stress_loss(self) -> None:
        intent = spread("1.50")
        # 500 * (1 - 0.006) = 497.000
        loss = stress_loss(intent, underlying_price=D("500"), move_fraction=D("0.006"))
        assert loss == D("150.000")


class TestRequiredBuyingPower:
    def test_takes_the_larger_of_initial_and_maintenance(self) -> None:
        """Sizing against the smaller lets a position through that the account
        cannot carry the moment the larger is what is actually held."""
        assert required_buying_power(margin(initial="500", maintenance="700")) == D("700")
        assert required_buying_power(margin(initial="900", maintenance="700")) == D("900")

    def test_none_for_a_missing_assessment_or_field(self) -> None:
        assert required_buying_power(None) is None
        assert required_buying_power(margin(accepted=False, rejection="no")) is None
        assert required_buying_power(margin(initial=None)) is None
        assert required_buying_power(margin(maintenance=None)) is None


# ===========================================================================
# CheckResult and assessment invariants
# ===========================================================================


class TestCheckResultInvariants:
    def test_an_approved_result_may_not_carry_a_reason(self) -> None:
        with pytest.raises(ValueError, match="must not carry a refusal reason"):
            CheckResult(
                check="x",
                approved=True,
                reason=RiskRefusalReason.MAX_DEFINED_LOSS_EXCEEDED,
            )

    def test_a_refusal_must_name_a_reason(self) -> None:
        with pytest.raises(ValueError, match="machine-readable reason"):
            CheckResult(check="x", approved=False, detail="because")

    def test_a_refusal_must_explain_itself(self) -> None:
        with pytest.raises(ValueError, match="explain itself"):
            CheckResult(
                check="x",
                approved=False,
                reason=RiskRefusalReason.MAX_DEFINED_LOSS_EXCEEDED,
                detail="   ",
            )

    def test_a_reason_must_be_a_declared_taxonomy_member(self) -> None:
        """A bare string would be indistinguishable from a code until someone
        tried to match on it."""
        with pytest.raises(ValueError, match="declared refusal taxonomy"):
            CheckResult(
                check="x",
                approved=False,
                reason="OPTIONS_SOMETHING",  # type: ignore[arg-type]
                detail="because",
            )

    def test_reason_code_is_the_stable_string(self) -> None:
        result = CheckResult(
            check="x",
            approved=False,
            reason=RiskRefusalReason.STRESS_LOSS_EXCEEDED,
            detail="because",
        )
        assert result.reason_code == "OPTIONS_STRESS_LOSS_EXCEEDED"
        assert CheckResult(check="x", approved=True).reason_code is None


class TestAssessmentCompleteness:
    def _result(self, name: str) -> CheckResult:
        return CheckResult(check=name, approved=True)

    def test_an_assessment_missing_a_check_cannot_be_built(self) -> None:
        """The property that replaces 'the gate raises': approval by omission is
        not expressible."""
        partial = tuple(self._result(name) for name in REQUIRED_CHECKS[:-1])
        with pytest.raises(ValueError, match="incomplete risk assessment"):
            CandidateRiskAssessment(
                strategy_id=uuid4(),
                evaluated_at=NOW,
                policy_version="test",
                results=partial,
            )

    def test_the_entitlement_check_is_one_of_the_required_ones(self) -> None:
        """Requirement: delayed data must never make a candidate tradeable. That
        holds only if the entitlement question is mandatory."""
        assert CHECK_MARKET_DATA_ENTITLEMENT in REQUIRED_CHECKS

    def test_a_duplicated_check_is_refused(self) -> None:
        results = tuple(self._result(name) for name in REQUIRED_CHECKS)
        with pytest.raises(ValueError, match="reported twice"):
            CandidateRiskAssessment(
                strategy_id=uuid4(),
                evaluated_at=NOW,
                policy_version="test",
                results=results + (self._result(CHECK_DEFINED_LOSS),),
            )

    def test_an_unrecognised_check_is_refused(self) -> None:
        results = tuple(self._result(name) for name in REQUIRED_CHECKS)
        with pytest.raises(ValueError, match="unrecognised checks"):
            CandidateRiskAssessment(
                strategy_id=uuid4(),
                evaluated_at=NOW,
                policy_version="test",
                results=results + (self._result("vibes"),),
            )

    def test_evaluated_at_must_be_timezone_aware(self) -> None:
        results = tuple(self._result(name) for name in REQUIRED_CHECKS)
        with pytest.raises(ValueError, match="timezone-aware"):
            CandidateRiskAssessment(
                strategy_id=uuid4(),
                evaluated_at=dt.datetime(2026, 7, 29, 13, 0),
                policy_version="test",
                results=results,
            )


# ===========================================================================
# Approval
# ===========================================================================


class TestApproval:
    def test_a_good_candidate_with_live_data_is_approved(self) -> None:
        assessment = approving_assessment()
        assert assessment.approved, assessment.describe()
        assert assessment.reason_codes == ()
        assert len(assessment.results) == len(REQUIRED_CHECKS)

    def test_nothing_short_circuits(self) -> None:
        """Every check runs even once one has refused, so a single report names
        every problem instead of sending the operator round the loop per cause."""
        assessment = approving_assessment(
            quotes=None, margin=None, underlying_price=None, net_liquidation=None
        )
        assert not assessment.approved
        assert len(assessment.results) == len(REQUIRED_CHECKS)
        assert len(assessment.refusals) == len(REQUIRED_CHECKS)

    def test_a_value_exactly_at_a_cap_is_approved(self) -> None:
        """Caps are inclusive. An off-by-one here silently narrows every limit."""
        policy = RiskPolicy(max_defined_loss_per_position=D("350"))
        result = check_defined_loss(
            spread("1.50"), policy=policy, net_liquidation=BIG_ACCOUNT
        )
        assert result.approved
        assert result.observed == D("350.00")

    def test_one_cent_over_a_cap_is_refused(self) -> None:
        policy = RiskPolicy(max_defined_loss_per_position=D("349.99"))
        result = check_defined_loss(
            spread("1.50"), policy=policy, net_liquidation=BIG_ACCOUNT
        )
        assert not result.approved
        assert result.reason_code == "OPTIONS_MAX_DEFINED_LOSS_EXCEEDED"


# ===========================================================================
# The entitlement gate, wired in
# ===========================================================================


class TestMarketDataEntitlement:
    def _reason(self, snapshot: StrategyQuoteSnapshot | None, **kwargs: Any) -> str | None:
        result = check_market_data_entitlement(
            snapshot, decision_time=NOW, policy=RiskPolicy(**kwargs)
        )
        return result.reason_code

    def test_live_current_data_passes(self) -> None:
        result = check_market_data_entitlement(
            live_snapshot(), decision_time=NOW, policy=RiskPolicy()
        )
        assert result.approved, result.detail

    def test_no_snapshot_fails_closed(self) -> None:
        """'No market data was supplied' is the strongest reason to refuse, not
        a reason to skip the check."""
        assert self._reason(None) == "OPTIONS_NO_MARKET_DATA_SNAPSHOT"

    def test_delayed_data_is_refused_with_the_realtime_code(self) -> None:
        """The requirement this milestone exists to enforce."""
        assert (
            self._reason(live_snapshot(reported=MarketDataType.DELAYED))
            == "OPTIONS_REALTIME_DATA_REQUIRED"
        )

    def test_delayed_frozen_and_frozen_are_refused_too(self) -> None:
        for reported in (MarketDataType.FROZEN, MarketDataType.DELAYED_FROZEN):
            assert (
                self._reason(live_snapshot(reported=reported))
                == "OPTIONS_REALTIME_DATA_REQUIRED"
            )

    def test_silence_from_the_provider_is_not_live(self) -> None:
        """Ticker.marketDataType defaults to 1, so an absent callback is UNKNOWN
        rather than evidence of a live feed."""
        assert (
            self._reason(live_snapshot(callback=False))
            == "MARKET_DATA_TYPE_CALLBACK_MISSING"
        )

    def test_a_stale_quote_is_refused(self) -> None:
        stale = live_snapshot(provider_at=NOW - dt.timedelta(minutes=5))
        assert self._reason(stale) == "MARKET_DATA_STALE"

    def test_a_quote_with_no_provider_timestamp_is_refused(self) -> None:
        assert (
            self._reason(live_snapshot(provider_at=None))
            == "MARKET_DATA_PROVIDER_TIMESTAMP_MISSING"
        )

    def test_missing_greeks_are_refused(self) -> None:
        assert self._reason(live_snapshot(with_greeks=False)) == "OPTION_GREEKS_MISSING"

    def test_greeks_present_but_delta_absent_are_refused(self) -> None:
        """modelGreeks is assigned even when every field sanitizes away."""
        assert self._reason(live_snapshot(delta=None)) == "OPTION_DELTA_INVALID"

    def test_a_superseded_generation_is_refused(self) -> None:
        """ib_async reuses ticker objects across subscriptions, so a value from
        an earlier generation is not current data."""
        snapshot = live_snapshot(
            declared_generations={
                "underlying": uuid4(),
                str(SHORT_CON_ID): uuid4(),
                str(LONG_CON_ID): uuid4(),
            }
        )
        assert self._reason(snapshot) == "MARKET_DATA_GENERATION_MISMATCH"

    def test_the_age_boundary_is_inclusive(self) -> None:
        policy = RiskPolicy(quote_maximum_age=dt.timedelta(seconds=10))
        exactly = live_snapshot(provider_at=NOW - dt.timedelta(seconds=10))
        assert check_market_data_entitlement(
            exactly, decision_time=NOW, policy=policy
        ).approved
        over = live_snapshot(
            provider_at=NOW - dt.timedelta(seconds=10, microseconds=1)
        )
        assert not check_market_data_entitlement(
            over, decision_time=NOW, policy=policy
        ).approved


class TestSnapshotConstruction:
    def test_a_snapshot_missing_a_leg_generation_refuses_rather_than_key_errors(
        self,
    ) -> None:
        """A KeyError escaping a gate is an outage; this must be a refusal with
        a machine-readable reason like every other market-data failure."""
        with pytest.raises(MarketDataRefusedError) as exc:
            live_snapshot(declared_generations={"underlying": uuid4()})
        assert exc.value.reason == RefusalReason.GENERATION_MISMATCH.value

    def test_a_snapshot_with_no_legs_is_refused(self) -> None:
        with pytest.raises(MarketDataRefusedError):
            StrategyQuoteSnapshot(
                underlying=UnderlyingQuote(
                    symbol="SPY", provenance=provenance(uuid4())
                ),
                legs=(),
                generations=(("underlying", uuid4()),),
            )


# ===========================================================================
# Defined loss, broker margin, stress
# ===========================================================================


class TestDefinedLoss:
    def test_over_the_absolute_cap(self) -> None:
        result = check_defined_loss(
            spread("1.50", quantity=5),
            policy=RiskPolicy(),
            net_liquidation=BIG_ACCOUNT,
        )
        assert result.reason_code == "OPTIONS_MAX_DEFINED_LOSS_EXCEEDED"
        assert result.observed == D("1750.00")

    def test_over_the_fraction_of_equity(self) -> None:
        result = check_defined_loss(
            spread("1.50"),
            policy=RiskPolicy(max_defined_loss_fraction=D("0.001")),
            net_liquidation=D("100000"),
        )
        assert result.reason_code == "OPTIONS_DEFINED_LOSS_FRACTION_EXCEEDED"
        assert result.limit == D("100.000")

    def test_missing_net_liquidation_fails_closed(self) -> None:
        result = check_defined_loss(
            spread("1.50"), policy=RiskPolicy(), net_liquidation=None
        )
        assert result.reason_code == "OPTIONS_NET_LIQUIDATION_UNAVAILABLE"


class TestBrokerMargin:
    def test_a_missing_what_if_is_refused(self) -> None:
        result = check_broker_margin(
            None, policy=RiskPolicy(), net_liquidation=BIG_ACCOUNT
        )
        assert result.reason_code == "OPTIONS_BROKER_WHATIF_MISSING"

    def test_a_rejected_what_if_is_refused_with_the_broker_reason(self) -> None:
        result = check_broker_margin(
            margin(accepted=False, rejection="riskless combination"),
            policy=RiskPolicy(),
            net_liquidation=BIG_ACCOUNT,
        )
        assert result.reason_code == "OPTIONS_BROKER_WHATIF_REJECTED"
        assert "riskless combination" in result.detail

    def test_a_missing_margin_field_is_refused_not_treated_as_zero(self) -> None:
        """An unknown margin impact is not a small one."""
        result = check_broker_margin(
            margin(maintenance=None), policy=RiskPolicy(), net_liquidation=BIG_ACCOUNT
        )
        assert result.reason_code == "OPTIONS_BROKER_MARGIN_FIELD_MISSING"

    def test_over_the_absolute_cap(self) -> None:
        result = check_broker_margin(
            margin(initial="5000", maintenance="5000"),
            policy=RiskPolicy(),
            net_liquidation=BIG_ACCOUNT,
        )
        assert result.reason_code == "OPTIONS_BROKER_MARGIN_EXCEEDED"

    def test_over_the_fraction_of_equity(self) -> None:
        result = check_broker_margin(
            margin(initial="500", maintenance="500"),
            policy=RiskPolicy(max_broker_margin_fraction=D("0.001")),
            net_liquidation=D("100000"),
        )
        assert result.reason_code == "OPTIONS_BROKER_MARGIN_FRACTION_EXCEEDED"

    def test_missing_net_liquidation_fails_closed(self) -> None:
        result = check_broker_margin(
            margin(), policy=RiskPolicy(), net_liquidation=None
        )
        assert result.reason_code == "OPTIONS_NET_LIQUIDATION_UNAVAILABLE"


class TestStressCheck:
    def test_a_missing_reference_price_fails_closed(self) -> None:
        result = check_stress_loss(
            spread("1.50"),
            policy=RiskPolicy(),
            underlying_price=None,
            net_liquidation=BIG_ACCOUNT,
        )
        assert result.reason_code == "OPTIONS_STRESS_REFERENCE_PRICE_MISSING"

    def test_a_non_positive_reference_price_fails_closed(self) -> None:
        result = check_stress_loss(
            spread("1.50"),
            policy=RiskPolicy(),
            underlying_price=D("0"),
            net_liquidation=BIG_ACCOUNT,
        )
        assert result.reason_code == "OPTIONS_STRESS_REFERENCE_PRICE_MISSING"

    def test_over_the_absolute_cap(self) -> None:
        result = check_stress_loss(
            spread("1.50", quantity=4),
            policy=RiskPolicy(max_defined_loss_per_position=D("10000")),
            underlying_price=D("500"),
            net_liquidation=BIG_ACCOUNT,
        )
        assert result.reason_code == "OPTIONS_STRESS_LOSS_EXCEEDED"
        assert result.observed == D("1400.00")

    def test_over_the_fraction_of_equity(self) -> None:
        result = check_stress_loss(
            spread("1.50"),
            policy=RiskPolicy(max_stress_loss_fraction=D("0.001")),
            underlying_price=D("500"),
            net_liquidation=D("100000"),
        )
        assert result.reason_code == "OPTIONS_STRESS_LOSS_FRACTION_EXCEEDED"

    def test_missing_net_liquidation_fails_closed(self) -> None:
        result = check_stress_loss(
            spread("1.50"),
            policy=RiskPolicy(),
            underlying_price=D("500"),
            net_liquidation=None,
        )
        assert result.reason_code == "OPTIONS_NET_LIQUIDATION_UNAVAILABLE"

    def test_a_stress_loss_above_the_stated_maximum_is_refused(self) -> None:
        """A defined-risk structure cannot lose more than its stated maximum. If
        it computes that way, the legs and the stored figure disagree and
        neither number should be trusted."""
        intent = spread("1.50")
        # Rebuild with a maximum_loss the domain would accept for a different
        # credit, so the stored figure understates the legs' real exposure.
        forged = OptionStrategyIntent(
            strategy_id=intent.strategy_id,
            strategy_type=intent.strategy_type,
            strategy_action=StrategyAction.CLOSE,
            underlying=intent.underlying,
            quantity=1,
            legs=intent.legs,
            expiration=intent.expiration,
            limit_price=D("1.50"),
            price_effect=PriceEffect.DEBIT,
            maximum_loss_per_contract=D("10"),
            configuration_version="test",
            created_at=NOW,
            closes_strategy_id=uuid4(),
        )
        result = check_stress_loss(
            forged,
            policy=RiskPolicy(),
            underlying_price=D("500"),
            net_liquidation=BIG_ACCOUNT,
        )
        assert result.reason_code == "OPTIONS_STRESS_EXCEEDS_DEFINED_LOSS"


# ===========================================================================
# Every refusal code must be reachable
# ===========================================================================


def _produce_no_market_data_snapshot() -> str | None:
    return check_market_data_entitlement(
        None, decision_time=NOW, policy=RiskPolicy()
    ).reason_code


def _produce_net_liquidation_unavailable() -> str | None:
    return check_defined_loss(
        spread("1.50"), policy=RiskPolicy(), net_liquidation=None
    ).reason_code


def _produce_max_defined_loss_exceeded() -> str | None:
    return check_defined_loss(
        spread("1.50", quantity=5), policy=RiskPolicy(), net_liquidation=BIG_ACCOUNT
    ).reason_code


def _produce_defined_loss_fraction_exceeded() -> str | None:
    return check_defined_loss(
        spread("1.50"),
        policy=RiskPolicy(max_defined_loss_fraction=D("0.001")),
        net_liquidation=D("100000"),
    ).reason_code


def _produce_broker_whatif_missing() -> str | None:
    return check_broker_margin(
        None, policy=RiskPolicy(), net_liquidation=BIG_ACCOUNT
    ).reason_code


def _produce_broker_whatif_rejected() -> str | None:
    return check_broker_margin(
        margin(accepted=False, rejection="no"),
        policy=RiskPolicy(),
        net_liquidation=BIG_ACCOUNT,
    ).reason_code


def _produce_broker_margin_field_missing() -> str | None:
    return check_broker_margin(
        margin(initial=None), policy=RiskPolicy(), net_liquidation=BIG_ACCOUNT
    ).reason_code


def _produce_broker_margin_exceeded() -> str | None:
    return check_broker_margin(
        margin(initial="9999", maintenance="9999"),
        policy=RiskPolicy(),
        net_liquidation=BIG_ACCOUNT,
    ).reason_code


def _produce_broker_margin_fraction_exceeded() -> str | None:
    return check_broker_margin(
        margin(),
        policy=RiskPolicy(max_broker_margin_fraction=D("0.001")),
        net_liquidation=D("100000"),
    ).reason_code


def _produce_stress_reference_price_missing() -> str | None:
    return check_stress_loss(
        spread("1.50"),
        policy=RiskPolicy(),
        underlying_price=None,
        net_liquidation=BIG_ACCOUNT,
    ).reason_code


def _produce_stress_exceeds_defined_loss() -> str | None:
    intent = spread("1.50")
    forged = OptionStrategyIntent(
        strategy_id=intent.strategy_id,
        strategy_type=intent.strategy_type,
        strategy_action=StrategyAction.CLOSE,
        underlying=intent.underlying,
        quantity=1,
        legs=intent.legs,
        expiration=intent.expiration,
        limit_price=D("1.50"),
        price_effect=PriceEffect.DEBIT,
        maximum_loss_per_contract=D("10"),
        configuration_version="test",
        created_at=NOW,
        closes_strategy_id=uuid4(),
    )
    return check_stress_loss(
        forged,
        policy=RiskPolicy(),
        underlying_price=D("500"),
        net_liquidation=BIG_ACCOUNT,
    ).reason_code


def _produce_stress_loss_exceeded() -> str | None:
    return check_stress_loss(
        spread("1.50", quantity=4),
        policy=RiskPolicy(max_defined_loss_per_position=D("10000")),
        underlying_price=D("500"),
        net_liquidation=BIG_ACCOUNT,
    ).reason_code


def _produce_stress_loss_fraction_exceeded() -> str | None:
    return check_stress_loss(
        spread("1.50"),
        policy=RiskPolicy(max_stress_loss_fraction=D("0.001")),
        underlying_price=D("500"),
        net_liquidation=D("100000"),
    ).reason_code


#: Every member of RiskRefusalReason, mapped to something that produces it.
PRODUCERS: dict[RiskRefusalReason, Callable[[], str | None]] = {
    RiskRefusalReason.NO_MARKET_DATA_SNAPSHOT: _produce_no_market_data_snapshot,
    RiskRefusalReason.NET_LIQUIDATION_UNAVAILABLE: _produce_net_liquidation_unavailable,
    RiskRefusalReason.MAX_DEFINED_LOSS_EXCEEDED: _produce_max_defined_loss_exceeded,
    RiskRefusalReason.DEFINED_LOSS_FRACTION_EXCEEDED: (
        _produce_defined_loss_fraction_exceeded
    ),
    RiskRefusalReason.BROKER_WHATIF_MISSING: _produce_broker_whatif_missing,
    RiskRefusalReason.BROKER_WHATIF_REJECTED: _produce_broker_whatif_rejected,
    RiskRefusalReason.BROKER_MARGIN_FIELD_MISSING: _produce_broker_margin_field_missing,
    RiskRefusalReason.BROKER_MARGIN_EXCEEDED: _produce_broker_margin_exceeded,
    RiskRefusalReason.BROKER_MARGIN_FRACTION_EXCEEDED: (
        _produce_broker_margin_fraction_exceeded
    ),
    RiskRefusalReason.STRESS_REFERENCE_PRICE_MISSING: (
        _produce_stress_reference_price_missing
    ),
    RiskRefusalReason.STRESS_EXCEEDS_DEFINED_LOSS: _produce_stress_exceeds_defined_loss,
    RiskRefusalReason.STRESS_LOSS_EXCEEDED: _produce_stress_loss_exceeded,
    RiskRefusalReason.STRESS_LOSS_FRACTION_EXCEEDED: (
        _produce_stress_loss_fraction_exceeded
    ),
}


class TestEveryRefusalReasonIsReachable:
    def test_the_producer_table_covers_the_whole_enum(self) -> None:
        """Adding a refusal code without a test that reaches it fails here,
        rather than shipping a branch nobody has ever executed."""
        assert set(PRODUCERS) == set(RiskRefusalReason)

    @pytest.mark.parametrize("reason", sorted(RiskRefusalReason, key=lambda r: r.value))
    def test_each_reason_is_actually_produced(self, reason: RiskRefusalReason) -> None:
        assert PRODUCERS[reason]() == reason.value


class TestMarketDataReasonsFlowThroughUnchanged:
    """The entitlement check must not re-label the market-data taxonomy.

    A caller branching on ``OPTIONS_REALTIME_DATA_REQUIRED`` needs that exact
    code -- a wrapper code would hide which layer refused and turn "buy the
    subscription" into "read the message and guess".
    """

    @pytest.mark.parametrize(
        "snapshot_kwargs,expected",
        [
            ({"reported": MarketDataType.DELAYED}, RefusalReason.REALTIME_DATA_REQUIRED),
            ({"callback": False}, RefusalReason.NO_DATA_TYPE_CALLBACK),
            ({"with_greeks": False}, RefusalReason.GREEKS_MISSING),
            ({"delta": None}, RefusalReason.DELTA_INVALID),
            ({"provider_at": None}, RefusalReason.NO_PROVIDER_TIMESTAMP),
        ],
    )
    def test_the_original_code_survives(
        self, snapshot_kwargs: dict[str, Any], expected: RefusalReason
    ) -> None:
        result = check_market_data_entitlement(
            live_snapshot(**snapshot_kwargs), decision_time=NOW, policy=RiskPolicy()
        )
        assert result.reason_code == expected.value
        assert result.check == CHECK_MARKET_DATA_ENTITLEMENT


class TestAssessmentRecord:
    def test_to_record_is_json_shaped_and_names_every_check(self) -> None:
        record = approving_assessment().to_record()
        assert record["approved"] is True
        assert {check["check"] for check in record["checks"]} == set(REQUIRED_CHECKS)
        assert record["policy_version"] == RiskPolicy().version

    def test_a_refused_record_carries_the_codes(self) -> None:
        record = approving_assessment(quotes=None).to_record()
        assert record["approved"] is False
        assert "OPTIONS_NO_MARKET_DATA_SNAPSHOT" in record["reason_codes"]

    def test_describe_lists_checks_in_the_declared_order(self) -> None:
        """Two reports of the same structure must be diffable."""
        text = approving_assessment().describe()
        positions = [text.index(name) for name in REQUIRED_CHECKS]
        assert positions == sorted(positions)
        assert CHECK_BROKER_MARGIN in text
        assert CHECK_STRESS_LOSS in text


#: The contract that vetoed a spread it had nothing to do with, on 2026-07-30.
#: Never selected, never to be traded -- it merely sat in the same chain window.
BYSTANDER_CON_ID = 891847214


class TestEntitlementIsScopedToTheSelectedStructure:
    """The regression for the incident that blocked the first fill.

    A quote snapshot is two things under one name. As a *selection universe* it
    holds every contract the scan inspected -- dozens of strikes, most of them
    considered and discarded. As an *execution proof* it should hold exactly the
    underlying and the legs about to be sent.

    Conflating them let contract 891847214, which was never selected, refuse a
    722/721 spread three times. Narrowing to the intent's legs is what let the
    engine's first order fill.

    This test exists because a mutation sweep found the narrowing was **not
    load-bearing**: reverting ``execution_entitlement_set`` to ``snapshot.legs``
    failed nothing, because every other call site omits ``intent`` and so takes
    the identical ``intent is None`` branch. The fix could have been silently
    reverted and the suite would have stayed green -- the C12 pattern again, and
    the honest gap carried from the day it was written.

    Note what kind of guard this is: reverting it makes the engine **stricter**,
    not laxer. It over-refuses rather than authorizing anything unsafe, which is
    exactly why no safety test caught it. That does not make it optional -- an
    engine that refuses every correct trade is as unusable as one that accepts
    wrong ones, and this one demonstrably did.
    """

    def _with_bystander(self, **kwargs: Any) -> StrategyQuoteSnapshot:
        """A live snapshot plus one unusable contract that is *not* in the intent."""
        base = live_snapshot()
        bystander_gen = uuid4()
        stranded = OptionQuote(
            con_id=BYSTANDER_CON_ID,
            # The exact condition observed: the provider never reported a
            # market-data type for it, so its liveness is UNKNOWN.
            provenance=provenance(bystander_gen, reported=None, callback=False),
            bid=None,
            ask=None,
            greeks=None,
        )
        # It has to carry a declared generation, or the snapshot refuses to
        # construct and the test never reaches the gate at all -- the scan really
        # did subscribe to this contract, it simply never heard back about it.
        return StrategyQuoteSnapshot(
            underlying=base.underlying,
            legs=(*base.legs, stranded),
            generations=(*base.generations, (str(BYSTANDER_CON_ID), bystander_gen)),
        )

    def test_an_unselected_chain_contract_does_not_veto_the_structure(self) -> None:
        intent = spread()
        result = check_market_data_entitlement(
            self._with_bystander(),
            decision_time=NOW,
            policy=RiskPolicy(),
            intent=intent,
        )
        assert result.approved, result.detail
        assert BYSTANDER_CON_ID not in {leg.con_id for leg in intent.legs}

    def test_the_bystander_really_would_have_vetoed_it(self) -> None:
        """The control. Without it the test above proves nothing.

        Omitting ``intent`` is what every other call site does, and it is the
        behaviour the mutation restored -- so this pins the difference the
        narrowing actually makes.
        """
        result = check_market_data_entitlement(
            self._with_bystander(), decision_time=NOW, policy=RiskPolicy()
        )
        assert not result.approved, (
            "the whole-chain check must refuse here, or the narrowing above is "
            "not being tested at all"
        )

    def test_a_selected_leg_still_vetoes(self) -> None:
        """Narrowing must not become skipping.

        The same defect made harmless is one thing; the gate going quiet on a
        leg that IS being traded is the failure it was protecting against.
        """
        intent = spread()
        base = live_snapshot()
        broken = StrategyQuoteSnapshot(
            underlying=base.underlying,
            legs=(
                base.legs[0],
                OptionQuote(
                    con_id=LONG_CON_ID,
                    provenance=provenance(uuid4(), reported=None, callback=False),
                    bid=None,
                    ask=None,
                    greeks=None,
                ),
            ),
            generations=base.generations,
        )
        result = check_market_data_entitlement(
            broken, decision_time=NOW, policy=RiskPolicy(), intent=intent
        )
        assert not result.approved, "a traded leg with no callback must refuse"
