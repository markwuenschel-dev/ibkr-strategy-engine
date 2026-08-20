"""Risk-policy thresholds: the numbers every later check compares against.

A policy object that survives construction with a bad threshold is worse than
no policy at all -- the governor keeps reporting that it checked. Every test
here is a way a threshold could have been silently accepted.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from engine.errors import ConfigError
from engine.options.policy import (
    DEFAULT_CORRELATION_GROUPS,
    DEFAULT_SECTORS,
    ENV_PREFIX,
    POLICY_VERSION,
    RiskPolicy,
)

D = Decimal

# Every field routed through _check_fraction in RiskPolicy.__post_init__.
FRACTION_FIELDS = (
    "max_defined_loss_fraction",
    "max_broker_margin_fraction",
    "max_stress_loss_fraction",
    "max_total_bpr_fraction",
    "max_incremental_bpr_fraction",
    "max_underlying_bpr_fraction",
    "max_sector_bpr_fraction",
    "max_correlation_group_bpr_fraction",
)

# Every field routed through _check_amount.
AMOUNT_FIELDS = (
    "max_defined_loss_per_position",
    "max_broker_margin_per_position",
    "max_stress_loss_per_position",
    "target_width",
    "risk_budget_per_position",
)

# Every field routed through _check_target_delta -- (0, 1) exclusive at both
# ends, unlike a fraction-of-equity cap where 1 is coherent.
TARGET_DELTA_FIELDS = ("neutral_target_delta", "directional_target_delta")

AGE_FIELDS = ("quote_maximum_age", "portfolio_snapshot_maximum_age")

FULL_ENV = {
    f"{ENV_PREFIX}MAX_DEFINED_LOSS_PER_POSITION": "250",
    f"{ENV_PREFIX}MAX_DEFINED_LOSS_FRACTION": "0.01",
    f"{ENV_PREFIX}MAX_BROKER_MARGIN_PER_POSITION": "300",
    f"{ENV_PREFIX}MAX_BROKER_MARGIN_FRACTION": "0.012",
    f"{ENV_PREFIX}STRESS_MOVE_FRACTION": "0.20",
    f"{ENV_PREFIX}MAX_STRESS_LOSS_PER_POSITION": "400",
    f"{ENV_PREFIX}MAX_STRESS_LOSS_FRACTION": "0.013",
    f"{ENV_PREFIX}QUOTE_MAXIMUM_AGE_SECONDS": "2.5",
    f"{ENV_PREFIX}MAX_TOTAL_BPR_FRACTION": "0.40",
    f"{ENV_PREFIX}MAX_INCREMENTAL_BPR_FRACTION": "0.06",
    f"{ENV_PREFIX}MAX_UNDERLYING_BPR_FRACTION": "0.11",
    f"{ENV_PREFIX}MAX_SECTOR_BPR_FRACTION": "0.16",
    f"{ENV_PREFIX}MAX_CORRELATION_GROUP_BPR_FRACTION": "0.21",
    f"{ENV_PREFIX}PORTFOLIO_SNAPSHOT_MAXIMUM_AGE_SECONDS": "90",
    f"{ENV_PREFIX}NEUTRAL_TARGET_DELTA": "0.14",
    f"{ENV_PREFIX}DIRECTIONAL_TARGET_DELTA": "0.28",
    f"{ENV_PREFIX}TARGET_WIDTH": "2.5",
    f"{ENV_PREFIX}RISK_BUDGET_PER_POSITION": "750",
    f"{ENV_PREFIX}SECTORS": "SPY:BROAD_MARKET,AAPL:TECH",
    f"{ENV_PREFIX}CORRELATION_GROUPS": "SPY:US_LARGE_CAP,AAPL:US_LARGE_CAP",
}


def fraction_policy(field: str, value: object) -> RiskPolicy:
    """One fraction field replaced, with the cross-field rule kept satisfiable.

    Raising the total cap alongside the incremental cap matters: without it a
    valid incremental of exactly 1 would be refused by the incremental<=total
    rule, and the test would pass for the wrong reason.
    """
    overrides: dict[str, object] = {field: value}
    if field == "max_incremental_bpr_fraction":
        overrides["max_total_bpr_fraction"] = D("1")
    return RiskPolicy(**overrides)  # type: ignore[arg-type]


def amount_policy(field: str, value: object) -> RiskPolicy:
    return RiskPolicy(**{field: value})  # type: ignore[arg-type]


# ===========================================================================
# Defaults
# ===========================================================================


class TestDefaults:
    def test_default_construction_succeeds(self) -> None:
        """The shipped defaults must themselves survive the validator; a
        default that cannot be constructed is a policy nobody can use."""
        policy = RiskPolicy()
        assert policy.version == POLICY_VERSION

    def test_policy_version_is_the_recorded_string(self) -> None:
        assert POLICY_VERSION == "options-risk/1"
        assert RiskPolicy().to_record()["version"] == POLICY_VERSION

    def test_defaults_are_all_decimals_not_floats(self) -> None:
        """A float threshold would drag binary rounding into a risk cap."""
        for field in FRACTION_FIELDS + AMOUNT_FIELDS + ("stress_move_fraction",):
            assert isinstance(getattr(RiskPolicy(), field), Decimal), field

    def test_default_ages_are_positive_timedeltas(self) -> None:
        policy = RiskPolicy()
        assert policy.quote_maximum_age == timedelta(seconds=10)
        assert policy.portfolio_snapshot_maximum_age == timedelta(seconds=60)

    def test_empty_version_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(version="   ")


# ===========================================================================
# Fraction boundaries -- (0, 1]
# ===========================================================================


class TestFractionBoundaries:
    def test_zero_refused_for_every_fraction(self) -> None:
        """A cap of zero disables the check it exists to perform by making it
        unsatisfiable, and reads in a journal as if it were enforced."""
        for field in FRACTION_FIELDS:
            with pytest.raises(ConfigError) as exc:
                fraction_policy(field, D("0"))
            assert field in str(exc.value), field

    def test_negative_refused_for_every_fraction(self) -> None:
        for field in FRACTION_FIELDS:
            with pytest.raises(ConfigError) as exc:
                fraction_policy(field, D("-0.01"))
            assert field in str(exc.value), field

    def test_exactly_one_accepted_for_every_fraction(self) -> None:
        """1 is the whole account, which is extreme but coherent. Refusing it
        would make the boundary off by one and reject a legitimate policy."""
        for field in FRACTION_FIELDS:
            policy = fraction_policy(field, D("1"))
            assert getattr(policy, field) == D("1"), field

    def test_above_one_refused_for_every_fraction(self) -> None:
        """15 meaning 15% is the typo this catches; unchecked it produces a
        governor that approves every candidate it is ever shown."""
        for field in FRACTION_FIELDS:
            with pytest.raises(ConfigError) as exc:
                fraction_policy(field, D("15"))
            assert field in str(exc.value), field

    def test_just_above_one_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(max_sector_bpr_fraction=D("1.0000001"))

    def test_nan_fraction_refused(self) -> None:
        """Decimal('NaN') parses without error and every comparison against it
        is False, so an unguarded <= would let it through."""
        with pytest.raises(ConfigError):
            RiskPolicy(max_sector_bpr_fraction=D("NaN"))

    def test_infinite_fraction_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(max_sector_bpr_fraction=D("Infinity"))


# ===========================================================================
# Absolute amount boundaries
# ===========================================================================


class TestAmountBoundaries:
    def test_zero_refused_for_every_amount(self) -> None:
        for field in AMOUNT_FIELDS:
            with pytest.raises(ConfigError) as exc:
                amount_policy(field, D("0"))
            assert field in str(exc.value), field

    def test_negative_refused_for_every_amount(self) -> None:
        for field in AMOUNT_FIELDS:
            with pytest.raises(ConfigError) as exc:
                amount_policy(field, D("-1"))
            assert field in str(exc.value), field

    def test_small_positive_amount_accepted(self) -> None:
        for field in AMOUNT_FIELDS:
            assert getattr(amount_policy(field, D("0.01")), field) == D("0.01"), field

    def test_amounts_have_no_upper_bound(self) -> None:
        """Unlike a fraction, a dollar amount above 1 is ordinary."""
        assert amount_policy(
            "max_defined_loss_per_position", D("100000")
        ).max_defined_loss_per_position == D("100000")

    def test_nan_amount_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(max_defined_loss_per_position=D("NaN"))

    def test_infinite_amount_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(max_defined_loss_per_position=D("Infinity"))


# ===========================================================================
# Types -- Decimal or nothing
# ===========================================================================


class TestTypeRefusals:
    def test_float_fraction_refused(self) -> None:
        """0.15 as a float is 0.1499999999999999944488848768742172978818416595458984375;
        letting one in makes two runs of the same policy divergeable."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(max_sector_bpr_fraction=0.15)  # type: ignore[arg-type]
        assert "Decimal" in str(exc.value)

    def test_int_fraction_refused(self) -> None:
        """1 is inside (0, 1] but is still not a Decimal, and int/Decimal
        mixing is where a silent float promotion starts."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(max_sector_bpr_fraction=1)  # type: ignore[arg-type]
        assert "Decimal" in str(exc.value)

    def test_float_amount_refused(self) -> None:
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(max_defined_loss_per_position=500.0)  # type: ignore[arg-type]
        assert "Decimal" in str(exc.value)

    def test_int_amount_refused(self) -> None:
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(max_defined_loss_per_position=500)  # type: ignore[arg-type]
        assert "Decimal" in str(exc.value)

    def test_string_fraction_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(max_sector_bpr_fraction="0.15")  # type: ignore[arg-type]

    def test_float_stress_move_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(stress_move_fraction=0.15)  # type: ignore[arg-type]

    def test_non_timedelta_age_refused(self) -> None:
        """Seconds-as-a-number would compare against a timedelta and raise
        somewhere far from the config that caused it."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(quote_maximum_age=10)  # type: ignore[arg-type]
        assert "timedelta" in str(exc.value)


