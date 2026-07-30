"""The price ladder: its grid, its natural, and its monotonicity.

Everything here is arithmetic against a hand-written book, so every number in an
assertion can be derived on paper from the four quotes at the top of the file.
That is deliberate: the ladder is the one part of the walk whose correctness is
fully decidable without a broker, and a test that has to be believed rather than
checked would be no better than the midpoint it exists to replace.

Two properties get the most attention, because they are the two that cost real
money when they are wrong:

* **the $3.00 tick boundary**, in both rounding directions and in all three
  regimes -- an off-grid limit is rejected by IBKR outright, and the boundary is
  where an inclusive-versus-exclusive comparison changes the answer by 5x;
* **monotonicity**, including the case where quantization collapses two rungs
  onto the same price. A walk that re-sends the price it is already resting at
  surrenders queue priority for nothing.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from uuid import uuid4

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
from engine.options.marketdata import (
    Liveness,
    MarketDataProvenance,
    MarketDataType,
    OptionQuote,
    UnderlyingQuote,
)
from engine.options.ports import StrategyQuoteSnapshot
from engine.options.pricing import (
    DEFAULT_TICK_REGIME,
    LADDER_FRACTIONS,
    PENNY_INTERVAL,
    PENNY_THROUGHOUT,
    STANDARD_INCREMENTS,
    TICK_BOUNDARY,
    TickRegime,
    build_ladder,
    midpoint_credit,
    natural_credit,
    quantize_credit,
    tick_regime_for,
)
from engine.options.proof import envelope_for

D = Decimal
NOW = dt.datetime(2026, 3, 2, 15, 30, tzinfo=dt.timezone.utc)
EXPIRY = dt.date(2026, 4, 17)

SHORT_CON_ID = 450
LONG_CON_ID = 449

# The book every ladder test is derived from. Chosen so that the four rungs are
# exactly 0.20 / 0.19 / 0.18 / 0.17 with nothing to round:
#
#   midpoint = 15.015 - 14.815 = 0.200      (short mid  - long mid)
#   natural  = 15.000 - 14.830 = 0.170      (short BID  - long ASK)
#
# and note the identity that makes the two impossible to set independently:
# midpoint - natural is exactly half the sum of the two leg spreads, here
# (0.03 + 0.03) / 2 = 0.03.
SHORT_BID = D("15.00")
SHORT_ASK = D("15.03")
LONG_BID = D("14.80")
LONG_ASK = D("14.83")

EXPECTED_RUNGS = (D("0.20"), D("0.19"), D("0.18"), D("0.17"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _provenance(generation, *, at: dt.datetime = NOW) -> MarketDataProvenance:
    return MarketDataProvenance(
        requested_type=int(MarketDataType.LIVE),
        subscription_generation=generation,
        subscribed_at=at,
        reported_type=int(MarketDataType.LIVE),
        callback_received=True,
        last_provider_event_at=at,
        last_local_receive_at=at,
    )


def book(
    *,
    short_bid: Decimal = SHORT_BID,
    short_ask: Decimal = SHORT_ASK,
    long_bid: Decimal = LONG_BID,
    long_ask: Decimal = LONG_ASK,
    at: dt.datetime = NOW,
) -> StrategyQuoteSnapshot:
    """One coherent two-leg snapshot with explicit bids and asks.

    Explicit rather than derived from a mid, because the entire point of the
    natural is that it reads the two sides separately -- a fixture that stored
    only a mid could not express the difference the module is about.
    """
    short_generation, long_generation, under_generation = uuid4(), uuid4(), uuid4()
    legs = (
        OptionQuote(
            con_id=SHORT_CON_ID,
            provenance=_provenance(short_generation, at=at),
            bid=short_bid,
            ask=short_ask,
        ),
        OptionQuote(
            con_id=LONG_CON_ID,
            provenance=_provenance(long_generation, at=at),
            bid=long_bid,
            ask=long_ask,
        ),
    )
    return StrategyQuoteSnapshot(
        underlying=UnderlyingQuote(
            symbol="SPY",
            provenance=_provenance(under_generation, at=at),
            bid=D("449.90"),
            ask=D("450.10"),
        ),
        legs=legs,
        generations=(
            ("underlying", under_generation),
            (str(SHORT_CON_ID), short_generation),
            (str(LONG_CON_ID), long_generation),
        ),
    )


def vertical(
    *, credit: str = "0.20", quantity: int = 1, underlying: str = "SPY"
) -> OptionStrategyIntent:
    """A 1-wide SPY put credit spread -- the structure of the 101-minute order."""
    legs = (
        OptionLegIntent(
            con_id=SHORT_CON_ID,
            symbol=underlying,
            expiration=EXPIRY,
            strike=D("450"),
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
            strike=D("449"),
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
        maximum_loss_per_contract=(D("1") - D(credit)) * 100,
        configuration_version="test",
        created_at=NOW,
    )


def ladder_for(intent, snapshot, *, minimum_credit: Decimal = D("0.05"), **kwargs):
    return build_ladder(
        intent,
        snapshot,
        envelope=kwargs.pop("envelope", None) or envelope_for(intent),
        minimum_credit=minimum_credit,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The tick grid
# ---------------------------------------------------------------------------


class TestTickRegimes:
    def test_spy_is_on_the_penny_grid_at_every_price(self) -> None:
        regime = tick_regime_for("SPY")
        assert regime is PENNY_THROUGHOUT
        assert regime.increment_for(D("0.20")) == D("0.01")
        assert regime.increment_for(D("12.50")) == D("0.01")

    def test_an_unlisted_class_falls_to_the_coarsest_grid(self) -> None:
        """The default must be coarse. A coarse price is legal in a fine-grid
        class; a fine price in a coarse-grid class is a rejected order."""
        regime = tick_regime_for("ZZZZ")
        assert regime is DEFAULT_TICK_REGIME is STANDARD_INCREMENTS
        assert regime.increment_for(D("0.20")) == D("0.05")
        assert regime.increment_for(D("5.00")) == D("0.10")

    def test_the_symbol_is_normalized(self) -> None:
        assert tick_regime_for("  spy ") is PENNY_THROUGHOUT
        assert tick_regime_for(None) is DEFAULT_TICK_REGIME  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "regime, below, at_or_above",
        [
            (PENNY_THROUGHOUT, D("0.01"), D("0.01")),
            (PENNY_INTERVAL, D("0.01"), D("0.05")),
            (STANDARD_INCREMENTS, D("0.05"), D("0.10")),
        ],
    )
    def test_the_boundary_is_inclusive_upward(self, regime, below, at_or_above) -> None:
        """$3.00 itself is in the UPPER tier. This is the comparison an
        off-by-one gets wrong, and it changes the increment by up to 5x."""
        assert regime.increment_for(TICK_BOUNDARY - D("0.01")) == below
        assert regime.increment_for(TICK_BOUNDARY) == at_or_above
        assert regime.increment_for(TICK_BOUNDARY + D("0.01")) == at_or_above

    def test_a_regime_whose_tiers_do_not_meet_at_the_boundary_is_refused(self) -> None:
        """The quantizer takes exactly one rounding step, which is only correct
        if both grids contain the boundary. A regime that breaks that is a
        construction error rather than a silently wrong price."""
        with pytest.raises(InvalidStrategyError, match="whole multiple"):
            TickRegime(name="broken", below=D("0.07"), at_or_above=D("0.07"))

    def test_an_inverted_pair_is_refused(self) -> None:
        with pytest.raises(InvalidStrategyError, match="finer than"):
            TickRegime(name="inverted", below=D("0.10"), at_or_above=D("0.05"))


class TestQuantization:
    def test_a_midpoint_off_the_grid_is_floored_onto_it(self) -> None:
        # (0.21 + 0.20) / 2 -- an average of two on-grid quotes is off-grid half
        # the time by construction, which is why this step is not cosmetic.
        assert quantize_credit(D("0.205"), regime=PENNY_THROUGHOUT) == D("0.20")
        assert quantize_credit(D("0.205"), regime=STANDARD_INCREMENTS) == D("0.20")
        assert quantize_credit(D("0.24"), regime=STANDARD_INCREMENTS) == D("0.20")

    def test_flooring_is_the_default_because_it_moves_toward_the_natural(self) -> None:
        floored = quantize_credit(D("0.19"), regime=STANDARD_INCREMENTS)
        assert floored == D("0.15")
        assert floored < D("0.19")

    def test_ceiling_is_available_for_a_floor_price(self) -> None:
        """A floor must round UP, or the walk's floor is not a floor: 0.13
        floored onto a nickel grid is 0.10, which is below the bound."""
        assert (
            quantize_credit(
                D("0.13"), regime=STANDARD_INCREMENTS, rounding=ROUND_CEILING
            )
            == D("0.15")
        )
        assert (
            quantize_credit(D("0.13"), regime=STANDARD_INCREMENTS, rounding=ROUND_FLOOR)
            == D("0.10")
        )

    @pytest.mark.parametrize(
        "regime, price, floored, ceiled",
        [
            # Standard: 0.05 below 3.00, 0.10 at or above.
            (STANDARD_INCREMENTS, D("2.99"), D("2.95"), D("3.00")),
            (STANDARD_INCREMENTS, D("3.00"), D("3.00"), D("3.00")),
            (STANDARD_INCREMENTS, D("3.01"), D("3.00"), D("3.10")),
            (STANDARD_INCREMENTS, D("3.05"), D("3.00"), D("3.10")),
            # Penny interval: 0.01 below 3.00, 0.05 at or above.
            (PENNY_INTERVAL, D("2.99"), D("2.99"), D("2.99")),
            (PENNY_INTERVAL, D("2.995"), D("2.99"), D("3.00")),
            (PENNY_INTERVAL, D("3.00"), D("3.00"), D("3.00")),
            (PENNY_INTERVAL, D("3.04"), D("3.00"), D("3.05")),
            # Penny throughout: no tier change at all.
            (PENNY_THROUGHOUT, D("2.99"), D("2.99"), D("2.99")),
            (PENNY_THROUGHOUT, D("3.00"), D("3.00"), D("3.00")),
            (PENNY_THROUGHOUT, D("3.004"), D("3.00"), D("3.01")),
        ],
    )
    def test_the_three_dollar_boundary_in_both_directions(
        self, regime, price, floored, ceiled
    ) -> None:
        assert quantize_credit(price, regime=regime, rounding=ROUND_FLOOR) == floored
        assert quantize_credit(price, regime=regime, rounding=ROUND_CEILING) == ceiled

    def test_a_quantized_price_lands_exactly_on_the_grid(self) -> None:
        """Not merely close. IBKR compares against the grid, not against a
        tolerance, and Decimal makes exactness checkable."""
        for regime in (PENNY_THROUGHOUT, PENNY_INTERVAL, STANDARD_INCREMENTS):
            for cents in range(1, 800, 7):
                price = D(cents) / D("100")
                result = quantize_credit(price, regime=regime)
                increment = regime.increment_for(result)
                assert (result / increment) % 1 == 0, (regime.name, price, result)
                assert result <= price

    def test_a_negative_or_non_finite_credit_is_refused(self) -> None:
        with pytest.raises(InvalidStrategyError, match="must not be negative"):
            quantize_credit(D("-0.05"), regime=PENNY_THROUGHOUT)
        with pytest.raises(InvalidStrategyError, match="finite"):
            quantize_credit(D("NaN"), regime=PENNY_THROUGHOUT)


# ---------------------------------------------------------------------------
# Reading a book
# ---------------------------------------------------------------------------


class TestNaturalAndMidpoint:
    def test_natural_is_short_bid_minus_long_ask(self) -> None:
        """The definition, pinned as arithmetic rather than as prose.

        Sell the short by hitting its bid, buy the long by lifting its ask.
        Every other pairing describes a price that requires two counterparties
        to arrive independently, which is not a price -- it is a hope.
        """
        assert natural_credit(vertical(), book()) == SHORT_BID - LONG_ASK == D("0.17")

    def test_the_natural_is_never_above_the_midpoint_on_an_uncrossed_book(self) -> None:
        intent, snapshot = vertical(), book()
        assert natural_credit(intent, snapshot) < midpoint_credit(intent, snapshot)

    def test_the_wrong_pairing_would_flatter_the_price(self) -> None:
        """The mistake this module exists to prevent, made explicit: valuing the
        short at its ask and the long at its bid gives 15.03 - 14.80 = 0.23 --
        *better* than the midpoint, and unobtainable."""
        flattering = SHORT_ASK - LONG_BID
        assert flattering == D("0.23")
        assert flattering > midpoint_credit(vertical(), book())
        assert natural_credit(vertical(), book()) < flattering

    def test_a_one_sided_market_has_no_natural(self) -> None:
        assert natural_credit(vertical(), book(short_bid=None)) is None  # type: ignore[arg-type]
        assert natural_credit(vertical(), book(long_ask=None)) is None  # type: ignore[arg-type]

    def test_a_missing_leg_has_no_natural(self) -> None:
        snapshot = book()
        other = vertical()
        stripped = StrategyQuoteSnapshot(
            underlying=snapshot.underlying,
            legs=(snapshot.legs[0],),
            generations=(
                ("underlying", snapshot.generations[0][1]),
                snapshot.generations[1],
            ),
        )
        assert natural_credit(other, stripped) is None

    def test_no_snapshot_is_not_a_price(self) -> None:
        assert natural_credit(vertical(), None) is None
        assert midpoint_credit(vertical(), None) is None


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


class TestLadder:
    def test_the_four_step_walk_is_exactly_the_expected_monotone_sequence(self) -> None:
        ladder = ladder_for(vertical(), book())
        assert ladder is not None
        assert ladder.rungs == EXPECTED_RUNGS
        # ...and the sequence is genuinely descending, not four copies of one
        # price that happen to pass a non-strict comparison.
        assert len(set(ladder.rungs)) == 4
        assert all(
            later < earlier
            for earlier, later in zip(ladder.rungs, ladder.rungs[1:], strict=False)
        )

    def test_the_ladder_starts_at_the_midpoint_and_ends_at_the_natural(self) -> None:
        intent, snapshot = vertical(), book()
        ladder = ladder_for(intent, snapshot)
        assert ladder is not None
        assert ladder.rungs[0] == midpoint_credit(intent, snapshot) == D("0.20")
        assert ladder.rungs[-1] == natural_credit(intent, snapshot) == D("0.17")

    def test_the_fractions_are_thirds(self) -> None:
        assert LADDER_FRACTIONS[0] == 0
        assert LADDER_FRACTIONS[-1] == 1
        assert len(LADDER_FRACTIONS) == 4
        # One third and two thirds of a 0.03 span are 0.01 and 0.02.
        assert EXPECTED_RUNGS[1] == EXPECTED_RUNGS[0] - D("0.01")
        assert EXPECTED_RUNGS[2] == EXPECTED_RUNGS[0] - D("0.02")

    def test_every_rung_is_on_the_grid_for_the_class(self) -> None:
        ladder = ladder_for(vertical(), book())
        assert ladder is not None
        assert ladder.regime is PENNY_THROUGHOUT
        for rung in ladder.rungs:
            assert rung == quantize_credit(rung, regime=ladder.regime)

    def test_a_coarse_grid_collapses_the_middle_rungs(self) -> None:
        """On a nickel grid a three-cent span has no interior rungs at all. The
        walk must not re-send a price it is already resting at, so the duplicate
        attempts are dropped rather than repeated."""
        ladder = ladder_for(vertical(), book(), regime=STANDARD_INCREMENTS)
        assert ladder is not None
        assert len(ladder.requested) == 4
        assert ladder.rungs == (D("0.20"),)
        assert ladder.attempts == 1

    def test_the_economic_floor_can_bind_before_the_natural(self) -> None:
        """A wide market: the natural is 0.02, far below anything worth trading.
        The walk stops at the floor, and the floor is on the grid."""
        wide = book(short_ask=D("15.10"), long_bid=D("14.72"), long_ask=D("14.98"))
        intent = vertical()
        assert natural_credit(intent, wide) == D("0.02")
        ladder = ladder_for(intent, wide, minimum_credit=D("0.12"))
        assert ladder is not None
        assert ladder.rungs[-1] == ladder.floor
        assert ladder.floor >= D("0.15")  # the envelope minimum, which is higher

    def test_the_envelope_minimum_is_a_hard_floor(self) -> None:
        """Even with the economic floor set to a penny, the authorization's own
        band still stops the walk. Two independent bounds, larger wins."""
        wide = book(short_ask=D("15.10"), long_bid=D("14.72"), long_ask=D("14.98"))
        intent = vertical()
        envelope = envelope_for(intent)
        ladder = ladder_for(intent, wide, minimum_credit=D("0.01"))
        assert ladder is not None
        assert ladder.floor == envelope.minimum == D("0.15")
        assert min(ladder.rungs) >= envelope.minimum
        assert all(envelope.contains(rung) for rung in ladder.rungs)

    def test_a_crossed_book_produces_no_ladder(self) -> None:
        """Natural above midpoint means the two sides did not come from one
        market. Interpolating would produce an *increasing* sequence."""
        crossed = book(short_bid=D("15.20"), short_ask=D("15.03"))
        assert build_ladder(
            vertical(),
            crossed,
            envelope=envelope_for(vertical()),
            minimum_credit=D("0.05"),
        ) is None

    def test_an_unpriceable_leg_produces_no_ladder(self) -> None:
        assert (
            ladder_for(vertical(), book(long_ask=None)) is None  # type: ignore[arg-type]
        )
        assert ladder_for(vertical(), None) is None

    def test_a_floor_above_the_ceiling_produces_no_ladder(self) -> None:
        intent = vertical()
        assert (
            ladder_for(intent, book(), minimum_credit=D("5.00")) is None
        )

    def test_a_debit_structure_is_refused(self) -> None:
        intent = vertical()
        closing = intent.closing_intent(
            strategy_id=uuid4(),
            limit_price=D("0.10"),
            created_at=NOW,
            configuration_version="test",
            quantity=1,
        )
        with pytest.raises(InvalidStrategyError, match="credit structures"):
            build_ladder(
                closing,
                book(),
                envelope=envelope_for(intent),
                minimum_credit=D("0.05"),
            )

    def test_a_ladder_cannot_be_built_with_an_increasing_sequence(self) -> None:
        """The invariant is enforced by the type, not only by the builder, so a
        future caller assembling rungs by hand cannot skip it."""
        from engine.options.pricing import PriceLadder

        with pytest.raises(InvalidStrategyError, match="strictly decrease"):
            PriceLadder(
                start=D("0.20"),
                target=D("0.17"),
                floor=D("0.15"),
                ceiling=D("0.25"),
                regime=PENNY_THROUGHOUT,
                requested=(D("0.20"), D("0.21")),
                rungs=(D("0.20"), D("0.21")),
            )

    def test_the_ladder_records_its_own_derivation(self) -> None:
        ladder = ladder_for(vertical(), book())
        assert ladder is not None
        record = ladder.to_record()
        assert record["regime"] == "penny-throughout"
        assert record["rungs"] == ["0.20", "0.19", "0.18", "0.17"]
        assert "0.20" in ladder.describe()
