"""Position management: when to take the profit, when to get out, when to wait.

Two tests here carry more weight than the rest.

The arithmetic tests use a profit target of **0.75** as well as the default
0.50, because at exactly one half ``credit * (1 - f)`` and ``credit * f`` are
the same number -- an inverted formula passes every test written against the
default and starts closing positions for twice the credit the moment the target
is tuned.

The fail-closed tests assert an *asymmetry*: a missing mark stops the profit
target from firing and does not stop the DTE rule. A position with no market
data at 15 DTE must produce CLOSE_DTE. Treating "no data" as "hold" there is
how a spread gets carried into expiration week by a quote feed nobody noticed
had died.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID, uuid4

import pytest

from engine.errors import InvalidStrategyError
from engine.options.domain import (
    OptionLegIntent,
    OptionRight,
    OptionStrategyIntent,
    OrderAction,
    PriceEffect,
    StrategyAction,
    StrategyType,
)
from engine.options.lifecycle import (
    LifecycleRefusalReason,
    ManagementAction,
    ManagementDecision,
    ManagementReason,
    PositionMark,
    closing_intent_for,
    decide_management_action,
    profit_target_debit,
)
from engine.options.policy import RiskPolicy
from engine.options.positions import OpenPosition, PositionState

D = Decimal
NOW = dt.datetime(2026, 7, 29, 13, 0, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 7, 29)
SHORT_CON_ID = 2001
LONG_CON_ID = 2002


def position(
    *,
    credit: str = "1.50",
    dte: int = 45,
    quantity: int = 1,
    state: PositionState = PositionState.OPEN,
    strategy_id: UUID | None = None,
) -> OpenPosition:
    """A 5-wide SPY put credit spread, short 500 / long 495, ``dte`` days out.

    ``dte`` is expressed relative to :data:`TODAY`, so every calendar assertion
    below reads as the number of days it is actually testing.
    """
    identifier = strategy_id or uuid4()
    expiration = TODAY + dt.timedelta(days=dte)
    legs = (
        OptionLegIntent(
            con_id=SHORT_CON_ID,
            symbol="SPY",
            expiration=expiration,
            strike=D("500"),
            right=OptionRight.PUT,
            action=OrderAction.SELL,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
        OptionLegIntent(
            con_id=LONG_CON_ID,
            symbol="SPY",
            expiration=expiration,
            strike=D("495"),
            right=OptionRight.PUT,
            action=OrderAction.BUY,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
    )
    intent = OptionStrategyIntent(
        strategy_id=identifier,
        strategy_type=StrategyType.PUT_CREDIT_SPREAD,
        strategy_action=StrategyAction.OPEN,
        underlying="SPY",
        quantity=quantity,
        legs=legs,
        expiration=expiration,
        limit_price=D(credit),
        price_effect=PriceEffect.CREDIT,
        maximum_loss_per_contract=(D("5") - D(credit)) * 100,
        configuration_version="test",
        created_at=NOW - dt.timedelta(days=10),
    )
    # A CLOSED position must record when it closed and a ROLLED one must name
    # what it rolled into, so the companion fields travel with the state.
    extras: dict[str, Any] = {}
    if state is PositionState.CLOSED:
        extras = {"closed_at": NOW, "closing_debit": D("0.75")}
    elif state is PositionState.ROLLED:
        extras = {"closed_at": NOW, "rolled_to": uuid4()}
    elif state is PositionState.UNCERTAIN:
        # An UNCERTAIN position must say why. It is also the state where holding
        # is least obviously right -- the outcome of a transmitted order is
        # unknown, so a second order against it could duplicate a live one.
        extras = {"uncertainty": "no callback before the poll timeout"}
    return OpenPosition(
        strategy_id=identifier,
        intent=intent,
        opened_at=NOW - dt.timedelta(days=10),
        state=state,
        buying_power_reserved=D("350"),
        filled_credit=D(credit),
        **extras,
    )


def mark(
    debit: str = "0.90",
    *,
    is_live: bool = True,
    as_of: dt.datetime | None = None,
) -> PositionMark:
    return PositionMark(
        debit_to_close=D(debit),
        as_of=NOW if as_of is None else as_of,
        is_live=is_live,
    )


def decide(
    *,
    pos: OpenPosition | None = None,
    policy: RiskPolicy | None = None,
    current: PositionMark | None = None,
    today: dt.date = TODAY,
) -> ManagementDecision:
    return decide_management_action(
        pos if pos is not None else position(),
        policy=policy if policy is not None else RiskPolicy(),
        mark=current,
        now=NOW,
        today=today,
    )


# ===========================================================================
# The target arithmetic -- where an inverted formula hides
# ===========================================================================


class TestProfitTargetArithmetic:
    def test_a_one_fifty_credit_targets_a_seventy_five_cent_debit(self) -> None:
        """The stated rule, asserted literally: 50% of max profit on a 1.50
        credit means buying it back for 0.75, not selling anything further."""
        target = profit_target_debit(
            filled_credit=D("1.50"), profit_target_fraction=D("0.50")
        )
        assert target == D("0.75")

    def test_the_formula_is_one_minus_the_fraction_not_the_fraction(self) -> None:
        """At exactly 0.50 the correct and inverted formulas agree, so this
        asserts at 0.75 where they do not: the target is 0.375, and an inverted
        implementation would say 1.125 -- a debit *above* the credit collected,
        i.e. closing every winner at a loss."""
        target = profit_target_debit(
            filled_credit=D("1.50"), profit_target_fraction=D("0.75")
        )
        assert target == D("0.375")
        assert target != D("1.125")

    def test_a_richer_credit_scales_the_target(self) -> None:
        assert profit_target_debit(
            filled_credit=D("2.20"), profit_target_fraction=D("0.50")
        ) == D("1.10")

    def test_the_target_is_always_below_the_credit_collected(self) -> None:
        """Buying back for more than was collected is a loss wearing the word
        'target'; the property must hold across the whole legal range."""
        for fraction in ("0.10", "0.25", "0.50", "0.75", "0.90"):
            target = profit_target_debit(
                filled_credit=D("1.50"), profit_target_fraction=D(fraction)
            )
            assert target < D("1.50"), fraction
            assert target > D("0"), fraction


# ===========================================================================
# Rule 1 -- the profit target
# ===========================================================================


class TestProfitTargetRule:
    def test_a_mark_exactly_at_the_target_fires(self) -> None:
        """The boundary is inclusive. Off by one here means the target is only
        ever hit by a position that overshot it."""
        decision = decide(current=mark("0.75"))
        assert decision.action is ManagementAction.CLOSE_PROFIT_TARGET
        assert decision.reason_code == "LIFECYCLE_PROFIT_TARGET_REACHED"
        assert decision.target_debit == D("0.75")

    def test_one_cent_above_the_target_holds(self) -> None:
        decision = decide(current=mark("0.76"))
        assert decision.action is ManagementAction.HOLD
        assert decision.reason_code == "LIFECYCLE_PROFIT_TARGET_NOT_REACHED"
        assert decision.target_debit is None

    def test_a_mark_below_the_target_fires(self) -> None:
        decision = decide(current=mark("0.40"))
        assert decision.action is ManagementAction.CLOSE_PROFIT_TARGET
        assert decision.target_debit == D("0.75")

    def test_the_detail_names_both_numbers(self) -> None:
        """An operator reading the record must be able to see the comparison
        that fired without re-deriving it from the policy."""
        decision = decide(current=mark("0.70"))
        assert "0.70" in decision.detail
        assert "0.75" in decision.detail

    def test_an_inverted_formula_would_fire_here_and_must_not(self) -> None:
        """With a 0.75 target fraction the real target is 0.375. A mark of 0.40
        is above it and must hold -- an inverted formula computes 1.125 and
        would close this position for three times what the rule intended."""
        decision = decide(
            current=mark("0.40"),
            policy=RiskPolicy(profit_target_fraction=D("0.75")),
        )
        assert decision.action is ManagementAction.HOLD
        assert decision.reason_code == "LIFECYCLE_PROFIT_TARGET_NOT_REACHED"

    def test_a_tuned_fraction_moves_the_target(self) -> None:
        decision = decide(
            current=mark("0.37"),
            policy=RiskPolicy(profit_target_fraction=D("0.75")),
        )
        assert decision.action is ManagementAction.CLOSE_PROFIT_TARGET
        assert decision.target_debit == D("0.375")

    def test_the_target_follows_the_filled_credit_not_the_intended_one(self) -> None:
        """A structure quoted at 1.50 that fills at 1.20 has a smaller maximum
        profit, and taking half of the credit that was never received would
        wait for a price the position cannot reach."""
        filled_low = position(credit="1.20")
        decision = decide(pos=filled_low, current=mark("0.60"))
        assert decision.action is ManagementAction.CLOSE_PROFIT_TARGET
        assert decision.target_debit == D("0.60")


# ===========================================================================
# Rule 2 -- the management DTE
# ===========================================================================


class TestManagementDteRule:
    def test_exactly_at_the_threshold_fires(self) -> None:
        """21 DTE is the threshold, and the comparison is ``<=``. Excluding the
        boundary means the rule first fires at 20 and the documented number is
        not the number enforced."""
        decision = decide(pos=position(dte=21), current=mark("1.40"))
        assert decision.action is ManagementAction.CLOSE_DTE
        assert decision.reason_code == "LIFECYCLE_MANAGEMENT_DTE_REACHED"

    def test_one_day_outside_the_threshold_holds(self) -> None:
        decision = decide(pos=position(dte=22), current=mark("1.40"))
        assert decision.action is ManagementAction.HOLD
        assert decision.reason_code == "LIFECYCLE_PROFIT_TARGET_NOT_REACHED"

    def test_well_inside_the_threshold_fires(self) -> None:
        decision = decide(pos=position(dte=3), current=mark("1.40"))
        assert decision.action is ManagementAction.CLOSE_DTE

    def test_expiration_day_fires(self) -> None:
        decision = decide(pos=position(dte=0), current=mark("1.40"))
        assert decision.action is ManagementAction.CLOSE_DTE

    def test_a_tuned_threshold_is_what_is_enforced(self) -> None:
        policy = RiskPolicy(management_dte=30)
        assert (
            decide(pos=position(dte=30), current=mark("1.40"), policy=policy).action
            is ManagementAction.CLOSE_DTE
        )
        assert (
            decide(pos=position(dte=31), current=mark("1.40"), policy=policy).action
            is ManagementAction.HOLD
        )

    def test_a_dte_exit_carries_no_target_debit(self) -> None:
        """The DTE rule computes no price. Carrying one would look like a limit
        the rule endorsed, when the exit is priced against the live book."""
        decision = decide(pos=position(dte=10), current=mark("1.40"))
        assert decision.target_debit is None

    def test_the_roll_flag_produces_roll_instead_of_close(self) -> None:
        decision = decide(
            pos=position(dte=21),
            current=mark("1.40"),
            policy=RiskPolicy(roll_at_management_dte=True),
        )
        assert decision.action is ManagementAction.ROLL
        assert decision.reason_code == "LIFECYCLE_MANAGEMENT_DTE_ROLL"

    def test_the_roll_flag_changes_nothing_outside_the_threshold(self) -> None:
        """A roll is a management action, not a schedule; a position at 45 DTE
        is not rolled just because rolling is enabled."""
        decision = decide(
            pos=position(dte=45),
            current=mark("1.40"),
            policy=RiskPolicy(roll_at_management_dte=True),
        )
        assert decision.action is ManagementAction.HOLD


# ===========================================================================
# Precedence -- the profit target beats the calendar
# ===========================================================================


class TestPrecedence:
    def test_the_profit_target_wins_when_both_rules_fire(self) -> None:
        """Both exits send the same closing order, so the difference is what is
        recorded and how it is priced. Labelling a reached target as a
        defensive exit corrupts the only statistic that says whether the target
        is set correctly."""
        decision = decide(pos=position(dte=10), current=mark("0.75"))
        assert decision.action is ManagementAction.CLOSE_PROFIT_TARGET
        assert decision.reason_code == "LIFECYCLE_PROFIT_TARGET_REACHED"
        assert decision.target_debit == D("0.75")

    def test_the_profit_target_wins_over_a_roll_too(self) -> None:
        decision = decide(
            pos=position(dte=10),
            current=mark("0.50"),
            policy=RiskPolicy(roll_at_management_dte=True),
        )
        assert decision.action is ManagementAction.CLOSE_PROFIT_TARGET

    def test_the_profit_target_wins_on_expiration_day(self) -> None:
        """There is no DTE so small that a reached target becomes the worse
        exit: the position is being bought back either way."""
        decision = decide(pos=position(dte=0), current=mark("0.30"))
        assert decision.action is ManagementAction.CLOSE_PROFIT_TARGET

    def test_the_dte_rule_wins_when_only_it_fires(self) -> None:
        decision = decide(pos=position(dte=10), current=mark("0.76"))
        assert decision.action is ManagementAction.CLOSE_DTE


# ===========================================================================
# Fail closed -- and the asymmetry between the two rules
# ===========================================================================


class TestMissingMarketDataAsymmetry:
    def test_no_mark_at_fifteen_dte_still_exits(self) -> None:
        """The rule this class exists for. The DTE rule needs a calendar and
        nothing else; holding a position through expiration week because the
        quote feed died is the failure being prevented."""
        decision = decide(pos=position(dte=15), current=None)
        assert decision.action is ManagementAction.CLOSE_DTE
        assert decision.reason_code == "LIFECYCLE_MANAGEMENT_DTE_REACHED"

    def test_no_mark_at_forty_dte_holds_with_a_reason_not_a_crash(self) -> None:
        """Nothing is actionable, and the record must say the profit target was
        never evaluated rather than implying it was evaluated and missed."""
        decision = decide(pos=position(dte=40), current=None)
        assert decision.action is ManagementAction.HOLD
        assert decision.reason_code == "LIFECYCLE_NO_MARK_AVAILABLE"

    def test_the_profit_target_cannot_fire_without_a_mark(self) -> None:
        """There is no price at which a missing quote proves a 50% winner."""
        decision = decide(pos=position(dte=40), current=None)
        assert decision.action is not ManagementAction.CLOSE_PROFIT_TARGET

    def test_a_non_live_mark_cannot_take_a_profit(self) -> None:
        """Delayed or frozen data must not decide when money is taken off the
        table, even when the number it shows is at the target."""
        decision = decide(pos=position(dte=40), current=mark("0.40", is_live=False))
        assert decision.action is ManagementAction.HOLD
        assert decision.reason_code == "LIFECYCLE_MARK_NOT_LIVE"

    def test_a_non_live_mark_does_not_stop_the_dte_exit(self) -> None:
        decision = decide(pos=position(dte=15), current=mark("1.40", is_live=False))
        assert decision.action is ManagementAction.CLOSE_DTE

    def test_a_stale_mark_cannot_take_a_profit(self) -> None:
        stale = mark("0.40", as_of=NOW - dt.timedelta(minutes=5))
        decision = decide(pos=position(dte=40), current=stale)
        assert decision.action is ManagementAction.HOLD
        assert decision.reason_code == "LIFECYCLE_MARK_STALE"

    def test_the_staleness_boundary_is_inclusive(self) -> None:
        """A quote exactly at the maximum age is current; refusing it would
        narrow every window by one tick of the clock."""
        policy = RiskPolicy(quote_maximum_age=dt.timedelta(seconds=10))
        exactly = mark("0.75", as_of=NOW - dt.timedelta(seconds=10))
        assert (
            decide(current=exactly, policy=policy).action
            is ManagementAction.CLOSE_PROFIT_TARGET
        )
        over = mark("0.75", as_of=NOW - dt.timedelta(seconds=10, microseconds=1))
        assert decide(current=over, policy=policy).action is ManagementAction.HOLD

    def test_a_stale_mark_does_not_stop_the_dte_exit(self) -> None:
        stale = mark("1.40", as_of=NOW - dt.timedelta(minutes=5))
        decision = decide(pos=position(dte=15), current=stale)
        assert decision.action is ManagementAction.CLOSE_DTE

    def test_a_blind_dte_exit_says_so_in_its_detail(self) -> None:
        """The exit is right either way, but an operator needs to know it was
        taken without a price."""
        decision = decide(pos=position(dte=15), current=None)
        assert "LIFECYCLE_NO_MARK_AVAILABLE" in decision.detail


# ===========================================================================
# A position that is not OPEN is never acted on
# ===========================================================================


class TestNonOpenStates:
    def test_no_state_other_than_open_ever_acts(self) -> None:
        """A CLOSING position already has a working order; a second close
        against it is how one four-lot becomes two."""
        for state in PositionState:
            if state is PositionState.OPEN:
                continue
            decision = decide(
                pos=position(dte=5, state=state), current=mark("0.10")
            )
            assert decision.action is ManagementAction.HOLD, state
            assert decision.reason_code == "LIFECYCLE_POSITION_NOT_OPEN", state

    def test_the_state_check_beats_both_rules(self) -> None:
        """Staged so the profit target and the DTE rule would both fire; the
        state check must be reached first."""
        decision = decide(
            pos=position(dte=1, state=PositionState.CLOSING), current=mark("0.05")
        )
        assert decision.action is ManagementAction.HOLD
        assert decision.reason_code == "LIFECYCLE_POSITION_NOT_OPEN"

    def test_the_detail_names_the_state(self) -> None:
        decision = decide(pos=position(state=PositionState.CLOSING), current=mark())
        assert "CLOSING" in decision.detail


# ===========================================================================
# The decision object's own invariants
# ===========================================================================


class TestManagementDecisionInvariants:
    def test_a_profit_target_close_must_carry_its_target(self) -> None:
        """A close with no limit price is an order nobody priced."""
        with pytest.raises(ValueError, match="must carry the target debit"):
            ManagementDecision(
                action=ManagementAction.CLOSE_PROFIT_TARGET,
                position_id=uuid4(),
                reason_code="LIFECYCLE_PROFIT_TARGET_REACHED",
                detail="because",
                evaluated_at=NOW,
            )

    def test_no_other_action_may_carry_a_target(self) -> None:
        """Only the profit-target rule computes a limit; a DTE exit carrying
        one would look like a price this module endorsed."""
        with pytest.raises(ValueError, match="must not carry a target_debit"):
            ManagementDecision(
                action=ManagementAction.CLOSE_DTE,
                position_id=uuid4(),
                reason_code="LIFECYCLE_MANAGEMENT_DTE_REACHED",
                detail="because",
                evaluated_at=NOW,
                target_debit=D("0.75"),
            )

    def test_a_reason_code_is_always_required(self) -> None:
        """A decision with no code is indistinguishable from one never made."""
        with pytest.raises(ValueError, match="machine-readable reason_code"):
            ManagementDecision(
                action=ManagementAction.HOLD,
                position_id=uuid4(),
                reason_code="   ",
                detail="because",
                evaluated_at=NOW,
            )

    def test_a_decision_must_explain_itself(self) -> None:
        with pytest.raises(ValueError, match="explain itself"):
            ManagementDecision(
                action=ManagementAction.HOLD,
                position_id=uuid4(),
                reason_code="LIFECYCLE_NO_MARK_AVAILABLE",
                detail="",
                evaluated_at=NOW,
            )

    def test_evaluated_at_must_be_timezone_aware(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            ManagementDecision(
                action=ManagementAction.HOLD,
                position_id=uuid4(),
                reason_code="LIFECYCLE_NO_MARK_AVAILABLE",
                detail="because",
                evaluated_at=dt.datetime(2026, 7, 29, 13, 0),
            )

    def test_a_non_positive_target_is_refused(self) -> None:
        with pytest.raises(ValueError, match="target_debit must be positive"):
            ManagementDecision(
                action=ManagementAction.CLOSE_PROFIT_TARGET,
                position_id=uuid4(),
                reason_code="LIFECYCLE_PROFIT_TARGET_REACHED",
                detail="because",
                evaluated_at=NOW,
                target_debit=D("0"),
            )

    def test_acts_is_true_for_every_action_but_hold(self) -> None:
        assert not decide(current=mark("1.40")).acts
        assert decide(current=mark("0.10")).acts
        assert decide(pos=position(dte=5), current=mark("1.40")).acts


class TestPositionMarkInvariants:
    def test_a_negative_buy_back_price_is_refused(self) -> None:
        """A negative debit is a sign error, not a free position -- and it
        would satisfy every profit target ever configured."""
        with pytest.raises(ValueError, match="must not be negative"):
            PositionMark(debit_to_close=D("-0.10"), as_of=NOW, is_live=True)

    def test_a_naive_timestamp_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            PositionMark(
                debit_to_close=D("0.75"),
                as_of=dt.datetime(2026, 7, 29, 13, 0),
                is_live=True,
            )

    def test_a_float_price_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Decimal"):
            PositionMark(debit_to_close=0.75, as_of=NOW, is_live=True)  # type: ignore[arg-type]

    def test_a_nan_price_is_refused(self) -> None:
        """Every comparison against NaN is False, so an unguarded ``<=`` reads
        it as 'not at target' forever."""
        with pytest.raises(ValueError, match="finite"):
            PositionMark(debit_to_close=D("NaN"), as_of=NOW, is_live=True)

    def test_a_zero_mark_is_allowed_and_fires_the_target(self) -> None:
        """A structure that has gone worthless is the best case, not an error."""
        decision = decide(current=mark("0"))
        assert decision.action is ManagementAction.CLOSE_PROFIT_TARGET


# ===========================================================================
# Determinism -- no clock, no hidden state
# ===========================================================================


class TestDeterminism:
    def test_the_same_inputs_produce_an_identical_decision(self) -> None:
        """A decision that differs between two identical calls cannot be
        reproduced from the record it wrote."""
        pos = position(dte=15)
        first = decide(pos=pos, current=mark("0.80"))
        second = decide(pos=pos, current=mark("0.80"))
        assert first == second

    def test_evaluated_at_is_the_supplied_time_not_a_clock_read(self) -> None:
        assert decide(current=mark()).evaluated_at == NOW

    def test_today_is_what_counts_the_days(self) -> None:
        """Passing a different ``today`` moves the DTE boundary and nothing
        else -- which is what makes a backtest possible."""
        pos = position(dte=45)
        assert decide(pos=pos, current=mark("1.40")).action is ManagementAction.HOLD
        later = TODAY + dt.timedelta(days=25)  # 20 DTE
        assert (
            decide(pos=pos, current=mark("1.40"), today=later).action
            is ManagementAction.CLOSE_DTE
        )


# ===========================================================================
# The closing order
# ===========================================================================


class TestClosingIntent:
    def _closing(self, decision: ManagementDecision, pos: OpenPosition, **kwargs: Any):
        return closing_intent_for(
            decision,
            pos,
            strategy_id=uuid4(),
            created_at=NOW,
            configuration_version="test",
            **{"quantity": pos.manageable_quantity, **kwargs},
        )

    def test_it_is_built_from_the_position_legs_inverted(self) -> None:
        """Same contracts, opposite actions. A close re-derived from the chain
        could land on a strike the position never held."""
        pos = position()
        intent = self._closing(decide(pos=pos, current=mark("0.75")), pos)
        assert intent.strategy_action is StrategyAction.CLOSE
        assert intent.price_effect is PriceEffect.DEBIT
        assert [leg.con_id for leg in intent.legs] == [SHORT_CON_ID, LONG_CON_ID]
        actions = {leg.con_id: leg.action for leg in intent.legs}
        assert actions[SHORT_CON_ID] is OrderAction.BUY
        assert actions[LONG_CON_ID] is OrderAction.SELL

    def test_it_names_the_strategy_it_retires(self) -> None:
        pos = position()
        intent = self._closing(decide(pos=pos, current=mark("0.75")), pos)
        assert intent.closes_strategy_id == pos.strategy_id

    def test_the_limit_defaults_to_the_computed_target(self) -> None:
        pos = position()
        intent = self._closing(decide(pos=pos, current=mark("0.75")), pos)
        assert intent.limit_price == D("0.75")

    def test_an_explicit_limit_wins(self) -> None:
        pos = position()
        intent = self._closing(
            decide(pos=pos, current=mark("0.75")), pos, limit_price=D("0.80")
        )
        assert intent.limit_price == D("0.80")

    def test_a_dte_exit_needs_a_limit_from_the_caller(self) -> None:
        """The DTE rule computes no price, and inventing one here would send a
        defensive exit at a number nothing in the market produced."""
        pos = position(dte=10)
        decision = decide(pos=pos, current=mark("1.40"))
        with pytest.raises(InvalidStrategyError, match="carries no target debit"):
            self._closing(decision, pos)

    def test_a_dte_exit_builds_once_a_limit_is_supplied(self) -> None:
        pos = position(dte=10)
        decision = decide(pos=pos, current=mark("1.40"))
        intent = self._closing(decision, pos, limit_price=D("1.45"))
        assert intent.limit_price == D("1.45")
        assert intent.strategy_action is StrategyAction.CLOSE

    def test_a_roll_decision_produces_the_closing_half(self) -> None:
        """A roll is a close plus a separately validated open; this builds the
        close, and the open is not this function's business."""
        pos = position(dte=10)
        decision = decide(
            pos=pos,
            current=mark("1.40"),
            policy=RiskPolicy(roll_at_management_dte=True),
        )
        intent = self._closing(decision, pos, limit_price=D("1.45"))
        assert intent.strategy_action is StrategyAction.CLOSE

    def test_a_hold_produces_no_order(self) -> None:
        pos = position()
        decision = decide(pos=pos, current=mark("1.40"))
        with pytest.raises(InvalidStrategyError, match="produces no order"):
            self._closing(decision, pos)

    def test_a_decision_about_another_position_is_refused(self) -> None:
        """The guard against closing position A with position B's decision --
        the legs would come from B and the record would name A."""
        one, two = position(), position()
        decision = decide(pos=one, current=mark("0.75"))
        with pytest.raises(InvalidStrategyError, match="names position"):
            self._closing(decision, two)

    def test_a_partial_close_is_allowed(self) -> None:
        pos = position(quantity=3)
        intent = self._closing(
            decide(pos=pos, current=mark("0.75")), pos, quantity=1
        )
        assert intent.quantity == 1

    def test_closing_more_than_is_held_is_refused(self) -> None:
        """A defensive action never increases contract count."""
        pos = position(quantity=1)
        with pytest.raises(InvalidStrategyError):
            self._closing(decide(pos=pos, current=mark("0.75")), pos, quantity=5)