# ===========================================================================
# The stress move -- (0, 1) exclusive on both ends
# ===========================================================================


class TestStressMoveFraction:
    def test_zero_refused(self) -> None:
        """A stress move of zero is not a stress test; it is the current price
        with a reassuring label on it."""
        with pytest.raises(ConfigError):
            RiskPolicy(stress_move_fraction=D("0"))

    def test_negative_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(stress_move_fraction=D("-0.15"))

    def test_one_refused_unlike_the_other_fractions(self) -> None:
        """A 100% adverse move takes the underlying to zero, past the point a
        terminal equity-option payoff says anything useful."""
        with pytest.raises(ConfigError):
            RiskPolicy(stress_move_fraction=D("1"))

    def test_above_one_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(stress_move_fraction=D("1.5"))

    def test_half_accepted(self) -> None:
        assert RiskPolicy(stress_move_fraction=D("0.5")).stress_move_fraction == D("0.5")

    def test_default_is_fifteen_percent(self) -> None:
        assert RiskPolicy().stress_move_fraction == D("0.15")

    def test_nan_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(stress_move_fraction=D("NaN"))


# ===========================================================================
# Strike-selection targets -- (0, 1) exclusive, like the stress move
# ===========================================================================


class TestTargetDeltas:
    def test_defaults_are_sixteen_and_thirty_delta(self) -> None:
        """The recorded strategy: 16-delta neutral, 30-delta directional."""
        policy = RiskPolicy()
        assert policy.neutral_target_delta == D("0.16")
        assert policy.directional_target_delta == D("0.30")

    def test_zero_refused_for_every_target(self) -> None:
        """A zero-delta target walks the selector to the furthest listed strike
        and sells a contract worth nothing, which collects no premium to justify
        the wing that protects it."""
        for field in TARGET_DELTA_FIELDS:
            with pytest.raises(ConfigError) as exc:
                RiskPolicy(**{field: D("0")})  # type: ignore[arg-type]
            assert field in str(exc.value), field

    def test_negative_refused_for_every_target(self) -> None:
        """The target is a magnitude. Put deltas are negative, but the number
        the operator configures is not."""
        for field in TARGET_DELTA_FIELDS:
            with pytest.raises(ConfigError) as exc:
                RiskPolicy(**{field: D("-0.16")})  # type: ignore[arg-type]
            assert field in str(exc.value), field

    def test_one_refused_for_every_target(self) -> None:
        """Unlike a fraction-of-equity cap, 1 is not coherent here: a 100-delta
        short is a synthetic position in the underlying wearing an option's
        name, not a premium-selling short strike."""
        for field in TARGET_DELTA_FIELDS:
            with pytest.raises(ConfigError) as exc:
                RiskPolicy(**{field: D("1")})  # type: ignore[arg-type]
            assert field in str(exc.value), field

    def test_above_one_refused_for_every_target(self) -> None:
        """16 meaning a 16-delta is the typo this catches; no option has a delta
        magnitude above 1."""
        for field in TARGET_DELTA_FIELDS:
            with pytest.raises(ConfigError) as exc:
                RiskPolicy(**{field: D("16")})  # type: ignore[arg-type]
            assert field in str(exc.value), field

    def test_interior_values_accepted_for_every_target(self) -> None:
        for field in TARGET_DELTA_FIELDS:
            policy = RiskPolicy(**{field: D("0.05")})  # type: ignore[arg-type]
            assert getattr(policy, field) == D("0.05"), field

    def test_just_below_one_accepted(self) -> None:
        """Extreme but coherent, and the boundary must not be off by one."""
        assert RiskPolicy(
            directional_target_delta=D("0.9999999")
        ).directional_target_delta == D("0.9999999")

    def test_nan_refused_for_every_target(self) -> None:
        """Every comparison against NaN is False, so an unguarded range check
        would accept it and the selector would then match no strike at all."""
        for field in TARGET_DELTA_FIELDS:
            with pytest.raises(ConfigError) as exc:
                RiskPolicy(**{field: D("NaN")})  # type: ignore[arg-type]
            assert "finite" in str(exc.value), field

    def test_infinite_refused_for_every_target(self) -> None:
        for field in TARGET_DELTA_FIELDS:
            with pytest.raises(ConfigError):
                RiskPolicy(**{field: D("Infinity")})  # type: ignore[arg-type]

    def test_float_refused_for_every_target(self) -> None:
        for field in TARGET_DELTA_FIELDS:
            with pytest.raises(ConfigError) as exc:
                RiskPolicy(**{field: 0.16})  # type: ignore[arg-type]
            assert "Decimal" in str(exc.value), field

    def test_the_two_targets_are_independent(self) -> None:
        """One knob shared between them would have to move in two directions the
        moment either is tuned."""
        policy = RiskPolicy(
            neutral_target_delta=D("0.10"), directional_target_delta=D("0.40")
        )
        assert policy.neutral_target_delta == D("0.10")
        assert policy.directional_target_delta == D("0.40")


