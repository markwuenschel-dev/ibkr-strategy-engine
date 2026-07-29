"""Invariants of the options domain model.

Every test here asserts a structure that must NOT be constructible, or a risk
number that must come out exactly. The domain refuses at construction, so each
negative case is a ``pytest.raises`` around the constructor itself -- there is
no "build then validate" seam for a caller to skip.

No broker, no network, no fixtures from conftest: these are pure value objects.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from engine.errors import EXIT_REFUSED, InvalidStrategyError, RefusedError
from engine.options.domain import (
    OptionLegIntent,
    OptionRight,
    OptionStrategyIntent,
    OrderAction,
    PriceEffect,
    StrategyAction,
    StrategyType,
    compute_maximum_loss_per_contract,
)

UNDERLYING = "SPY"
EXPIRY = date(2026, 9, 18)
MULTIPLIER = 100
NOW = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)
CONFIG_VERSION = "options-test-1"

D = Decimal


def leg(
    con_id: int,
    strike: str,
    right: OptionRight,
    action: OrderAction,
    *,
    expiration: date = EXPIRY,
    multiplier: int = MULTIPLIER,
    ratio: int = 1,
    symbol: str = UNDERLYING,
) -> OptionLegIntent:
    return OptionLegIntent(
        con_id=con_id,
        symbol=symbol,
        expiration=expiration,
        strike=D(strike),
        right=right,
        action=action,
        ratio=ratio,
        multiplier=multiplier,
        exchange="SMART",
        trading_class=symbol,
    )


def strategy(
    strategy_type: StrategyType,
    legs: tuple[OptionLegIntent, ...],
    credit: str,
    max_loss: str,
    *,
    quantity: int = 1,
    action: StrategyAction = StrategyAction.OPEN,
    price_effect: PriceEffect = PriceEffect.CREDIT,
    expiration: date = EXPIRY,
    closes: UUID | None = None,
    created_at: datetime = NOW,
    underlying: str = UNDERLYING,
) -> OptionStrategyIntent:
    return OptionStrategyIntent(
        strategy_id=uuid4(),
        strategy_type=strategy_type,
        strategy_action=action,
        underlying=underlying,
        quantity=quantity,
        legs=legs,
        expiration=expiration,
        limit_price=D(credit),
        price_effect=price_effect,
        maximum_loss_per_contract=D(max_loss),
        configuration_version=CONFIG_VERSION,
        created_at=created_at,
        closes_strategy_id=closes,
    )


# -- canonical, valid structures --------------------------------------------


def put_credit_spread_legs() -> tuple[OptionLegIntent, ...]:
    """Short 500 put, long 495 put. Width 5."""
    return (
        leg(1001, "500", OptionRight.PUT, OrderAction.SELL),
        leg(1002, "495", OptionRight.PUT, OrderAction.BUY),
    )


def call_credit_spread_legs() -> tuple[OptionLegIntent, ...]:
    """Short 520 call, long 525 call. Width 5."""
    return (
        leg(2001, "520", OptionRight.CALL, OrderAction.SELL),
        leg(2002, "525", OptionRight.CALL, OrderAction.BUY),
    )


def iron_condor_legs(
    *, put_long: str = "490", call_long: str = "530"
) -> tuple[OptionLegIntent, ...]:
    """Short 500p / short 520c, with configurable wing widths."""
    return (
        leg(3001, "500", OptionRight.PUT, OrderAction.SELL),
        leg(3002, put_long, OptionRight.PUT, OrderAction.BUY),
        leg(3003, "520", OptionRight.CALL, OrderAction.SELL),
        leg(3004, call_long, OptionRight.CALL, OrderAction.BUY),
    )


def a_put_credit_spread(**kwargs) -> OptionStrategyIntent:
    return strategy(
        StrategyType.PUT_CREDIT_SPREAD,
        put_credit_spread_legs(),
        "1.50",
        "350",
        **kwargs,
    )


# ===========================================================================
# Leg-level invariants
# ===========================================================================


class TestLegInvariants:
    def test_qualified_contract_id_is_required(self) -> None:
        """con_id 0 is what an unqualified ib_async Contract carries."""
        with pytest.raises(InvalidStrategyError, match="con_id must be positive"):
            leg(0, "500", OptionRight.PUT, OrderAction.SELL)

    def test_negative_contract_id_rejected(self) -> None:
        with pytest.raises(InvalidStrategyError, match="con_id must be positive"):
            leg(-1, "500", OptionRight.PUT, OrderAction.SELL)

    def test_ratio_must_be_positive(self) -> None:
        with pytest.raises(InvalidStrategyError, match="ratio must be positive"):
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL, ratio=0)

    def test_zero_multiplier_rejected(self) -> None:
        with pytest.raises(InvalidStrategyError, match="multiplier must be positive"):
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL, multiplier=0)

    def test_negative_multiplier_rejected(self) -> None:
        with pytest.raises(InvalidStrategyError, match="multiplier must be positive"):
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL, multiplier=-100)

    def test_multiplier_has_no_default(self) -> None:
        """It must come from the qualified contract, never be assumed to be 100."""
        with pytest.raises(TypeError):
            OptionLegIntent(  # type: ignore[call-arg]
                con_id=1001,
                symbol=UNDERLYING,
                expiration=EXPIRY,
                strike=D("500"),
                right=OptionRight.PUT,
                action=OrderAction.SELL,
                ratio=1,
                exchange="SMART",
            )

    def test_bool_is_not_an_acceptable_ratio(self) -> None:
        """isinstance(True, int) is True in Python; a bool must not pass as 1."""
        with pytest.raises(InvalidStrategyError, match="ratio must be an int"):
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL, ratio=True)  # type: ignore[arg-type]

    def test_float_strike_rejected(self) -> None:
        with pytest.raises(InvalidStrategyError, match="strike must be a Decimal"):
            OptionLegIntent(
                con_id=1001,
                symbol=UNDERLYING,
                expiration=EXPIRY,
                strike=500.0,  # type: ignore[arg-type]
                right=OptionRight.PUT,
                action=OrderAction.SELL,
                ratio=1,
                multiplier=MULTIPLIER,
                exchange="SMART",
            )

    def test_nonpositive_strike_rejected(self) -> None:
        with pytest.raises(InvalidStrategyError, match="strike must be positive"):
            leg(1001, "0", OptionRight.PUT, OrderAction.SELL)

    def test_datetime_is_not_an_expiration(self) -> None:
        """A datetime is a date subclass; accepting one would silently carry a
        timezone into a field that is compared for equality across legs."""
        with pytest.raises(InvalidStrategyError, match="must be a date, not a datetime"):
            leg(
                1001,
                "500",
                OptionRight.PUT,
                OrderAction.SELL,
                expiration=datetime(2026, 9, 18, tzinfo=timezone.utc),  # type: ignore[arg-type]
            )

    def test_raw_string_right_rejected(self) -> None:
        with pytest.raises(InvalidStrategyError, match="right must be an OptionRight"):
            OptionLegIntent(
                con_id=1001,
                symbol=UNDERLYING,
                expiration=EXPIRY,
                strike=D("500"),
                right="P",  # type: ignore[arg-type]
                action=OrderAction.SELL,
                ratio=1,
                multiplier=MULTIPLIER,
                exchange="SMART",
            )

    def test_inverted_preserves_contract_identity(self) -> None:
        original = leg(1001, "500", OptionRight.PUT, OrderAction.SELL)
        flipped = original.inverted()
        assert flipped.action is OrderAction.BUY
        assert flipped.con_id == original.con_id
        assert flipped.strike == original.strike
        assert flipped.right is original.right
        assert flipped.multiplier == original.multiplier


# ===========================================================================
# Maximum loss arithmetic
# ===========================================================================


class TestMaximumLoss:
    def test_put_credit_spread_maximum_loss(self) -> None:
        """(short - long - credit) * multiplier = (500 - 495 - 1.50) * 100."""
        assert compute_maximum_loss_per_contract(
            strategy_type=StrategyType.PUT_CREDIT_SPREAD,
            legs=put_credit_spread_legs(),
            credit=D("1.50"),
            multiplier=MULTIPLIER,
        ) == D("350.00")

    def test_call_credit_spread_maximum_loss(self) -> None:
        """(long - short - credit) * multiplier = (525 - 520 - 1.50) * 100."""
        assert compute_maximum_loss_per_contract(
            strategy_type=StrategyType.CALL_CREDIT_SPREAD,
            legs=call_credit_spread_legs(),
            credit=D("1.50"),
            multiplier=MULTIPLIER,
        ) == D("350.00")

    def test_iron_condor_uses_the_wider_wing(self) -> None:
        """Put wing 10 wide, call wing 10 wide -- symmetric baseline."""
        assert compute_maximum_loss_per_contract(
            strategy_type=StrategyType.IRON_CONDOR,
            legs=iron_condor_legs(),
            credit=D("2.00"),
            multiplier=MULTIPLIER,
        ) == D("800.00")

    def test_asymmetric_condor_uses_the_larger_wing_not_the_smaller(self) -> None:
        """Put wing 500-490 = 10; call wing 525-520 = 5. Risk is the 10."""
        legs = iron_condor_legs(put_long="490", call_long="525")
        assert compute_maximum_loss_per_contract(
            strategy_type=StrategyType.IRON_CONDOR,
            legs=legs,
            credit=D("2.00"),
            multiplier=MULTIPLIER,
        ) == D("800.00")

    def test_asymmetric_condor_the_other_way_round(self) -> None:
        """Put wing 5, call wing 15. Risk is the 15, not the 5 and not the mean."""
        legs = iron_condor_legs(put_long="495", call_long="535")
        assert compute_maximum_loss_per_contract(
            strategy_type=StrategyType.IRON_CONDOR,
            legs=legs,
            credit=D("2.00"),
            multiplier=MULTIPLIER,
        ) == D("1300.00")

    def test_multiplier_is_respected_not_assumed(self) -> None:
        """A multiplier of 10 must produce a tenth of the 100-multiplier loss."""
        legs = (
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL, multiplier=10),
            leg(1002, "495", OptionRight.PUT, OrderAction.BUY, multiplier=10),
        )
        assert compute_maximum_loss_per_contract(
            strategy_type=StrategyType.PUT_CREDIT_SPREAD,
            legs=legs,
            credit=D("1.50"),
            multiplier=10,
        ) == D("35.00")

    def test_nonpositive_credit_rejected(self) -> None:
        with pytest.raises(InvalidStrategyError, match="credit must be positive"):
            compute_maximum_loss_per_contract(
                strategy_type=StrategyType.PUT_CREDIT_SPREAD,
                legs=put_credit_spread_legs(),
                credit=D("0"),
                multiplier=MULTIPLIER,
            )

    def test_credit_at_or_above_width_rejected(self) -> None:
        """A credit >= the width is a riskless combination; IBKR rejects it too."""
        with pytest.raises(InvalidStrategyError, match="not less than the widest wing"):
            compute_maximum_loss_per_contract(
                strategy_type=StrategyType.PUT_CREDIT_SPREAD,
                legs=put_credit_spread_legs(),
                credit=D("5.00"),
                multiplier=MULTIPLIER,
            )

    def test_decimal_arithmetic_is_exact(self) -> None:
        """0.1 + 0.2 style drift must not reach a risk figure."""
        legs = (
            leg(1001, "500.30", OptionRight.PUT, OrderAction.SELL),
            leg(1002, "500.10", OptionRight.PUT, OrderAction.BUY),
        )
        loss = compute_maximum_loss_per_contract(
            strategy_type=StrategyType.PUT_CREDIT_SPREAD,
            legs=legs,
            credit=D("0.10"),
            multiplier=MULTIPLIER,
        )
        assert loss == D("10.000")


# ===========================================================================
# Strategy shape and structure
# ===========================================================================


class TestStrategyShape:
    def test_valid_put_credit_spread(self) -> None:
        intent = a_put_credit_spread()
        assert intent.strategy_type is StrategyType.PUT_CREDIT_SPREAD
        assert intent.multiplier == MULTIPLIER
        assert intent.maximum_loss_per_contract == D("350")

    def test_valid_call_credit_spread(self) -> None:
        intent = strategy(
            StrategyType.CALL_CREDIT_SPREAD, call_credit_spread_legs(), "1.50", "350"
        )
        assert intent.total_maximum_loss == D("350")

    def test_valid_iron_condor(self) -> None:
        intent = strategy(
            StrategyType.IRON_CONDOR, iron_condor_legs(), "2.00", "800"
        )
        assert len(intent.legs) == 4

    def test_asymmetric_condor_is_accepted(self) -> None:
        """Wing symmetry is not required -- only that risk uses the wider one."""
        intent = strategy(
            StrategyType.IRON_CONDOR,
            iron_condor_legs(put_long="490", call_long="525"),
            "2.00",
            "800",
        )
        assert intent.maximum_loss_per_contract == D("800")

    def test_leg_order_in_the_tuple_does_not_matter(self) -> None:
        """Structure is identified by right and action, never by position."""
        forward = iron_condor_legs()
        shuffled = (forward[3], forward[1], forward[2], forward[0])
        intent = strategy(StrategyType.IRON_CONDOR, shuffled, "2.00", "800")
        assert intent.maximum_loss_per_contract == D("800")

    def test_vertical_with_three_legs_rejected(self) -> None:
        legs = put_credit_spread_legs() + (
            leg(1003, "490", OptionRight.PUT, OrderAction.BUY),
        )
        with pytest.raises(InvalidStrategyError, match="exactly 2 legs"):
            strategy(StrategyType.PUT_CREDIT_SPREAD, legs, "1.50", "350")

    def test_condor_with_three_legs_rejected(self) -> None:
        with pytest.raises(InvalidStrategyError, match="exactly 4 legs"):
            strategy(StrategyType.IRON_CONDOR, iron_condor_legs()[:3], "2.00", "800")

    def test_missing_protective_leg_rejected(self) -> None:
        """Two shorts and no long is a naked position wearing a spread's name."""
        legs = (
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL),
            leg(1002, "495", OptionRight.PUT, OrderAction.SELL),
        )
        with pytest.raises(InvalidStrategyError, match="one short leg and one long"):
            strategy(StrategyType.PUT_CREDIT_SPREAD, legs, "1.50", "350")

    def test_condor_missing_protection_rejected(self) -> None:
        legs = (
            leg(3001, "500", OptionRight.PUT, OrderAction.SELL),
            leg(3002, "490", OptionRight.PUT, OrderAction.SELL),
            leg(3003, "520", OptionRight.CALL, OrderAction.SELL),
            leg(3004, "530", OptionRight.CALL, OrderAction.BUY),
        )
        with pytest.raises(InvalidStrategyError, match="two short and two long"):
            strategy(StrategyType.IRON_CONDOR, legs, "2.00", "800")

    def test_wrong_right_for_strategy_type_rejected(self) -> None:
        """A call has no business in a put credit spread."""
        legs = (
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL),
            leg(1002, "505", OptionRight.CALL, OrderAction.BUY),
        )
        with pytest.raises(InvalidStrategyError, match="must all be PUTs"):
            strategy(StrategyType.PUT_CREDIT_SPREAD, legs, "1.50", "350")

    def test_duplicate_contract_ids_rejected(self) -> None:
        legs = (
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL),
            leg(1001, "495", OptionRight.PUT, OrderAction.BUY),
        )
        with pytest.raises(InvalidStrategyError, match="distinct contracts"):
            strategy(StrategyType.PUT_CREDIT_SPREAD, legs, "1.50", "350")

    def test_ratio_spreads_rejected(self) -> None:
        legs = (
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL, ratio=2),
            leg(1002, "495", OptionRight.PUT, OrderAction.BUY, ratio=1),
        )
        with pytest.raises(InvalidStrategyError, match="only 1:1 structures"):
            strategy(StrategyType.PUT_CREDIT_SPREAD, legs, "1.50", "350")

    def test_mixed_multipliers_rejected(self) -> None:
        legs = (
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL, multiplier=100),
            leg(1002, "495", OptionRight.PUT, OrderAction.BUY, multiplier=10),
        )
        with pytest.raises(InvalidStrategyError, match="share one multiplier"):
            strategy(StrategyType.PUT_CREDIT_SPREAD, legs, "1.50", "350")

    def test_mixed_underlyings_rejected(self) -> None:
        legs = (
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL),
            leg(1002, "495", OptionRight.PUT, OrderAction.BUY, symbol="QQQ"),
        )
        with pytest.raises(InvalidStrategyError, match="every leg must be on"):
            strategy(StrategyType.PUT_CREDIT_SPREAD, legs, "1.50", "350")


