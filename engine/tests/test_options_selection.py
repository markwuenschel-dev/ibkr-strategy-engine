"""Delta strike selection and max-loss sizing: every way the wrong strike wins.

Two failure modes drive most of this file, because both are silent. A missing
delta read as ``0`` selects the furthest contract in the chain and nothing
raises; a signed comparison against a positive target ranks the whole put chain
backwards and nothing raises either. Each has a test that fails if the guard is
removed, not merely one that passes while it is there.

The last class is the load-bearing one: every :class:`StrikeSelection` the
selector produces is fed through :func:`build_vertical`, so a pair that could
not become a valid :class:`OptionStrategyIntent` fails here rather than several
layers later.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from engine.errors import InvalidStrategyError
from engine.options.chain import QualifiedOption
from engine.options.domain import (
    OptionRight,
    OptionStrategyIntent,
    OrderAction,
    PriceEffect,
    StrategyAction,
    StrategyType,
    compute_maximum_loss_per_contract,
)
from engine.options.marketdata import (
    MarketDataProvenance,
    MarketDataType,
    OptionGreeks,
    OptionQuote,
    UnderlyingQuote,
)
from engine.options.policy import RiskPolicy
from engine.options.ports import StrategyQuoteSnapshot
from engine.options.selection import (
    Bias,
    DeltaCandidate,
    StrikeSelection,
    build_vertical,
    candidates_from_snapshot,
    rights_for,
    select_short_strike,
    select_vertical,
    size_position,
    strategy_type_for,
    target_delta_for,
)

D = Decimal
NOW = dt.datetime(2026, 7, 29, 13, 0, tzinfo=dt.timezone.utc)
EXPIRY = dt.date(2026, 9, 18)
OTHER_EXPIRY = dt.date(2026, 10, 16)

#: Large enough that sizing never refuses a structure in a test that is about
#: strike selection. Tests about sizing set their own budget.
DEEP_POCKETS = RiskPolicy(risk_budget_per_position=D("1000000"))


def contract(
    strike: str,
    *,
    right: str = "P",
    expiration: dt.date = EXPIRY,
    symbol: str = "SPY",
    multiplier: int = 100,
    con_id: int | None = None,
) -> QualifiedOption:
    """One qualified contract. ``con_id`` is derived from the strike and right so
    two contracts in the same chain never collide by accident."""
    return QualifiedOption(
        con_id=con_id
        if con_id is not None
        else int(D(strike) * 10) + (100000 if right.upper().startswith("C") else 0),
        symbol=symbol,
        expiration=expiration,
        strike=D(strike),
        right=right,
        multiplier=multiplier,
        exchange="SMART",
        trading_class="SPY",
    )


def candidate(
    strike: str,
    delta: str | None,
    *,
    right: str = "P",
    expiration: dt.date = EXPIRY,
    symbol: str = "SPY",
    multiplier: int = 100,
    con_id: int | None = None,
) -> DeltaCandidate:
    return DeltaCandidate(
        contract=contract(
            strike,
            right=right,
            expiration=expiration,
            symbol=symbol,
            multiplier=multiplier,
            con_id=con_id,
        ),
        delta=None if delta is None else D(delta),
    )


def put_chain(pairs: tuple[tuple[str, str | None], ...]) -> tuple[DeltaCandidate, ...]:
    return tuple(candidate(strike, delta) for strike, delta in pairs)


def call_chain(pairs: tuple[tuple[str, str | None], ...]) -> tuple[DeltaCandidate, ...]:
    return tuple(candidate(strike, delta, right="C") for strike, delta in pairs)


#: A 5-point put chain around 500 whose deltas fall as the strike does, which is
#: what a real chain looks like. 490 is the 16-delta strike.
STANDARD_PUTS = put_chain(
    (
        ("505", "-0.55"),
        ("500", "-0.45"),
        ("495", "-0.30"),
        ("490", "-0.16"),
        ("485", "-0.09"),
        ("480", "-0.05"),
        ("475", "-0.02"),
    )
)

STANDARD_CALLS = call_chain(
    (
        ("495", "0.55"),
        ("500", "0.45"),
        ("505", "0.30"),
        ("510", "0.16"),
        ("515", "0.09"),
        ("520", "0.05"),
        ("525", "0.02"),
    )
)


# ===========================================================================
# Bias -> rights, strategy type, delta target
# ===========================================================================


class TestBias:
    def test_directional_biases_name_one_right_each(self) -> None:
        assert rights_for(Bias.BULLISH) == (OptionRight.PUT,)
        assert rights_for(Bias.BEARISH) == (OptionRight.CALL,)

    def test_neutral_names_both_sides(self) -> None:
        assert rights_for(Bias.NEUTRAL) == (OptionRight.PUT, OptionRight.CALL)

    def test_the_strategy_type_follows_the_right(self) -> None:
        assert strategy_type_for(OptionRight.PUT) is StrategyType.PUT_CREDIT_SPREAD
        assert strategy_type_for(OptionRight.CALL) is StrategyType.CALL_CREDIT_SPREAD

    def test_neutral_uses_the_further_out_target(self) -> None:
        """16-delta neutral / 30-delta directional is the recorded strategy. A
        condor is short both wings, so its shorts sit further out."""
        policy = RiskPolicy()
        assert target_delta_for(Bias.NEUTRAL, policy) == D("0.16")
        assert target_delta_for(Bias.BULLISH, policy) == D("0.30")
        assert target_delta_for(Bias.BEARISH, policy) == D("0.30")

    def test_the_targets_come_from_the_policy_not_a_constant(self) -> None:
        policy = RiskPolicy(
            neutral_target_delta=D("0.10"), directional_target_delta=D("0.25")
        )
        assert target_delta_for(Bias.NEUTRAL, policy) == D("0.10")
        assert target_delta_for(Bias.BEARISH, policy) == D("0.25")


# ===========================================================================
# The candidate universe
# ===========================================================================


class TestDeltaCandidate:
    def test_a_missing_quote_yields_no_delta_rather_than_zero(self) -> None:
        assert DeltaCandidate.from_quote(contract("490"), None).delta is None

    def test_absolute_delta_is_the_magnitude(self) -> None:
        assert candidate("490", "-0.16").absolute_delta == D("0.16")
        assert candidate("510", "0.16", right="C").absolute_delta == D("0.16")
        assert candidate("490", None).absolute_delta is None

    def test_a_candidate_without_a_delta_is_not_selectable(self) -> None:
        assert candidate("490", "-0.16").is_selectable
        assert not candidate("490", None).is_selectable

    def test_an_unrecognised_right_is_not_selectable(self) -> None:
        """A right IBKR did not send as P/C is not a side to default onto."""
        assert candidate("490", "-0.16", right="X").right is None
        assert not candidate("490", "-0.16", right="X").is_selectable

    def test_the_long_spellings_are_recognised(self) -> None:
        """IBKR uses both 'P' and 'PUT' depending on the field."""
        assert candidate("490", "-0.16", right="PUT").right is OptionRight.PUT
        assert candidate("510", "0.16", right="call").right is OptionRight.CALL


def snapshot_for(
    quotes: tuple[OptionQuote, ...], *, generations: dict[str, UUID] | None = None
) -> StrategyQuoteSnapshot:
    declared = generations or {
        **{str(quote.con_id): quote.provenance.subscription_generation for quote in quotes},
        "underlying": uuid4(),
    }
    return StrategyQuoteSnapshot(
        underlying=UnderlyingQuote(
            symbol="SPY",
            provenance=MarketDataProvenance(
                requested_type=int(MarketDataType.LIVE),
                subscription_generation=declared["underlying"],
                subscribed_at=NOW,
                reported_type=int(MarketDataType.LIVE),
                callback_received=True,
                last_provider_event_at=NOW,
                last_local_receive_at=NOW,
            ),
        ),
        legs=quotes,
        generations=tuple(declared.items()),
    )


def quote_for(con_id: int, delta: str | None) -> OptionQuote:
    generation = uuid4()
    return OptionQuote(
        con_id=con_id,
        provenance=MarketDataProvenance(
            requested_type=int(MarketDataType.LIVE),
            subscription_generation=generation,
            subscribed_at=NOW,
            reported_type=int(MarketDataType.LIVE),
            callback_received=True,
            last_provider_event_at=NOW,
            last_local_receive_at=NOW,
        ),
        greeks=OptionGreeks(
            received_at=NOW,
            subscription_generation=generation,
            delta=None if delta is None else D(delta),
        ),
    )


class TestCandidatesFromSnapshot:
    def test_contracts_are_joined_to_quotes_on_con_id(self) -> None:
        contracts = [contract("490"), contract("485")]
        snapshot = snapshot_for(
            (
                quote_for(contracts[1].con_id, "-0.09"),
                quote_for(contracts[0].con_id, "-0.16"),
            )
        )
        joined = candidates_from_snapshot(contracts, snapshot)
        assert [c.contract.strike for c in joined] == [D("490"), D("485")]
        assert [c.delta for c in joined] == [D("-0.16"), D("-0.09")]

    def test_a_contract_with_no_quote_keeps_its_place_with_no_delta(self) -> None:
        """It stays in the sequence so a caller can count what it saw, and is
        skipped by every selector."""
        contracts = [contract("490"), contract("485")]
        snapshot = snapshot_for((quote_for(contracts[0].con_id, "-0.16"),))
        joined = candidates_from_snapshot(contracts, snapshot)
        assert joined[1].delta is None
        assert not joined[1].is_selectable

    def test_a_quote_whose_greeks_have_no_delta_yields_none(self) -> None:
        """modelGreeks is assigned even when every field sanitizes away."""
        one = contract("490")
        joined = candidates_from_snapshot(
            [one], snapshot_for((quote_for(one.con_id, None),))
        )
        assert joined[0].delta is None


# ===========================================================================
# Short strike selection
# ===========================================================================


class TestShortStrikeByDelta:
    def test_an_exact_delta_match_is_selected(self) -> None:
        chosen = select_short_strike(
            STANDARD_PUTS, target_delta=D("0.16"), right=OptionRight.PUT
        )
        assert chosen is not None
        assert chosen.contract.strike == D("490")
        assert chosen.delta == D("-0.16")

    def test_the_nearest_delta_is_selected_when_none_matches(self) -> None:
        chain = put_chain((("495", "-0.30"), ("490", "-0.19"), ("485", "-0.09")))
        chosen = select_short_strike(chain, target_delta=D("0.16"), right=OptionRight.PUT)
        assert chosen is not None
        assert chosen.contract.strike == D("490")

    def test_a_tie_breaks_toward_the_further_out_of_the_money_strike(self) -> None:
        """A 14-delta and an 18-delta are equally distant from a 16-delta target
        and are not equally risky. The lower magnitude is further out of the
        money: lower probability of finishing in the money, smaller loss at any
        given adverse move."""
        chain = put_chain((("495", "-0.18"), ("490", "-0.14")))
        chosen = select_short_strike(chain, target_delta=D("0.16"), right=OptionRight.PUT)
        assert chosen is not None
        assert chosen.delta == D("-0.14")
        assert chosen.contract.strike == D("490")

    def test_a_tie_breaks_the_same_way_on_the_call_side(self) -> None:
        """Further out of the money for a call is the *higher* strike, so the
        conservative choice moves the opposite way along the chain."""
        chain = call_chain((("505", "0.18"), ("510", "0.14")))
        chosen = select_short_strike(
            chain, target_delta=D("0.16"), right=OptionRight.CALL
        )
        assert chosen is not None
        assert chosen.contract.strike == D("510")

    def test_a_candidate_with_no_delta_is_skipped_even_when_it_is_the_only_one(
        self,
    ) -> None:
        """Missing is not zero, and it does not win by being the last one
        standing. There is no strike to select, and that is the answer."""
        assert (
            select_short_strike(
                put_chain((("490", None),)),
                target_delta=D("0.16"),
                right=OptionRight.PUT,
            )
            is None
        )

    def test_a_missing_delta_does_not_compete_as_zero(self) -> None:
        """If ``None`` were read as ``0``, the 490 would be 0.16 from the target
        and would beat the 0.90 that is 0.74 away -- selecting a contract on the
        strength of data that never arrived."""
        chain = put_chain((("490", None), ("470", "-0.90")))
        chosen = select_short_strike(chain, target_delta=D("0.16"), right=OptionRight.PUT)
        assert chosen is not None
        assert chosen.contract.strike == D("470")

    def test_put_deltas_are_compared_as_magnitudes_not_signed(self) -> None:
        """Signed, ``-0.30`` is 0.60 from a target of 0.30 while ``-0.05`` is
        0.35 -- so a signed comparison picks the 5-delta and calls it a
        30-delta short. The whole put chain ranks backwards, silently."""
        chain = put_chain((("495", "-0.30"), ("480", "-0.05")))
        chosen = select_short_strike(chain, target_delta=D("0.30"), right=OptionRight.PUT)
        assert chosen is not None
        assert chosen.contract.strike == D("495")

    def test_call_deltas_are_positive_and_select_the_same_way(self) -> None:
        chosen = select_short_strike(
            STANDARD_CALLS, target_delta=D("0.16"), right=OptionRight.CALL
        )
        assert chosen is not None
        assert chosen.contract.strike == D("510")
        assert chosen.delta == D("0.16")

    def test_the_other_right_is_not_considered(self) -> None:
        """A chain carrying both sides must not let a call answer a put query."""
        chosen = select_short_strike(
            STANDARD_PUTS + STANDARD_CALLS,
            target_delta=D("0.16"),
            right=OptionRight.PUT,
        )
        assert chosen is not None
        assert chosen.contract.right == "P"
        assert chosen.contract.strike == D("490")

    def test_a_put_reporting_a_positive_delta_is_not_selectable(self) -> None:
        """A value that disagrees with the sign convention is not an unusual
        strike; it is a number this function cannot interpret."""
        chain = put_chain((("490", "0.16"),))
        assert (
            select_short_strike(chain, target_delta=D("0.16"), right=OptionRight.PUT)
            is None
        )

    def test_a_call_reporting_a_negative_delta_is_not_selectable(self) -> None:
        chain = call_chain((("510", "-0.16"),))
        assert (
            select_short_strike(chain, target_delta=D("0.16"), right=OptionRight.CALL)
            is None
        )

    def test_a_delta_of_exactly_zero_is_a_real_value_and_is_selectable(self) -> None:
        """The reason ``None`` and ``0`` must stay distinct: a far out-of-the-
        money contract really can round to zero delta."""
        chosen = select_short_strike(
            put_chain((("400", "0"),)), target_delta=D("0.16"), right=OptionRight.PUT
        )
        assert chosen is not None
        assert chosen.contract.strike == D("400")

    def test_an_empty_universe_returns_none(self) -> None:
        assert (
            select_short_strike((), target_delta=D("0.16"), right=OptionRight.PUT)
            is None
        )

    def test_selection_does_not_depend_on_input_order(self) -> None:
        """Two runs against the same chain must select the same contract, or a
        journal record cannot be re-derived from it."""
        forward = select_short_strike(
            STANDARD_PUTS, target_delta=D("0.17"), right=OptionRight.PUT
        )
        backward = select_short_strike(
            tuple(reversed(STANDARD_PUTS)), target_delta=D("0.17"), right=OptionRight.PUT
        )
        assert forward is not None and backward is not None
        assert forward.contract.con_id == backward.contract.con_id


class TestShortStrikeArgumentRefusals:
    @pytest.mark.parametrize("target", ["0", "1", "-0.16", "1.5"])
    def test_a_target_outside_the_open_unit_interval_is_refused(
        self, target: str
    ) -> None:
        with pytest.raises(InvalidStrategyError, match="between 0 and 1"):
            select_short_strike(
                STANDARD_PUTS, target_delta=D(target), right=OptionRight.PUT
            )

    def test_a_float_target_is_refused(self) -> None:
        """0.16 as a float is not 0.16, and strike selection is not the place to
        introduce binary rounding."""
        with pytest.raises(InvalidStrategyError, match="Decimal"):
            select_short_strike(
                STANDARD_PUTS,
                target_delta=0.16,  # type: ignore[arg-type]
                right=OptionRight.PUT,
            )

    def test_a_nan_target_is_refused(self) -> None:
        """Every comparison against NaN is False, so an unguarded range check
        would let it through and it would then never match anything."""
        with pytest.raises(InvalidStrategyError, match="finite"):
            select_short_strike(
                STANDARD_PUTS, target_delta=D("NaN"), right=OptionRight.PUT
            )

    def test_a_bare_string_right_is_refused(self) -> None:
        with pytest.raises(InvalidStrategyError, match="OptionRight"):
            select_short_strike(
                STANDARD_PUTS,
                target_delta=D("0.16"),
                right="P",  # type: ignore[arg-type]
            )


# ===========================================================================
# The vertical: short strike plus its protection
# ===========================================================================


class TestSelectVertical:
    def test_a_put_spread_puts_its_protection_below_the_short(self) -> None:
        selection = select_vertical(
            STANDARD_PUTS,
            target_delta=D("0.16"),
            right=OptionRight.PUT,
            target_width=D("5"),
        )
        assert selection is not None
        assert selection.short.strike == D("490")
        assert selection.long.strike == D("485")
        assert selection.long.strike < selection.short.strike
        assert selection.width == D("5")
        assert selection.short_delta == D("-0.16")

    def test_a_call_spread_puts_its_protection_above_the_short(self) -> None:
        selection = select_vertical(
            STANDARD_CALLS,
            target_delta=D("0.16"),
            right=OptionRight.CALL,
            target_width=D("5"),
        )
        assert selection is not None
        assert selection.short.strike == D("510")
        assert selection.long.strike == D("515")
        assert selection.long.strike > selection.short.strike

    def test_the_protective_leg_is_chosen_by_width_not_by_delta(self) -> None:
        """A 10-wide target takes the 480 rather than the 485, even though the
        485's delta is nearer anything."""
        selection = select_vertical(
            STANDARD_PUTS,
            target_delta=D("0.16"),
            right=OptionRight.PUT,
            target_width=D("10"),
        )
        assert selection is not None
        assert selection.long.strike == D("480")
        assert selection.width == D("10")

    def test_the_nearest_listed_width_is_used_when_the_target_is_not_listed(
        self,
    ) -> None:
        selection = select_vertical(
            STANDARD_PUTS,
            target_delta=D("0.16"),
            right=OptionRight.PUT,
            target_width=D("6"),
        )
        assert selection is not None
        assert selection.width == D("5")

    def test_a_width_tie_breaks_toward_the_narrower_spread(self) -> None:
        """2.5 and 7.5 are equally distant from a 5-wide target. The narrower
        one caps the loss lower, which is the conservative half of the tie."""
        chain = put_chain(
            (("490", "-0.16"), ("487.5", "-0.13"), ("482.5", "-0.07"))
        )
        selection = select_vertical(
            chain,
            target_delta=D("0.16"),
            right=OptionRight.PUT,
            target_width=D("5"),
        )
        assert selection is not None
        assert selection.long.strike == D("487.5")
        assert selection.width == D("2.5")

    def test_a_protective_leg_with_no_delta_is_still_usable(self) -> None:
        """A chain can quote greeks near the money and nothing for the wing.
        Refusing to protect the short then leaves not trading -- or worse,
        trading it naked -- as the only alternatives."""
        chain = put_chain((("490", "-0.16"), ("485", None)))
        selection = select_vertical(
            chain,
            target_delta=D("0.16"),
            right=OptionRight.PUT,
            target_width=D("5"),
        )
        assert selection is not None
        assert selection.long.strike == D("485")

    def test_a_chain_with_nothing_further_out_returns_none(self) -> None:
        """The short is the lowest listed put, so there is nothing to buy as
        protection and no defined-risk structure exists here."""
        chain = put_chain((("495", "-0.30"), ("490", "-0.16")))
        assert (
            select_vertical(
                chain,
                target_delta=D("0.16"),
                right=OptionRight.PUT,
                target_width=D("5"),
            )
            is None
        )

    def test_a_chain_with_no_deltas_returns_none(self) -> None:
        chain = put_chain((("495", None), ("490", None), ("485", None)))
        assert (
            select_vertical(
                chain,
                target_delta=D("0.16"),
                right=OptionRight.PUT,
                target_width=D("5"),
            )
            is None
        )

    def test_the_protection_is_never_the_short_contract_itself(self) -> None:
        """A duplicated con_id breaks the domain's distinct-contract invariant
        much later and much less clearly."""
        selection = select_vertical(
            STANDARD_PUTS,
            target_delta=D("0.16"),
            right=OptionRight.PUT,
            target_width=D("5"),
        )
        assert selection is not None
        assert selection.short.con_id != selection.long.con_id

    def test_a_wing_from_another_expiration_is_not_used(self) -> None:
        """Calendars and diagonals are not supported structures, and a domain
        refusal several layers later is a worse diagnosis than no selection."""
        chain = (
            candidate("490", "-0.16"),
            candidate("485", "-0.09", expiration=OTHER_EXPIRY),
        )
        assert (
            select_vertical(
                chain,
                target_delta=D("0.16"),
                right=OptionRight.PUT,
                target_width=D("5"),
            )
            is None
        )

    def test_a_wing_with_a_different_multiplier_is_not_used(self) -> None:
        """A mixed-multiplier structure makes the max-loss arithmetic wrong."""
        chain = (
            candidate("490", "-0.16"),
            candidate("485", "-0.09", multiplier=10),
        )
        assert (
            select_vertical(
                chain,
                target_delta=D("0.16"),
                right=OptionRight.PUT,
                target_width=D("5"),
            )
            is None
        )

    def test_a_wing_on_another_underlying_is_not_used(self) -> None:
        chain = (
            candidate("490", "-0.16"),
            candidate("485", "-0.09", symbol="QQQ"),
        )
        assert (
            select_vertical(
                chain,
                target_delta=D("0.16"),
                right=OptionRight.PUT,
                target_width=D("5"),
            )
            is None
        )

    @pytest.mark.parametrize("width", ["0", "-5"])
    def test_a_non_positive_target_width_is_refused(self, width: str) -> None:
        with pytest.raises(InvalidStrategyError, match="target_width"):
            select_vertical(
                STANDARD_PUTS,
                target_delta=D("0.16"),
                right=OptionRight.PUT,
                target_width=D(width),
            )

    def test_a_float_target_width_is_refused(self) -> None:
        with pytest.raises(InvalidStrategyError, match="Decimal"):
            select_vertical(
                STANDARD_PUTS,
                target_delta=D("0.16"),
                right=OptionRight.PUT,
                target_width=5.0,  # type: ignore[arg-type]
            )