# ===========================================================================
# Width and risk budget -- amounts, so only positivity binds
# ===========================================================================


class TestWidthAndRiskBudget:
    def test_defaults_are_five_wide_and_five_hundred(self) -> None:
        policy = RiskPolicy()
        assert policy.target_width == D("5")
        assert policy.risk_budget_per_position == D("500")

    def test_zero_width_refused(self) -> None:
        """A width of zero puts the protective leg on the short strike itself,
        which is no protection at all."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(target_width=D("0"))
        assert "target_width" in str(exc.value)

    def test_negative_width_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(target_width=D("-5"))

    def test_zero_risk_budget_refused(self) -> None:
        """A budget of zero sizes every candidate to nothing, which reads in a
        report as 'the market offered nothing' rather than 'the budget was
        misconfigured'."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(risk_budget_per_position=D("0"))
        assert "risk_budget_per_position" in str(exc.value)

    def test_negative_risk_budget_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(risk_budget_per_position=D("-500"))

    def test_a_sub_dollar_width_is_accepted(self) -> None:
        """Some chains list 0.50-wide strikes; the policy must not assume the
        5-point spacing of SPY."""
        assert RiskPolicy(target_width=D("0.50")).target_width == D("0.50")

    def test_a_width_above_one_is_ordinary_unlike_a_fraction(self) -> None:
        assert RiskPolicy(target_width=D("25")).target_width == D("25")

    def test_nan_and_infinite_refused(self) -> None:
        for value in (D("NaN"), D("Infinity")):
            with pytest.raises(ConfigError):
                RiskPolicy(target_width=value)
            with pytest.raises(ConfigError):
                RiskPolicy(risk_budget_per_position=value)

    def test_floats_refused(self) -> None:
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(target_width=5.0)  # type: ignore[arg-type]
        assert "Decimal" in str(exc.value)
        with pytest.raises(ConfigError):
            RiskPolicy(risk_budget_per_position=500.0)  # type: ignore[arg-type]