# ===========================================================================
# Expiration coherence
# ===========================================================================


class TestExpirations:
    def test_mixed_expirations_rejected(self) -> None:
        """Calendars and diagonals are not supported structures."""
        legs = (
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL),
            leg(
                1002,
                "495",
                OptionRight.PUT,
                OrderAction.BUY,
                expiration=EXPIRY + timedelta(days=28),
            ),
        )
        with pytest.raises(InvalidStrategyError, match="share one expiration"):
            strategy(StrategyType.PUT_CREDIT_SPREAD, legs, "1.50", "350")

    def test_strategy_expiration_must_match_its_legs(self) -> None:
        with pytest.raises(InvalidStrategyError, match="does not match its legs"):
            a_put_credit_spread(expiration=EXPIRY + timedelta(days=7))


# ===========================================================================
# Strike ordering -- the reversed-wing cases
# ===========================================================================


class TestStrikeOrdering:
    def test_reversed_put_wing_rejected(self) -> None:
        """Long put ABOVE the short put is undefined risk, not a credit spread."""
        legs = (
            leg(1001, "495", OptionRight.PUT, OrderAction.SELL),
            leg(1002, "500", OptionRight.PUT, OrderAction.BUY),
        )
        with pytest.raises(InvalidStrategyError, match="long put strike .* must be below"):
            strategy(StrategyType.PUT_CREDIT_SPREAD, legs, "1.50", "350")

    def test_reversed_call_wing_rejected(self) -> None:
        legs = (
            leg(2001, "525", OptionRight.CALL, OrderAction.SELL),
            leg(2002, "520", OptionRight.CALL, OrderAction.BUY),
        )
        with pytest.raises(InvalidStrategyError, match="long call strike .* must be above"):
            strategy(StrategyType.CALL_CREDIT_SPREAD, legs, "1.50", "350")

    def test_equal_strikes_rejected(self) -> None:
        legs = (
            leg(1001, "500", OptionRight.PUT, OrderAction.SELL),
            leg(1002, "500", OptionRight.PUT, OrderAction.BUY),
        )
        with pytest.raises(InvalidStrategyError, match="must be below"):
            strategy(StrategyType.PUT_CREDIT_SPREAD, legs, "1.50", "350")

    def test_condor_with_inverted_short_strikes_rejected(self) -> None:
        """Short put above the short call is a guaranteed loss."""
        legs = (
            leg(3001, "530", OptionRight.PUT, OrderAction.SELL),
            leg(3002, "490", OptionRight.PUT, OrderAction.BUY),
            leg(3003, "500", OptionRight.CALL, OrderAction.SELL),
            leg(3004, "540", OptionRight.CALL, OrderAction.BUY),
        )
        with pytest.raises(InvalidStrategyError, match="short put .* must be below"):
            strategy(StrategyType.IRON_CONDOR, legs, "2.00", "3800")