# ===========================================================================
# The record
# ===========================================================================


class TestDecisionRecord:
    def test_to_record_is_json_shaped(self) -> None:
        record = decide(current=mark("0.75")).to_record()
        assert record["action"] == "CLOSE_PROFIT_TARGET"
        assert record["reason"] == "LIFECYCLE_PROFIT_TARGET_REACHED"
        assert record["target_debit"] == "0.7500"
        assert record["evaluated_at"] == NOW.isoformat()
        for key in ("position_id", "action", "reason", "detail", "evaluated_at"):
            assert isinstance(record[key], str), key

    def test_a_hold_still_records_why(self) -> None:
        record = decide(pos=position(dte=40), current=None).to_record()
        assert record["action"] == "HOLD"
        assert record["reason"] == "LIFECYCLE_NO_MARK_AVAILABLE"
        assert record["target_debit"] is None

    def test_the_record_names_the_position(self) -> None:
        pos = position()
        record = decide(pos=pos, current=mark("1.40")).to_record()
        assert record["position_id"] == str(pos.strategy_id)

    def test_describe_is_a_non_empty_line(self) -> None:
        described = decide(current=mark("0.75")).describe()
        assert "CLOSE_PROFIT_TARGET" in described
        assert "LIFECYCLE_PROFIT_TARGET_REACHED" in described