class TestRiskBudgetFractionOfEquity:
    def test_default_is_two_percent(self) -> None:
        assert RiskPolicy().risk_budget_fraction_of_equity == D("0.02")

    def test_none_is_an_explicit_opt_out(self) -> None:
        """Flat-dollar sizing, unconditionally -- the pre-2026-08-18 behavior,
        kept reachable rather than removed."""
        policy = RiskPolicy(risk_budget_fraction_of_equity=None)
        assert policy.risk_budget_fraction_of_equity is None

    def test_zero_refused(self) -> None:
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(risk_budget_fraction_of_equity=D("0"))
        assert "risk_budget_fraction_of_equity" in str(exc.value)

    def test_negative_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(risk_budget_fraction_of_equity=D("-0.02"))

    def test_above_one_refused(self) -> None:
        """This is a fraction of net liquidation, not a percentage -- the same
        typo guard every other fraction field on this policy has."""
        with pytest.raises(ConfigError):
            RiskPolicy(risk_budget_fraction_of_equity=D("15"))

    def test_exceeding_the_incremental_bpr_cap_is_refused(self) -> None:
        """Sizing that routinely produces a candidate the governor's own
        incremental-BPR cap refuses is a misconfiguration, not a slow day."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(
                risk_budget_fraction_of_equity=D("0.10"),
                max_incremental_bpr_fraction=D("0.05"),
            )
        assert "max_incremental_bpr_fraction" in str(exc.value)

    def test_exactly_at_the_incremental_bpr_cap_is_accepted(self) -> None:
        policy = RiskPolicy(
            risk_budget_fraction_of_equity=D("0.05"),
            max_incremental_bpr_fraction=D("0.05"),
        )
        assert policy.risk_budget_fraction_of_equity == D("0.05")

    def test_nan_and_infinite_refused(self) -> None:
        for value in (D("NaN"), D("Infinity")):
            with pytest.raises(ConfigError):
                RiskPolicy(risk_budget_fraction_of_equity=value)

    def test_records_and_describes_when_set(self) -> None:
        policy = RiskPolicy(risk_budget_fraction_of_equity=D("0.03"))
        assert policy.to_record()["risk_budget_fraction_of_equity"] == "0.03"
        assert "0.03" in policy.describe()

    def test_records_and_describes_when_off(self) -> None:
        policy = RiskPolicy(risk_budget_fraction_of_equity=None)
        assert policy.to_record()["risk_budget_fraction_of_equity"] is None
        assert "off" in policy.describe()

    def test_from_env_parses_a_decimal(self) -> None:
        env = {"IBKR_OPTIONS_RISK_BUDGET_FRACTION_OF_EQUITY": "0.04"}
        assert RiskPolicy.from_env(env=env).risk_budget_fraction_of_equity == D("0.04")

    def test_from_env_off_disables_it(self) -> None:
        env = {"IBKR_OPTIONS_RISK_BUDGET_FRACTION_OF_EQUITY": "off"}
        assert RiskPolicy.from_env(env=env).risk_budget_fraction_of_equity is None

    def test_from_env_none_disables_it_too(self) -> None:
        env = {"IBKR_OPTIONS_RISK_BUDGET_FRACTION_OF_EQUITY": "NONE"}
        assert RiskPolicy.from_env(env=env).risk_budget_fraction_of_equity is None

    def test_from_env_default_is_unset_variable(self) -> None:
        assert RiskPolicy.from_env(env={}).risk_budget_fraction_of_equity == D("0.02")

    def test_from_env_garbage_is_refused_not_silently_dropped(self) -> None:
        env = {"IBKR_OPTIONS_RISK_BUDGET_FRACTION_OF_EQUITY": "five percent"}
        with pytest.raises(ConfigError):
            RiskPolicy.from_env(env=env)


# ===========================================================================
# Maximum ages
# ===========================================================================


class TestAgeFields:
    def test_zero_refused_for_every_age(self) -> None:
        """A maximum age of zero rejects every quote, including a fresh one --
        a halt that looks like a data outage."""
        for field in AGE_FIELDS:
            with pytest.raises(ConfigError) as exc:
                RiskPolicy(**{field: timedelta(0)})  # type: ignore[arg-type]
            assert field in str(exc.value), field

    def test_negative_refused_for_every_age(self) -> None:
        for field in AGE_FIELDS:
            with pytest.raises(ConfigError) as exc:
                RiskPolicy(**{field: timedelta(seconds=-1)})  # type: ignore[arg-type]
            assert field in str(exc.value), field

    def test_positive_accepted_for_every_age(self) -> None:
        for field in AGE_FIELDS:
            policy = RiskPolicy(**{field: timedelta(seconds=3)})  # type: ignore[arg-type]
            assert getattr(policy, field) == timedelta(seconds=3), field

    def test_sub_second_age_accepted(self) -> None:
        assert RiskPolicy(
            quote_maximum_age=timedelta(milliseconds=250)
        ).quote_maximum_age == timedelta(milliseconds=250)


# ===========================================================================
# The cross-field rule: incremental <= total
# ===========================================================================


class TestIncrementalVersusTotal:
    def test_incremental_above_total_refused(self) -> None:
        """An incremental cap above the total cap can never bind, so it looks
        like a control while doing nothing at all."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(
                max_total_bpr_fraction=D("0.20"),
                max_incremental_bpr_fraction=D("0.25"),
            )
        assert "max_incremental_bpr_fraction" in str(exc.value)
        assert "max_total_bpr_fraction" in str(exc.value)

    def test_incremental_equal_to_total_accepted(self) -> None:
        """Equal means one position may use the entire book's budget -- extreme
        but coherent, and the boundary must not be off by one."""
        policy = RiskPolicy(
            max_total_bpr_fraction=D("0.20"),
            max_incremental_bpr_fraction=D("0.20"),
        )
        assert policy.max_incremental_bpr_fraction == policy.max_total_bpr_fraction

    def test_incremental_below_total_accepted(self) -> None:
        policy = RiskPolicy(
            max_total_bpr_fraction=D("0.35"),
            max_incremental_bpr_fraction=D("0.05"),
        )
        assert policy.max_incremental_bpr_fraction < policy.max_total_bpr_fraction

    def test_defaults_satisfy_the_rule(self) -> None:
        policy = RiskPolicy()
        assert policy.max_incremental_bpr_fraction <= policy.max_total_bpr_fraction


# ===========================================================================
# Classification maps
# ===========================================================================