class TestStrikeSelectionInvariants:
    """The pair validates itself, so a selection that could not become a valid
    intent fails where it was made rather than several layers later."""

    def test_a_reversed_put_wing_is_refused(self) -> None:
        with pytest.raises(InvalidStrategyError, match="must be below"):
            StrikeSelection(
                short=contract("485"),
                long=contract("490"),
                short_delta=D("-0.09"),
                width=D("5"),
            )

    def test_a_reversed_call_wing_is_refused(self) -> None:
        with pytest.raises(InvalidStrategyError, match="must be above"):
            StrikeSelection(
                short=contract("515", right="C"),
                long=contract("510", right="C"),
                short_delta=D("0.09"),
                width=D("5"),
            )

    def test_the_same_contract_twice_is_refused(self) -> None:
        one = contract("490")
        with pytest.raises(InvalidStrategyError, match="same contract"):
            StrikeSelection(short=one, long=one, short_delta=D("-0.16"), width=D("0"))

    def test_mixed_rights_are_refused(self) -> None:
        with pytest.raises(InvalidStrategyError, match="one right"):
            StrikeSelection(
                short=contract("490"),
                long=contract("485", right="C"),
                short_delta=D("-0.16"),
                width=D("5"),
            )

    def test_mixed_expirations_are_refused(self) -> None:
        with pytest.raises(InvalidStrategyError, match="one expiration"):
            StrikeSelection(
                short=contract("490"),
                long=contract("485", expiration=OTHER_EXPIRY),
                short_delta=D("-0.16"),
                width=D("5"),
            )

    def test_mixed_multipliers_are_refused(self) -> None:
        with pytest.raises(InvalidStrategyError, match="one multiplier"):
            StrikeSelection(
                short=contract("490"),
                long=contract("485", multiplier=10),
                short_delta=D("-0.16"),
                width=D("5"),
            )

    def test_mixed_underlyings_are_refused(self) -> None:
        with pytest.raises(InvalidStrategyError, match="one underlying"):
            StrikeSelection(
                short=contract("490"),
                long=contract("485", symbol="QQQ"),
                short_delta=D("-0.16"),
                width=D("5"),
            )

    def test_a_width_that_disagrees_with_the_strikes_is_refused(self) -> None:
        """The stored width is what a report shows; it must not be able to
        disagree with the contracts it claims to describe."""
        with pytest.raises(InvalidStrategyError, match="does not match"):
            StrikeSelection(
                short=contract("490"),
                long=contract("485"),
                short_delta=D("-0.16"),
                width=D("10"),
            )

    def test_a_float_width_is_refused(self) -> None:
        with pytest.raises(InvalidStrategyError, match="width"):
            StrikeSelection(
                short=contract("490"),
                long=contract("485"),
                short_delta=D("-0.16"),
                width=5.0,  # type: ignore[arg-type]
            )

    def test_the_strategy_type_follows_the_legs(self) -> None:
        selection = StrikeSelection(
            short=contract("490"),
            long=contract("485"),
            short_delta=D("-0.16"),
            width=D("5"),
        )
        assert selection.strategy_type is StrategyType.PUT_CREDIT_SPREAD
        assert selection.multiplier == 100
        assert "490" in selection.describe()