# ===========================================================================
# Stored maximum loss cannot disagree with the legs
# ===========================================================================


class TestStoredMaximumLoss:
    def test_mismatched_maximum_loss_rejected(self) -> None:
        """The governor sizes off this number; it must not be assertable."""
        with pytest.raises(InvalidStrategyError, match="does not match the value computed"):
            strategy(
                StrategyType.PUT_CREDIT_SPREAD,
                put_credit_spread_legs(),
                "1.50",
                "100",  # understates the real 350
            )

    def test_total_maximum_loss_scales_with_quantity(self) -> None:
        intent = a_put_credit_spread(quantity=7)
        assert intent.maximum_loss_per_contract == D("350")
        assert intent.total_maximum_loss == D("2450")

    def test_total_credit_uses_multiplier_and_quantity(self) -> None:
        intent = a_put_credit_spread(quantity=3)
        assert intent.total_credit == D("450.00")

    def test_zero_quantity_rejected(self) -> None:
        with pytest.raises(InvalidStrategyError, match="quantity must be positive"):
            a_put_credit_spread(quantity=0)


# ===========================================================================
# Lifecycle: opens, closes, and the link between them
# ===========================================================================


class TestLifecycle:
    def test_open_must_collect_a_credit(self) -> None:
        with pytest.raises(InvalidStrategyError, match="must collect a credit"):
            a_put_credit_spread(price_effect=PriceEffect.DEBIT)

    def test_open_must_not_reference_a_strategy_to_close(self) -> None:
        with pytest.raises(InvalidStrategyError, match="must not reference"):
            a_put_credit_spread(closes=uuid4())

    def test_close_must_name_the_strategy_it_retires(self) -> None:
        """Closing legs come from the persisted strategy, never a fresh lookup."""
        with pytest.raises(InvalidStrategyError, match="must name the open strategy"):
            strategy(
                StrategyType.PUT_CREDIT_SPREAD,
                tuple(l.inverted() for l in put_credit_spread_legs()),
                "0.75",
                "350",
                action=StrategyAction.CLOSE,
                price_effect=PriceEffect.DEBIT,
            )

    def test_close_pays_a_debit(self) -> None:
        with pytest.raises(InvalidStrategyError, match="pays a debit"):
            strategy(
                StrategyType.PUT_CREDIT_SPREAD,
                tuple(l.inverted() for l in put_credit_spread_legs()),
                "0.75",
                "350",
                action=StrategyAction.CLOSE,
                price_effect=PriceEffect.CREDIT,
                closes=uuid4(),
            )

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(InvalidStrategyError, match="timezone-aware"):
            a_put_credit_spread(created_at=datetime(2026, 7, 29, 14, 0))