class TestClassificationValidation:
    def test_duplicate_symbol_refused(self) -> None:
        """Two classifications for one symbol makes concentration arithmetic
        depend on which entry is read first."""
        for field in ("sectors", "correlation_groups"):
            with pytest.raises(ConfigError) as exc:
                RiskPolicy(**{field: (("SPY", "BROAD_MARKET"), ("SPY", "TECH"))})  # type: ignore[arg-type]
            assert field in str(exc.value), field

    def test_duplicate_detected_across_case_and_whitespace(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(sectors=(("SPY", "BROAD_MARKET"), (" spy ", "TECH")))

    def test_empty_symbol_refused(self) -> None:
        for field in ("sectors", "correlation_groups"):
            with pytest.raises(ConfigError):
                RiskPolicy(**{field: (("", "TECH"),)})  # type: ignore[arg-type]

    def test_whitespace_only_symbol_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(sectors=(("   ", "TECH"),))

    def test_empty_classification_refused(self) -> None:
        """A symbol mapped to '' would look classified and compare equal to
        every other blank, silently pooling unrelated names."""
        for field in ("sectors", "correlation_groups"):
            with pytest.raises(ConfigError):
                RiskPolicy(**{field: (("SPY", ""),)})  # type: ignore[arg-type]

    def test_whitespace_only_classification_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(correlation_groups=(("SPY", "  "),))

    def test_non_pair_entry_refused(self) -> None:
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(sectors=(("SPY", "BROAD_MARKET", "EXTRA"),))  # type: ignore[arg-type]
        assert "pairs" in str(exc.value)

    def test_bare_string_entry_refused(self) -> None:
        """('SPY',) forgotten as ('SPY') is a string, and iterating it would
        otherwise unpack into characters."""
        with pytest.raises(ConfigError):
            RiskPolicy(sectors=("SPY",))  # type: ignore[arg-type]

    def test_one_element_tuple_entry_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(sectors=(("SPY",),))  # type: ignore[arg-type]

    def test_list_instead_of_tuple_refused(self) -> None:
        """A list is mutable and unhashable, which would break the frozen
        policy's hashability along with its immutability."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(sectors=[("SPY", "BROAD_MARKET")])  # type: ignore[arg-type]
        assert "tuple" in str(exc.value)

    def test_empty_map_accepted_and_classifies_nothing(self) -> None:
        """Deliberate: an empty map is not refused here, because every symbol
        then fails closed at the governor rather than at construction."""
        policy = RiskPolicy(sectors=(), correlation_groups=())
        assert policy.sectors == ()
        assert policy.correlation_groups == ()
        assert policy.sector_for("SPY") is None
        assert policy.correlation_group_for("SPY") is None


# ===========================================================================
# Classification lookups
# ===========================================================================


class TestClassificationLookups:
    def test_exact_match(self) -> None:
        assert RiskPolicy().sector_for("SPY") == "BROAD_MARKET"
        assert RiskPolicy().correlation_group_for("SPY") == "US_LARGE_CAP"

    def test_lookup_is_case_insensitive(self) -> None:
        policy = RiskPolicy()
        assert policy.sector_for("aapl") == "TECHNOLOGY"
        assert policy.correlation_group_for("aapl") == "US_LARGE_CAP"

    def test_lookup_tolerates_surrounding_whitespace(self) -> None:
        """A symbol pasted from a config line carries its spaces."""
        policy = RiskPolicy()
        assert policy.sector_for("  msft \n") == "TECHNOLOGY"
        assert policy.correlation_group_for("\tMSFT ") == "US_LARGE_CAP"

    def test_unclassified_symbol_returns_none_not_a_default(self) -> None:
        """None means refuse at the governor. A fallback string here would
        silently pool every unknown ticker into one imaginary sector."""
        policy = RiskPolicy()
        assert policy.sector_for("TSLA") is None
        assert policy.correlation_group_for("TSLA") is None

    def test_lookup_matches_a_stored_key_that_has_whitespace(self) -> None:
        policy = RiskPolicy(sectors=((" nvda ", "TECHNOLOGY"),))
        assert policy.sector_for("NVDA") == "TECHNOLOGY"

    def test_empty_symbol_lookup_returns_none(self) -> None:
        assert RiskPolicy().sector_for("") is None


# ===========================================================================
# from_env
# ===========================================================================


class TestFromEnv:
    def test_empty_env_gives_the_defaults(self) -> None:
        """An unset variable must not be read as an empty threshold."""
        assert RiskPolicy.from_env(env={}) == RiskPolicy()

    def test_blank_value_falls_back_to_the_default(self) -> None:
        policy = RiskPolicy.from_env(
            env={f"{ENV_PREFIX}MAX_SECTOR_BPR_FRACTION": "   "}
        )
        assert policy.max_sector_bpr_fraction == RiskPolicy().max_sector_bpr_fraction

    def test_every_documented_variable_is_read(self) -> None:
        policy = RiskPolicy.from_env(env=dict(FULL_ENV))
        assert policy.max_defined_loss_per_position == D("250")
        assert policy.max_defined_loss_fraction == D("0.01")
        assert policy.max_broker_margin_per_position == D("300")
        assert policy.max_broker_margin_fraction == D("0.012")
        assert policy.stress_move_fraction == D("0.20")
        assert policy.max_stress_loss_per_position == D("400")
        assert policy.max_stress_loss_fraction == D("0.013")
        assert policy.quote_maximum_age == timedelta(seconds=2.5)
        assert policy.max_total_bpr_fraction == D("0.40")
        assert policy.max_incremental_bpr_fraction == D("0.06")
        assert policy.max_underlying_bpr_fraction == D("0.11")
        assert policy.max_sector_bpr_fraction == D("0.16")
        assert policy.max_correlation_group_bpr_fraction == D("0.21")
        assert policy.portfolio_snapshot_maximum_age == timedelta(seconds=90)
        assert policy.neutral_target_delta == D("0.14")
        assert policy.directional_target_delta == D("0.28")
        assert policy.target_width == D("2.5")
        assert policy.risk_budget_per_position == D("750")
        assert policy.sectors == (("SPY", "BROAD_MARKET"), ("AAPL", "TECH"))
        assert policy.correlation_groups == (
            ("SPY", "US_LARGE_CAP"),
            ("AAPL", "US_LARGE_CAP"),
        )

    def test_explicit_override_beats_the_environment(self) -> None:
        """The caller's argument is the more specific instruction; an env var
        winning would make a test or a CLI flag silently ineffective."""
        policy = RiskPolicy.from_env(
            env={f"{ENV_PREFIX}MAX_DEFINED_LOSS_FRACTION": "0.09"},
            max_defined_loss_fraction=D("0.03"),
        )
        assert policy.max_defined_loss_fraction == D("0.03")

    def test_override_beats_the_environment_for_maps_too(self) -> None:
        policy = RiskPolicy.from_env(
            env={f"{ENV_PREFIX}SECTORS": "SPY:BROAD_MARKET"},
            sectors=(("QQQ", "TECHNOLOGY"),),
        )
        assert policy.sectors == (("QQQ", "TECHNOLOGY"),)

    def test_unparseable_decimal_refused(self) -> None:
        """'0,15' or 'fifteen' must stop construction, not become a default
        that nobody notices was substituted."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy.from_env(env={f"{ENV_PREFIX}MAX_SECTOR_BPR_FRACTION": "fifteen"})
        assert "not a decimal number" in str(exc.value)

    def test_unparseable_amount_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy.from_env(
                env={f"{ENV_PREFIX}MAX_DEFINED_LOSS_PER_POSITION": "$500"}
            )

    def test_out_of_range_env_value_still_validated(self) -> None:
        """Parsing is not validation: 15 parses fine and is still a percent
        that forgot to be divided."""
        with pytest.raises(ConfigError):
            RiskPolicy.from_env(env={f"{ENV_PREFIX}MAX_SECTOR_BPR_FRACTION": "15"})

    def test_nan_env_value_refused(self) -> None:
        """Decimal('NaN') is a legal parse, so only the finiteness check
        stops it."""
        with pytest.raises(ConfigError):
            RiskPolicy.from_env(env={f"{ENV_PREFIX}MAX_SECTOR_BPR_FRACTION": "NaN"})

    def test_out_of_range_target_delta_from_env_refused(self) -> None:
        """'16' meaning a 16-delta parses fine and is still not a magnitude."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy.from_env(env={f"{ENV_PREFIX}NEUTRAL_TARGET_DELTA": "16"})
        assert "neutral_target_delta" in str(exc.value)

    def test_nonpositive_width_or_budget_from_env_refused(self) -> None:
        for key in ("TARGET_WIDTH", "RISK_BUDGET_PER_POSITION"):
            with pytest.raises(ConfigError):
                RiskPolicy.from_env(env={f"{ENV_PREFIX}{key}": "0"})

    def test_nonpositive_age_from_env_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy.from_env(env={f"{ENV_PREFIX}QUOTE_MAXIMUM_AGE_SECONDS": "0"})

    def test_non_finite_age_seconds_raises_config_error(self) -> None:
        """'NaN' and 'Infinity' both parse as valid Decimals, so without an
        explicit finiteness guard they reach timedelta() and surface as
        ValueError/OverflowError -- escaping the ConfigError contract that lets
        a caller report a configuration problem instead of crashing."""
        for value in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(ConfigError) as exc:
                RiskPolicy.from_env(
                    env={f"{ENV_PREFIX}QUOTE_MAXIMUM_AGE_SECONDS": value}
                )
            assert "finite" in str(exc.value)

        # Decimal's exponent range is far wider than float's, so this one IS a
        # finite Decimal and only becomes infinite at the float conversion --
        # a separate guard, and the reason the finiteness check alone is not
        # enough.
        with pytest.raises(ConfigError) as exc:
            RiskPolicy.from_env(
                env={f"{ENV_PREFIX}QUOTE_MAXIMUM_AGE_SECONDS": "1e400"}
            )
        assert "too large" in str(exc.value)

        with pytest.raises(ConfigError):
            RiskPolicy.from_env(
                env={f"{ENV_PREFIX}PORTFOLIO_SNAPSHOT_MAXIMUM_AGE_SECONDS": "Infinity"}
            )

    def test_sub_microsecond_age_rounds_to_zero_and_is_refused(self) -> None:
        """timedelta's resolution is a microsecond, so a positive-but-tiny
        value becomes timedelta(0) and is caught by the positivity check."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy.from_env(
                env={f"{ENV_PREFIX}QUOTE_MAXIMUM_AGE_SECONDS": "0.0000001"}
            )
        assert "must be positive" in str(exc.value)

    def test_env_is_not_read_from_the_process_when_a_dict_is_given(self) -> None:
        """Passing {} must mean 'no variables set', not 'fall back to
        os.environ' -- otherwise a developer's shell leaks into a run."""
        assert RiskPolicy.from_env(env={}).max_sector_bpr_fraction == D("0.15")