# ===========================================================================
# Sizing
# ===========================================================================


class TestSizePosition:
    def test_an_exact_division_gives_the_whole_quotient(self) -> None:
        assert size_position(
            maximum_loss_per_contract=D("500"), risk_budget=D("1000")
        ) == 2

    def test_a_remainder_floors_down(self) -> None:
        """3.5 contracts is 3. Rounding to 4 spends 1400 of a 1000 budget."""
        assert size_position(
            maximum_loss_per_contract=D("350"), risk_budget=D("1250")
        ) == 3

    def test_exactly_one_contract_of_budget_gives_one(self) -> None:
        assert size_position(
            maximum_loss_per_contract=D("500"), risk_budget=D("500")
        ) == 1

    def test_a_budget_below_one_contract_gives_zero_not_one(self) -> None:
        """Zero means do not trade. Rounding up to one 'so the scan has
        something to show' is the failure this function exists to prevent."""
        assert size_position(
            maximum_loss_per_contract=D("500"), risk_budget=D("499.99")
        ) == 0

    def test_a_far_too_small_budget_gives_zero(self) -> None:
        assert size_position(
            maximum_loss_per_contract=D("350"), risk_budget=D("10")
        ) == 0

    def test_a_zero_budget_gives_zero(self) -> None:
        assert size_position(
            maximum_loss_per_contract=D("350"), risk_budget=D("0")
        ) == 0

    def test_a_negative_budget_gives_zero_rather_than_a_negative_quantity(
        self,
    ) -> None:
        """An account already over its budget must size to nothing, not to a
        quantity that would read as a short position somewhere downstream."""
        assert size_position(
            maximum_loss_per_contract=D("350"), risk_budget=D("-1000")
        ) == 0

    @pytest.mark.parametrize("maximum_loss", ["0", "-1", "-350"])
    def test_a_non_positive_maximum_loss_raises(self, maximum_loss: str) -> None:
        """There is no quantity to return: the division is by zero or produces a
        negative contract count, and both are a sizing bug rather than a market
        condition the caller could respond to."""
        with pytest.raises(ValueError, match="must be positive"):
            size_position(
                maximum_loss_per_contract=D(maximum_loss), risk_budget=D("1000")
            )

    def test_a_nan_maximum_loss_raises_rather_than_passing_the_sign_check(
        self,
    ) -> None:
        """NaN fails every comparison, so an unguarded ``<= 0`` lets it through
        and the division raises InvalidOperation somewhere further out."""
        with pytest.raises(ValueError, match="finite"):
            size_position(maximum_loss_per_contract=D("NaN"), risk_budget=D("1000"))

    def test_a_nan_budget_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            size_position(maximum_loss_per_contract=D("350"), risk_budget=D("NaN"))

    def test_an_infinite_budget_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            size_position(
                maximum_loss_per_contract=D("350"), risk_budget=D("Infinity")
            )

    def test_a_float_maximum_loss_raises(self) -> None:
        with pytest.raises(ValueError, match="Decimal"):
            size_position(
                maximum_loss_per_contract=350.0,  # type: ignore[arg-type]
                risk_budget=D("1000"),
            )

    def test_a_float_budget_raises(self) -> None:
        with pytest.raises(ValueError, match="Decimal"):
            size_position(
                maximum_loss_per_contract=D("350"),
                risk_budget=1000.0,  # type: ignore[arg-type]
            )

    def test_the_arithmetic_is_decimal_exact(self) -> None:
        """In binary floating point 0.3 / 0.1 is 2.9999999999999996, so a float
        implementation floors to 2 and sizes the position one contract short."""
        assert size_position(
            maximum_loss_per_contract=D("0.1"), risk_budget=D("0.3")
        ) == 3

    def test_a_quotient_that_rounds_up_at_context_precision_still_floors(self) -> None:
        """True Decimal division is evaluated at 28 significant digits, so this
        quotient -- 2.9999999999999999999999999998 -- comes back as exactly 3
        and a floor applied afterwards sizes the position a contract too large.
        Exact integer division sees the 2 that is really there."""
        assert (
            size_position(
                maximum_loss_per_contract=D("0.6666666666666666666666666667"),
                risk_budget=D("2"),
            )
            == 2
        )

    def test_a_repeating_quotient_still_floors(self) -> None:
        assert size_position(
            maximum_loss_per_contract=D("333.33"), risk_budget=D("1000")
        ) == 3

    def test_the_result_is_an_int_not_a_decimal(self) -> None:
        """The domain refuses a quantity that is not an int, and a Decimal
        quantity would fail there rather than here."""
        quantity = size_position(
            maximum_loss_per_contract=D("500"), risk_budget=D("1000")
        )
        assert isinstance(quantity, int)
        assert not isinstance(quantity, bool)