# ===========================================================================
# Every lifecycle code must be reachable
# ===========================================================================


def _produce_position_not_open() -> str | None:
    return decide(
        pos=position(state=PositionState.CLOSING), current=mark("0.75")
    ).reason_code


def _produce_no_mark_available() -> str | None:
    return decide(pos=position(dte=45), current=None).reason_code


def _produce_mark_not_live() -> str | None:
    return decide(
        pos=position(dte=45), current=mark("0.40", is_live=False)
    ).reason_code


def _produce_mark_stale() -> str | None:
    return decide(
        pos=position(dte=45),
        current=mark("0.40", as_of=NOW - dt.timedelta(minutes=5)),
    ).reason_code


def _produce_profit_target_not_reached() -> str | None:
    return decide(pos=position(dte=45), current=mark("1.40")).reason_code


def _produce_profit_target_reached() -> str | None:
    return decide(pos=position(dte=45), current=mark("0.75")).reason_code


def _produce_management_dte_reached() -> str | None:
    return decide(pos=position(dte=21), current=mark("1.40")).reason_code


def _produce_management_dte_roll() -> str | None:
    return decide(
        pos=position(dte=21),
        current=mark("1.40"),
        policy=RiskPolicy(roll_at_management_dte=True),
    ).reason_code


#: Every member of LifecycleRefusalReason, mapped to something that runs the
#: real decision function and produces it. Matching enum members against test
#: method *names* would prove only that somebody typed the name.
REFUSAL_PRODUCERS: dict[LifecycleRefusalReason, Callable[[], str | None]] = {
    LifecycleRefusalReason.POSITION_NOT_OPEN: _produce_position_not_open,
    LifecycleRefusalReason.NO_MARK_AVAILABLE: _produce_no_mark_available,
    LifecycleRefusalReason.MARK_NOT_LIVE: _produce_mark_not_live,
    LifecycleRefusalReason.MARK_STALE: _produce_mark_stale,
    LifecycleRefusalReason.PROFIT_TARGET_NOT_REACHED: (
        _produce_profit_target_not_reached
    ),
}