# ===========================================================================
# from_env pair parsing
# ===========================================================================


class TestFromEnvPairParsing:
    def test_pairs_parse_into_tuples(self) -> None:
        policy = RiskPolicy.from_env(
            env={f"{ENV_PREFIX}SECTORS": "SPY:BROAD_MARKET,AAPL:TECH"}
        )
        assert policy.sectors == (("SPY", "BROAD_MARKET"), ("AAPL", "TECH"))

    def test_both_sides_are_uppercased(self) -> None:
        """Lookups uppercase the query, so a lowercase stored key would never
        match anything the caller asks for."""
        policy = RiskPolicy.from_env(
            env={f"{ENV_PREFIX}SECTORS": "spy:broad_market,aapl:tech"}
        )
        assert policy.sectors == (("SPY", "BROAD_MARKET"), ("AAPL", "TECH"))
        assert policy.sector_for("SPY") == "BROAD_MARKET"

    def test_surrounding_whitespace_is_stripped(self) -> None:
        policy = RiskPolicy.from_env(
            env={f"{ENV_PREFIX}SECTORS": " SPY : BROAD_MARKET , AAPL : TECH "}
        )
        assert policy.sectors == (("SPY", "BROAD_MARKET"), ("AAPL", "TECH"))

    def test_correlation_groups_parse_the_same_way(self) -> None:
        policy = RiskPolicy.from_env(
            env={f"{ENV_PREFIX}CORRELATION_GROUPS": "spy:us_large_cap"}
        )
        assert policy.correlation_groups == (("SPY", "US_LARGE_CAP"),)

    def test_unset_map_keeps_the_default(self) -> None:
        policy = RiskPolicy.from_env(env={})
        assert policy.sectors == DEFAULT_SECTORS
        assert policy.correlation_groups == DEFAULT_CORRELATION_GROUPS

    def test_entry_without_a_colon_refused(self) -> None:
        """Dropping the malformed entry instead would leave a symbol failing
        closed later for a reason nobody connects back to this typo."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy.from_env(env={f"{ENV_PREFIX}SECTORS": "SPY BROAD_MARKET"})
        assert "SYMBOL:CLASSIFICATION" in str(exc.value)

    def test_entry_with_two_colons_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy.from_env(env={f"{ENV_PREFIX}SECTORS": "SPY:BROAD:MARKET"})

    def test_entry_with_an_empty_symbol_refused(self) -> None:
        with pytest.raises(ConfigError) as exc:
            RiskPolicy.from_env(env={f"{ENV_PREFIX}SECTORS": ":BROAD_MARKET"})
        assert "empty side" in str(exc.value)

    def test_entry_with_an_empty_classification_refused(self) -> None:
        with pytest.raises(ConfigError) as exc:
            RiskPolicy.from_env(env={f"{ENV_PREFIX}SECTORS": "SPY:"})
        assert "empty side" in str(exc.value)

    def test_malformed_correlation_group_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy.from_env(env={f"{ENV_PREFIX}CORRELATION_GROUPS": "SPY"})

    def test_duplicate_symbol_from_env_refused(self) -> None:
        """Parsing accepts the shape; __post_init__ still catches the clash."""
        with pytest.raises(ConfigError):
            RiskPolicy.from_env(env={f"{ENV_PREFIX}SECTORS": "SPY:BROAD,SPY:TECH"})

    def test_trailing_comma_is_tolerated(self) -> None:
        """An empty chunk is skipped, not treated as a malformed entry."""
        policy = RiskPolicy.from_env(env={f"{ENV_PREFIX}SECTORS": "SPY:BROAD_MARKET,"})
        assert policy.sectors == (("SPY", "BROAD_MARKET"),)


# ===========================================================================
# Immutability
# ===========================================================================


class TestImmutability:
    def test_assignment_to_a_threshold_raises(self) -> None:
        """The policy that approved a candidate must be the policy the journal
        records; a mutable threshold makes that claim unprovable."""
        policy = RiskPolicy()
        with pytest.raises(Exception):
            policy.max_sector_bpr_fraction = D("0.99")  # type: ignore[misc]
        assert policy.max_sector_bpr_fraction == D("0.15")

    def test_assignment_to_the_map_raises(self) -> None:
        policy = RiskPolicy()
        with pytest.raises(Exception):
            policy.sectors = ()  # type: ignore[misc]

    def test_policy_is_hashable(self) -> None:
        """Tuples rather than dicts, precisely so an instance can be hashed and
        used as a key in a decision record."""
        assert isinstance(hash(RiskPolicy()), int)
        assert len({RiskPolicy(), RiskPolicy()}) == 1

    def test_equal_policies_hash_equally(self) -> None:
        assert hash(RiskPolicy(max_sector_bpr_fraction=D("0.20"))) == hash(
            RiskPolicy(max_sector_bpr_fraction=D("0.20"))
        )

    def test_different_policies_are_not_equal(self) -> None:
        assert RiskPolicy() != RiskPolicy(max_sector_bpr_fraction=D("0.20"))


# ===========================================================================
# Reporting surfaces
# ===========================================================================


class TestReporting:
    def test_to_record_values_are_all_strings(self) -> None:
        """The journal is text; a Decimal leaking in would serialize
        differently depending on the writer."""
        record = RiskPolicy().to_record()
        for key, value in record.items():
            assert isinstance(key, str), key
            assert isinstance(value, str), key

    def test_to_record_round_trips_the_key_thresholds(self) -> None:
        policy = RiskPolicy(
            max_defined_loss_per_position=D("250"),
            max_sector_bpr_fraction=D("0.16"),
            stress_move_fraction=D("0.20"),
        )
        record = policy.to_record()
        assert D(record["max_defined_loss_per_position"]) == D("250")
        assert D(record["max_sector_bpr_fraction"]) == D("0.16")
        assert D(record["stress_move_fraction"]) == D("0.20")
        assert D(record["max_defined_loss_fraction"]) == policy.max_defined_loss_fraction

    def test_to_record_stores_ages_as_seconds(self) -> None:
        record = RiskPolicy(
            quote_maximum_age=timedelta(seconds=7),
            portfolio_snapshot_maximum_age=timedelta(seconds=90),
        ).to_record()
        assert float(record["quote_maximum_age_seconds"]) == 7.0
        assert float(record["portfolio_snapshot_maximum_age_seconds"]) == 90.0

    def test_to_record_covers_every_threshold_field(self) -> None:
        record = RiskPolicy().to_record()
        for field in FRACTION_FIELDS + AMOUNT_FIELDS + ("stress_move_fraction",):
            assert field in record, field

    def test_to_record_carries_the_classification_maps(self) -> None:
        """A concentration refusal is only reproducible if the mapping that
        produced the classification travels with it. Without these, a reader can
        see that TECHNOLOGY was full but not which symbols counted as
        technology at the time."""
        record = RiskPolicy().to_record()
        assert record["sectors"] == "SPY:BROAD_MARKET,AAPL:TECHNOLOGY,MSFT:TECHNOLOGY"
        assert record["correlation_groups"] == (
            "SPY:US_LARGE_CAP,AAPL:US_LARGE_CAP,MSFT:US_LARGE_CAP"
        )

    def test_to_record_round_trips_the_classification_maps_through_from_env(
        self,
    ) -> None:
        """The recorded form must be the form from_env parses, or the record is
        documentation rather than something a past decision can be rebuilt from."""
        original = RiskPolicy()
        record = original.to_record()
        rebuilt = RiskPolicy.from_env(
            env={
                f"{ENV_PREFIX}SECTORS": record["sectors"],
                f"{ENV_PREFIX}CORRELATION_GROUPS": record["correlation_groups"],
            }
        )
        assert rebuilt.sectors == original.sectors
        assert rebuilt.correlation_groups == original.correlation_groups

    def test_to_record_carries_the_selection_and_sizing_numbers(self) -> None:
        """The strikes a past decision selected are only re-derivable if the
        targets that selected them travel with the record."""
        record = RiskPolicy(
            neutral_target_delta=D("0.14"),
            directional_target_delta=D("0.28"),
            target_width=D("2.5"),
            risk_budget_per_position=D("750"),
        ).to_record()
        assert D(record["neutral_target_delta"]) == D("0.14")
        assert D(record["directional_target_delta"]) == D("0.28")
        assert D(record["target_width"]) == D("2.5")
        assert D(record["risk_budget_per_position"]) == D("750")

    def test_describe_names_the_delta_targets_and_the_budget(self) -> None:
        described = RiskPolicy().describe()
        assert "0.16" in described
        assert "0.30" in described
        assert "500" in described

    def test_describe_is_a_non_empty_string(self) -> None:
        described = RiskPolicy().describe()
        assert isinstance(described, str)
        assert described.strip()

    def test_describe_names_the_policy_version(self) -> None:
        assert POLICY_VERSION in RiskPolicy().describe()

    def test_describe_reads_no_clock_and_is_stable(self) -> None:
        """Two calls that differ would put a moving value into the scan report
        and make runs non-comparable."""
        assert RiskPolicy().describe() == RiskPolicy().describe()


# ===========================================================================
# Position management -- the profit target and the management DTE
# ===========================================================================


class TestProfitTargetFraction:
    def test_default_is_half_the_credit(self) -> None:
        assert RiskPolicy().profit_target_fraction == D("0.50")

    def test_zero_refused(self) -> None:
        """A target of zero is met the instant the position is opened, which
        would close every trade for the credit it just collected."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(profit_target_fraction=D("0"))
        assert "profit_target_fraction" in str(exc.value)

    def test_negative_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(profit_target_fraction=D("-0.50"))

    def test_one_refused_unlike_the_equity_fractions(self) -> None:
        """Taking 100% of maximum profit means waiting for the last cent of
        extrinsic value -- reachable only at expiry, which is the outcome the
        management rules exist to avoid."""
        with pytest.raises(ConfigError):
            RiskPolicy(profit_target_fraction=D("1"))

    def test_above_one_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(profit_target_fraction=D("1.5"))

    def test_interior_values_accepted(self) -> None:
        for value in ("0.01", "0.25", "0.50", "0.75", "0.99"):
            assert RiskPolicy(
                profit_target_fraction=D(value)
            ).profit_target_fraction == D(value), value

    def test_float_refused(self) -> None:
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(profit_target_fraction=0.5)  # type: ignore[arg-type]
        assert "Decimal" in str(exc.value)

    def test_nan_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(profit_target_fraction=D("NaN"))

    def test_infinite_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(profit_target_fraction=D("Infinity"))