# ===========================================================================
# Selection -> intent
# ===========================================================================


def standard_selection(right: OptionRight = OptionRight.PUT) -> StrikeSelection:
    chain = STANDARD_PUTS if right is OptionRight.PUT else STANDARD_CALLS
    selection = select_vertical(
        chain, target_delta=D("0.16"), right=right, target_width=D("5")
    )
    assert selection is not None
    return selection


def built(
    selection: StrikeSelection,
    *,
    credit: str = "1.50",
    policy: RiskPolicy | None = None,
    strategy_id: UUID | None = None,
) -> OptionStrategyIntent | None:
    return build_vertical(
        selection,
        credit=D(credit),
        policy=policy or RiskPolicy(),
        configuration_version="test",
        created_at=NOW,
        strategy_id=strategy_id,
    )


class TestBuildVertical:
    def test_a_selection_becomes_a_validated_opening_credit_intent(self) -> None:
        intent = built(standard_selection())
        assert intent is not None
        assert intent.strategy_type is StrategyType.PUT_CREDIT_SPREAD
        assert intent.strategy_action is StrategyAction.OPEN
        assert intent.price_effect is PriceEffect.CREDIT
        assert intent.underlying == "SPY"
        assert intent.expiration == EXPIRY
        assert intent.limit_price == D("1.50")

    def test_the_short_and_long_legs_carry_the_selected_contracts(self) -> None:
        selection = standard_selection()
        intent = built(selection)
        assert intent is not None
        shorts = [leg for leg in intent.legs if leg.action is OrderAction.SELL]
        longs = [leg for leg in intent.legs if leg.action is OrderAction.BUY]
        assert [leg.con_id for leg in shorts] == [selection.short.con_id]
        assert [leg.con_id for leg in longs] == [selection.long.con_id]
        assert shorts[0].multiplier == selection.short.multiplier
        assert shorts[0].trading_class == selection.short.trading_class

    def test_the_call_side_builds_a_call_credit_spread(self) -> None:
        intent = built(standard_selection(OptionRight.CALL))
        assert intent is not None
        assert intent.strategy_type is StrategyType.CALL_CREDIT_SPREAD
        assert all(leg.right is OptionRight.CALL for leg in intent.legs)

    def test_the_maximum_loss_is_computed_from_the_legs(self) -> None:
        """Never passed in: the domain recomputes and compares it, so a figure
        derived any other way would only be caught there."""
        intent = built(standard_selection())
        assert intent is not None
        assert intent.maximum_loss_per_contract == compute_maximum_loss_per_contract(
            strategy_type=intent.strategy_type,
            legs=intent.legs,
            credit=intent.limit_price,
            multiplier=intent.multiplier,
        )
        assert intent.maximum_loss_per_contract == D("350.00")

    def test_the_quantity_comes_from_the_risk_budget(self) -> None:
        """The replacement for the hardcoded quantity=1. A 350-per-contract
        structure against a 1500 budget is four contracts, not one."""
        intent = built(
            standard_selection(),
            policy=RiskPolicy(risk_budget_per_position=D("1500")),
        )
        assert intent is not None
        assert intent.quantity == 4
        assert intent.total_maximum_loss == D("1400.00")

    def test_the_default_budget_sizes_a_five_wide_to_one_contract(self) -> None:
        intent = built(standard_selection())
        assert intent is not None
        assert intent.quantity == 1

    def test_a_budget_below_one_contract_returns_none_rather_than_a_quantity(
        self,
    ) -> None:
        """Zero contracts is a refusal, and OptionStrategyIntent refuses a
        quantity of zero outright -- so there is no object to hand back."""
        assert (
            built(
                standard_selection(),
                policy=RiskPolicy(risk_budget_per_position=D("100")),
            )
            is None
        )

    def test_a_credit_at_or_above_the_width_raises_rather_than_returning_none(
        self,
    ) -> None:
        """A structural error is not a market condition. Returning None would
        let a caller's 'if candidate is None: continue' swallow it."""
        with pytest.raises(InvalidStrategyError, match="riskless|not less than"):
            built(standard_selection(), credit="5.00")

    def test_a_non_positive_credit_raises(self) -> None:
        with pytest.raises(InvalidStrategyError, match="credit must be positive"):
            built(standard_selection(), credit="0")

    def test_the_strategy_id_and_timestamp_are_the_callers(self) -> None:
        """The same selection and credit must always produce the same record."""
        identifier = uuid4()
        intent = built(standard_selection(), strategy_id=identifier)
        assert intent is not None
        assert intent.strategy_id == identifier
        assert intent.created_at == NOW
        assert intent.configuration_version == "test"

    def test_an_omitted_strategy_id_is_generated(self) -> None:
        first = built(standard_selection())
        second = built(standard_selection())
        assert first is not None and second is not None
        assert first.strategy_id != second.strategy_id

    def test_an_opening_intent_never_references_a_close(self) -> None:
        intent = built(standard_selection())
        assert intent is not None
        assert intent.closes_strategy_id is None