class TestClosingIntent:
    def test_closing_intent_inverts_actions_and_keeps_contracts(self) -> None:
        opened = a_put_credit_spread()
        closing = opened.closing_intent(
            strategy_id=uuid4(),
            limit_price=D("0.75"),
            created_at=NOW,
            configuration_version=CONFIG_VERSION,
        )
        assert closing.strategy_action is StrategyAction.CLOSE
        assert closing.price_effect is PriceEffect.DEBIT
        assert closing.closes_strategy_id == opened.strategy_id
        assert [l.con_id for l in closing.legs] == [l.con_id for l in opened.legs]
        assert [l.strike for l in closing.legs] == [l.strike for l in opened.legs]
        assert [l.action for l in closing.legs] == [
            OrderAction.BUY,
            OrderAction.SELL,
        ]

    def test_closing_an_iron_condor_is_constructible(self) -> None:
        """Regression: the inverted structure must not trip strike ordering,
        which is written in the opening frame where the long put sits below."""
        opened = strategy(
            StrategyType.IRON_CONDOR, iron_condor_legs(), "2.00", "800"
        )
        closing = opened.closing_intent(
            strategy_id=uuid4(),
            limit_price=D("1.00"),
            created_at=NOW,
            configuration_version=CONFIG_VERSION,
        )
        assert len(closing.legs) == 4
        assert all(l.is_long for l in closing.legs if l.strike in (D("500"), D("520")))

    def test_closing_cannot_increase_contract_count(self) -> None:
        """A defensive action never adds risk."""
        opened = a_put_credit_spread(quantity=2)
        with pytest.raises(InvalidStrategyError, match="cannot close 5 contracts"):
            opened.closing_intent(
                strategy_id=uuid4(),
                limit_price=D("0.75"),
                created_at=NOW,
                configuration_version=CONFIG_VERSION,
                quantity=5,
            )

    def test_partial_close_is_allowed(self) -> None:
        opened = a_put_credit_spread(quantity=4)
        closing = opened.closing_intent(
            strategy_id=uuid4(),
            limit_price=D("0.75"),
            created_at=NOW,
            configuration_version=CONFIG_VERSION,
            quantity=1,
        )
        assert closing.quantity == 1

    def test_only_an_open_can_be_closed(self) -> None:
        opened = a_put_credit_spread()
        closing = opened.closing_intent(
            strategy_id=uuid4(),
            limit_price=D("0.75"),
            created_at=NOW,
            configuration_version=CONFIG_VERSION,
        )
        with pytest.raises(InvalidStrategyError, match="only an opening strategy"):
            closing.closing_intent(
                strategy_id=uuid4(),
                limit_price=D("0.50"),
                created_at=NOW,
                configuration_version=CONFIG_VERSION,
            )


# ===========================================================================
# Error taxonomy
# ===========================================================================


class TestErrorTaxonomy:
    def test_invalid_strategy_is_a_refusal(self) -> None:
        """It must be catchable as a refusal, and carry the refusal exit code."""
        assert issubclass(InvalidStrategyError, RefusedError)
        assert InvalidStrategyError.exit_code == EXIT_REFUSED

    def test_invalid_strategy_is_distinguishable_from_a_plain_refusal(self) -> None:
        """A structural violation must not be swallowed by a handler written
        for 'this candidate did not pass a limit'."""
        with pytest.raises(InvalidStrategyError):
            a_put_credit_spread(quantity=0)

    def test_refusals_carry_a_hint_where_one_helps(self) -> None:
        try:
            strategy(
                StrategyType.PUT_CREDIT_SPREAD,
                put_credit_spread_legs(),
                "5.00",
                "350",
            )
        except InvalidStrategyError as exc:
            assert exc.hint is not None
            assert "riskless" in exc.hint
        else:  # pragma: no cover - the construction must fail
            pytest.fail("a credit at the full width must be refused")