class TestManagementDte:
    def test_default_is_twenty_one_days(self) -> None:
        assert RiskPolicy().management_dte == 21

    def test_zero_refused(self) -> None:
        """A threshold of zero only fires on expiration day itself, after the
        gamma the rule exists to avoid."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(management_dte=0)
        assert "management_dte" in str(exc.value)

    def test_negative_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(management_dte=-1)

    def test_one_accepted(self) -> None:
        assert RiskPolicy(management_dte=1).management_dte == 1

    def test_large_value_accepted(self) -> None:
        """A threshold above any DTE the engine opens is aggressive, not
        malformed; refusing it would be this module inventing a strategy."""
        assert RiskPolicy(management_dte=60).management_dte == 60

    def test_float_refused(self) -> None:
        """21.5 days is not a calendar boundary, and truncating it silently
        would enforce a threshold the operator did not write."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(management_dte=21.0)  # type: ignore[arg-type]
        assert "int" in str(exc.value)

    def test_decimal_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(management_dte=D("21"))  # type: ignore[arg-type]

    def test_bool_refused(self) -> None:
        """bool is a subclass of int, so True would pass the obvious check and
        mean 'manage every position one day before expiry'."""
        with pytest.raises(ConfigError) as exc:
            RiskPolicy(management_dte=True)  # type: ignore[arg-type]
        assert "management_dte" in str(exc.value)

    def test_string_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy(management_dte="21")  # type: ignore[arg-type]


