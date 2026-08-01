"""The volatility-regime classifier: tiers, edges, and honest refusals.

The property under test throughout: **missing inputs degrade toward refusal,
never toward permission**, and every decision names its reasons. A classifier
that guessed on absent data would be the IVR>=50 wall's failure mode inverted
-- trading blind instead of never trading.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from engine.errors import ConfigError
from engine.options.ivrank import (
    IVObservation,
    build_iv_rank,
    iv_percentile,
)
from engine.options.regime import (
    REFUSAL_DEPRESSED,
    REFUSAL_EDGE,
    REFUSAL_UNKNOWN,
    LowIVFamilyRegistry,
    RegimeDecision,
    StrategyFamily,
    VolatilityAssessment,
    VolatilityRegime,
    VolatilityRegimePolicy,
    classify,
    regime_mode,
)

D = Decimal
POLICY = VolatilityRegimePolicy()


def assessment(**kwargs) -> VolatilityAssessment:
    return VolatilityAssessment(symbol="SPY", **kwargs)


class TestTierPlacement:
    def test_high_at_and_above_fifty(self) -> None:
        for ivr in ("50", "50.0001", "97"):
            decision = classify(assessment(iv_rank=D(ivr)), POLICY)
            assert decision.regime is VolatilityRegime.HIGH, decision.describe()
            assert decision.permits_entry
            assert decision.allocation == D("1.00")
            assert decision.permitted_families == (StrategyFamily.SHORT_PREMIUM,)

    def test_medium_with_edge(self) -> None:
        decision = classify(
            assessment(iv_rank=D("35"), iv_rv_ratio=D("1.10")), POLICY
        )
        assert decision.regime is VolatilityRegime.MEDIUM
        assert decision.permits_entry
        assert decision.allocation == D("0.50")

    def test_low_with_strong_edge_is_directional_only(self) -> None:
        decision = classify(
            assessment(iv_rank=D("21.5"), iv_rv_ratio=D("1.20")), POLICY
        )
        assert decision.regime is VolatilityRegime.LOW
        assert decision.permits_entry
        assert decision.allocation == D("0.25")
        assert decision.permitted_families == (StrategyFamily.DIRECTIONAL_CREDIT,)
        assert decision.preferred_dte == (50, 65)

    def test_depressed_refuses_short_premium(self) -> None:
        decision = classify(assessment(iv_rank=D("13.2")), POLICY)
        assert decision.regime is VolatilityRegime.DEPRESSED
        assert not decision.permits_entry
        assert decision.refusal_code == REFUSAL_DEPRESSED
        assert decision.allocation == D("0")
        assert decision.permitted_families == ()

    def test_boundaries_are_half_open(self) -> None:
        """Exactly 30 is MEDIUM, exactly 20 is LOW, just under is the tier below."""
        at_30 = classify(assessment(iv_rank=D("30"), iv_rv_ratio=D("2")), POLICY)
        under_30 = classify(
            assessment(iv_rank=D("29.999"), iv_rv_ratio=D("2")), POLICY
        )
        at_20 = classify(assessment(iv_rank=D("20"), iv_rv_ratio=D("2")), POLICY)
        under_20 = classify(
            assessment(iv_rank=D("19.999"), iv_rv_ratio=D("2")), POLICY
        )
        assert at_30.regime is VolatilityRegime.MEDIUM
        assert under_30.regime is VolatilityRegime.LOW
        assert at_20.regime is VolatilityRegime.LOW
        assert under_20.regime is VolatilityRegime.DEPRESSED


class TestFailTowardRefusal:
    """IV Rank must not be evaluated alone -- and unknown never permits."""

    def test_missing_iv_rank_is_unknown_and_refuses(self) -> None:
        decision = classify(assessment(), POLICY)
        assert decision.regime is VolatilityRegime.UNKNOWN
        assert not decision.permits_entry
        assert decision.refusal_code == REFUSAL_UNKNOWN

    def test_medium_without_edge_data_refuses(self) -> None:
        """IVR 35 with no realized-vol data: the tier is placed but its
        requirement cannot be established -- unknown fails the requirement."""
        decision = classify(assessment(iv_rank=D("35")), POLICY)
        assert decision.regime is VolatilityRegime.MEDIUM
        assert not decision.permits_entry
        assert decision.refusal_code == REFUSAL_EDGE
        assert any("could not be established" in r for r in decision.reasons)

    def test_medium_with_insufficient_edge_refuses(self) -> None:
        decision = classify(
            assessment(iv_rank=D("35"), iv_rv_ratio=D("0.95")), POLICY
        )
        assert not decision.permits_entry
        assert decision.refusal_code == REFUSAL_EDGE

    def test_low_demands_a_stronger_edge_than_medium(self) -> None:
        """An edge that clears MEDIUM (1.00) does not clear LOW (1.15)."""
        edge = D("1.05")
        medium = classify(assessment(iv_rank=D("35"), iv_rv_ratio=edge), POLICY)
        low = classify(assessment(iv_rank=D("25"), iv_rv_ratio=edge), POLICY)
        assert medium.permits_entry
        assert not low.permits_entry
        assert low.refusal_code == REFUSAL_EDGE

    def test_every_decision_names_its_reasons(self) -> None:
        for kwargs in (
            {},
            {"iv_rank": D("60")},
            {"iv_rank": D("35"), "iv_rv_ratio": D("1.2")},
            {"iv_rank": D("25")},
            {"iv_rank": D("10")},
        ):
            decision = classify(assessment(**kwargs), POLICY)
            assert decision.reasons, decision.describe()
            record = decision.to_record()
            assert record["reasons"]
            assert record["policy_version"] == POLICY.version


class TestDepressedRegistry:
    def test_the_shipped_registry_is_empty(self) -> None:
        """Spec: do not invent or activate low-IV strategies before they are
        validated. The registry existing AND being empty is the design."""
        assert VolatilityRegimePolicy().registry.validated == ()

    def test_a_validated_family_is_named_but_still_not_routed(self) -> None:
        policy = VolatilityRegimePolicy(
            registry=LowIVFamilyRegistry(validated=(StrategyFamily.LOW_IV_VALIDATED,))
        )
        decision = classify(assessment(iv_rank=D("5")), policy)
        assert not decision.permits_entry
        assert any("not implemented by this decision" in r for r in decision.reasons)


class TestPolicyValidation:
    def test_inverted_boundaries_refuse(self) -> None:
        with pytest.raises(ConfigError):
            VolatilityRegimePolicy(
                low_minimum_iv_rank=D("40"), medium_minimum_iv_rank=D("30")
            )

    def test_zero_allocation_refuses(self) -> None:
        with pytest.raises(ConfigError):
            VolatilityRegimePolicy(medium_allocation=D("0"))

    def test_low_edge_below_medium_edge_refuses(self) -> None:
        with pytest.raises(ConfigError):
            VolatilityRegimePolicy(
                medium_minimum_iv_rv=D("1.2"), low_minimum_iv_rv=D("1.0")
            )

    def test_from_env_reads_prefixed_overrides(self) -> None:
        policy = VolatilityRegimePolicy.from_env(
            {"IBKR_OPTIONS_REGIME_HIGH_MINIMUM_IV_RANK": "55"}
        )
        assert policy.high_minimum_iv_rank == D("55")

    def test_to_record_round_trips_the_thresholds(self) -> None:
        record = VolatilityRegimePolicy().to_record()
        assert record["high_minimum_iv_rank"] == "50"
        assert record["validated_low_iv_families"] == ""


class TestRegimeMode:
    def test_defaults_to_shadow(self) -> None:
        assert regime_mode({}) == "shadow"

    def test_only_the_exact_word_live_activates(self) -> None:
        assert regime_mode({"IBKR_OPTIONS_REGIME_MODE": "live"}) == "live"
        for typo in ("Live ", "LIVE!", "on", "true", "prod"):
            assert regime_mode({"IBKR_OPTIONS_REGIME_MODE": typo}) == "shadow", typo


class TestIVPercentile:
    def _obs(self, values: list[str]) -> list[IVObservation]:
        start = dt.date(2026, 1, 1)
        return [
            IVObservation(on=start + dt.timedelta(days=i), implied_volatility=D(v))
            for i, v in enumerate(values)
        ]

    def test_percentile_counts_at_or_below(self) -> None:
        obs = self._obs(["0.10", "0.20", "0.30", "0.40"])
        assert iv_percentile(obs, D("0.30")) == D("75")
        assert iv_percentile(obs, D("0.05")) == D("0")
        assert iv_percentile(obs, D("0.40")) == D("100")

    def test_empty_or_nonpositive_is_none(self) -> None:
        assert iv_percentile([], D("0.2")) is None
        assert iv_percentile(self._obs(["0.1"]), D("0")) is None

    def test_build_iv_rank_carries_the_percentile(self) -> None:
        """Rank and percentile disagree under a spike-dominated range -- the
        exact situation the regime classifier needs both numbers for."""
        values = ["0.15"] * 59 + ["0.90", "0.18"]
        metric = build_iv_rank(
            "SPY",
            self._obs(values),
            calculated_at=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        )
        assert metric.iv_rank is not None and metric.iv_rank < D("5")
        assert metric.iv_percentile is not None and metric.iv_percentile > D("95")

    def test_degraded_metric_has_no_percentile(self) -> None:
        metric = build_iv_rank(
            "SPY",
            self._obs(["0.2"] * 10),
            calculated_at=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        )
        assert metric.degraded_reason is not None
        assert metric.iv_percentile is None