#: The same discipline for the codes that accompany an action.
ACTION_PRODUCERS: dict[ManagementReason, Callable[[], str | None]] = {
    ManagementReason.PROFIT_TARGET_REACHED: _produce_profit_target_reached,
    ManagementReason.MANAGEMENT_DTE_REACHED: _produce_management_dte_reached,
    ManagementReason.MANAGEMENT_DTE_ROLL: _produce_management_dte_roll,
}


class TestEveryReasonIsReachable:
    def test_the_refusal_producer_table_covers_the_whole_enum(self) -> None:
        """Adding a code without something that reaches it fails here, rather
        than shipping a branch nobody has ever executed."""
        assert set(REFUSAL_PRODUCERS) == set(LifecycleRefusalReason)

    def test_the_action_producer_table_covers_the_whole_enum(self) -> None:
        assert set(ACTION_PRODUCERS) == set(ManagementReason)

    @pytest.mark.parametrize(
        "reason", sorted(LifecycleRefusalReason, key=lambda r: r.value)
    )
    def test_each_refusal_is_actually_produced(
        self, reason: LifecycleRefusalReason
    ) -> None:
        assert REFUSAL_PRODUCERS[reason]() == reason.value

    @pytest.mark.parametrize("reason", sorted(ManagementReason, key=lambda r: r.value))
    def test_each_action_reason_is_actually_produced(
        self, reason: ManagementReason
    ) -> None:
        assert ACTION_PRODUCERS[reason]() == reason.value

    def test_every_action_is_produced_by_the_real_decider(self) -> None:
        """The action enum gets the same treatment: a member no rule can emit
        is a state the rest of the engine would branch on and never see."""
        produced = {
            decide(pos=position(dte=45), current=mark("1.40")).action,
            decide(pos=position(dte=45), current=mark("0.75")).action,
            decide(pos=position(dte=10), current=mark("1.40")).action,
            decide(
                pos=position(dte=10),
                current=mark("1.40"),
                policy=RiskPolicy(roll_at_management_dte=True),
            ).action,
        }
        assert produced == set(ManagementAction)