class TestRollAtManagementDte:
    def test_default_is_exit_not_roll(self) -> None:
        """A roll is a new position wearing an old one's story; defaulting to
        it would open structures nothing validated as their own trade."""
        assert RiskPolicy().roll_at_management_dte is False

    def test_true_accepted(self) -> None:
        assert RiskPolicy(roll_at_management_dte=True).roll_at_management_dte is True

    def test_non_bool_refused(self) -> None:
        """A truthy string would enable rolling for every operator who wrote
        'no' in the wrong place."""
        for value in ("true", 1, D("1"), None):
            with pytest.raises(ConfigError) as exc:
                RiskPolicy(roll_at_management_dte=value)  # type: ignore[arg-type]
            assert "roll_at_management_dte" in str(exc.value), value


class TestManagementFromEnv:
    def test_every_management_variable_is_read(self) -> None:
        policy = RiskPolicy.from_env(
            env={
                f"{ENV_PREFIX}PROFIT_TARGET_FRACTION": "0.60",
                f"{ENV_PREFIX}MANAGEMENT_DTE": "30",
                f"{ENV_PREFIX}ROLL_AT_MANAGEMENT_DTE": "true",
            }
        )
        assert policy.profit_target_fraction == D("0.60")
        assert policy.management_dte == 30
        assert policy.roll_at_management_dte is True

    def test_unset_management_variables_keep_the_defaults(self) -> None:
        policy = RiskPolicy.from_env(env={})
        assert policy.profit_target_fraction == D("0.50")
        assert policy.management_dte == 21
        assert policy.roll_at_management_dte is False

    def test_false_is_parsed_as_false_not_as_a_truthy_string(self) -> None:
        """bool('false') is True, which would turn 'roll: false' into rolling
        every position at 21 DTE and look like a deliberate strategy."""
        for value in ("false", "FALSE", "no", "off", "0"):
            assert (
                RiskPolicy.from_env(
                    env={f"{ENV_PREFIX}ROLL_AT_MANAGEMENT_DTE": value}
                ).roll_at_management_dte
                is False
            ), value

    def test_the_true_spellings_are_all_accepted(self) -> None:
        for value in ("true", "TRUE", "yes", "on", "1"):
            assert (
                RiskPolicy.from_env(
                    env={f"{ENV_PREFIX}ROLL_AT_MANAGEMENT_DTE": value}
                ).roll_at_management_dte
                is True
            ), value

    def test_an_ambiguous_flag_is_refused(self) -> None:
        with pytest.raises(ConfigError) as exc:
            RiskPolicy.from_env(env={f"{ENV_PREFIX}ROLL_AT_MANAGEMENT_DTE": "maybe"})
        assert "not a boolean" in str(exc.value)

    def test_a_fractional_dte_is_refused_not_truncated(self) -> None:
        with pytest.raises(ConfigError) as exc:
            RiskPolicy.from_env(env={f"{ENV_PREFIX}MANAGEMENT_DTE": "21.5"})
        assert "whole number" in str(exc.value)

    def test_an_unparseable_dte_is_refused(self) -> None:
        with pytest.raises(ConfigError):
            RiskPolicy.from_env(env={f"{ENV_PREFIX}MANAGEMENT_DTE": "three weeks"})

    def test_an_out_of_range_env_value_is_still_validated(self) -> None:
        """Parsing is not validation: '0' parses fine and is still a threshold
        that only fires on expiration day."""
        with pytest.raises(ConfigError):
            RiskPolicy.from_env(env={f"{ENV_PREFIX}MANAGEMENT_DTE": "0"})
        with pytest.raises(ConfigError):
            RiskPolicy.from_env(env={f"{ENV_PREFIX}PROFIT_TARGET_FRACTION": "1"})

    def test_explicit_overrides_beat_the_environment(self) -> None:
        policy = RiskPolicy.from_env(
            env={f"{ENV_PREFIX}MANAGEMENT_DTE": "30"}, management_dte=14
        )
        assert policy.management_dte == 14


class TestManagementReporting:
    def test_to_record_carries_the_management_thresholds(self) -> None:
        record = RiskPolicy(
            profit_target_fraction=D("0.60"),
            management_dte=30,
            roll_at_management_dte=True,
        ).to_record()
        assert record["profit_target_fraction"] == "0.60"
        assert record["management_dte"] == "30"
        assert record["roll_at_management_dte"] == "True"

    def test_describe_names_the_management_rules(self) -> None:
        described = RiskPolicy().describe()
        assert "0.50" in described
        assert "21 DTE" in described
        assert "exit at" in described

    def test_describe_says_roll_when_rolling(self) -> None:
        assert "roll at" in RiskPolicy(roll_at_management_dte=True).describe()