# ===========================================================================
# Every selection the selector produces must be buildable
# ===========================================================================


WIDE_PUTS = put_chain(
    tuple(
        (str(strike), f"-{(strike - 440) / 1000:.4f}")
        for strike in range(450, 531, 5)
    )
)

WIDE_CALLS = call_chain(
    tuple(
        (str(strike), f"{(530 - strike) / 1000:.4f}")
        for strike in range(450, 531, 5)
    )
)


class TestEverySelectionIsBuildable:
    """The load-bearing property. A pair the selector is willing to return but
    the domain refuses is a bug that would otherwise surface as an exception in
    a scan loop, several layers from the code that chose the strikes."""

    @pytest.mark.parametrize("target_delta", ["0.05", "0.16", "0.30", "0.45"])
    @pytest.mark.parametrize("target_width", ["2", "5", "10", "25"])
    @pytest.mark.parametrize("right", [OptionRight.PUT, OptionRight.CALL])
    def test_selection_then_build_never_refuses(
        self, target_delta: str, target_width: str, right: OptionRight
    ) -> None:
        chain = WIDE_PUTS if right is OptionRight.PUT else WIDE_CALLS
        selection = select_vertical(
            chain,
            target_delta=D(target_delta),
            right=right,
            target_width=D(target_width),
        )
        assert selection is not None, "the wide chain must be able to supply a pair"

        # A third of the width, which is always a valid credit for a defined-risk
        # spread and never reaches the riskless-combination refusal.
        credit = (selection.width / D("3")).quantize(D("0.01"))
        intent = build_vertical(
            selection,
            credit=credit,
            policy=DEEP_POCKETS,
            configuration_version="test",
            created_at=NOW,
        )
        assert intent is not None
        assert intent.quantity >= 1
        assert intent.strategy_type is strategy_type_for(right)
        assert intent.total_maximum_loss > 0

    @pytest.mark.parametrize("target_delta", ["0.05", "0.16", "0.30"])
    def test_the_selected_short_is_the_nearest_available_magnitude(
        self, target_delta: str
    ) -> None:
        """States the selection rule independently of the implementation: no
        other candidate in the chain is strictly nearer the target."""
        selection = select_vertical(
            WIDE_PUTS,
            target_delta=D(target_delta),
            right=OptionRight.PUT,
            target_width=D("5"),
        )
        assert selection is not None
        chosen = abs(abs(selection.short_delta) - D(target_delta))
        for other in WIDE_PUTS:
            assert other.delta is not None
            assert abs(abs(other.delta) - D(target_delta)) >= chosen
