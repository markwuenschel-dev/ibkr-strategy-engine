"""The liquidity gate: exit-ability is a hard requirement, unmeasured fails.

Fixtures build a deliberately *liquid* baseline and then break exactly one
property per test, so a failure names the sub-check that regressed rather
than "liquidity is broken somewhere".
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import uuid4

from engine.options.domain import (
    OptionLegIntent,
    OptionRight,
    OptionStrategyIntent,
    OrderAction,
    PriceEffect,
    StrategyAction,
    StrategyType,
)
from engine.options.liquidity import (
    CHECK_LIQUIDITY,
    LiquidityRefusalReason,
    check_liquidity,
)
from engine.options.marketdata import (
    MarketDataProvenance,
    MarketDataType,
    OptionQuote,
    UnderlyingQuote,
)
from engine.options.policy import RiskPolicy
from engine.options.ports import StrategyQuoteSnapshot

D = Decimal
NOW = dt.datetime(2026, 8, 1, 14, 0, tzinfo=dt.timezone.utc)
POLICY = RiskPolicy()

SHORT_ID = 450
LONG_ID = 445


def _provenance() -> MarketDataProvenance:
    from uuid import uuid4 as new

    return MarketDataProvenance(
        requested_type=int(MarketDataType.LIVE),
        subscription_generation=new(),
        subscribed_at=NOW,
        reported_type=int(MarketDataType.LIVE),
        callback_received=True,
        last_provider_event_at=NOW,
        last_local_receive_at=NOW,
    )


def leg_quote(
    con_id: int,
    *,
    bid: str | None = None,
    ask: str | None = None,
    open_interest: int | None = 1000,
    volume: int | None = 500,
) -> OptionQuote:
    return OptionQuote(
        con_id=con_id,
        provenance=_provenance(),
        bid=D(bid) if bid is not None else None,
        ask=D(ask) if ask is not None else None,
        open_interest=open_interest,
        volume=volume,
    )


def snapshot(
    short: OptionQuote, long: OptionQuote, *, filler_count: int = 10
) -> StrategyQuoteSnapshot:
    """The two structure legs plus enough quoted filler strikes to clear the
    chain-density floor -- density is a property of the window, not the legs."""
    fillers = tuple(
        leg_quote(9000 + i, bid="1.00", ask="1.05") for i in range(filler_count)
    )
    legs = (short, long, *fillers)
    return StrategyQuoteSnapshot(
        underlying=UnderlyingQuote(
            symbol="SPY", provenance=_provenance(), bid=D("500"), ask=D("500.1")
        ),
        legs=legs,
        generations=(
            ("underlying", _provenance().subscription_generation),
            *((str(q.con_id), q.provenance.subscription_generation) for q in legs),
        ),
    )


def intent(*, multiplier: int = 100) -> OptionStrategyIntent:
    legs = (
        OptionLegIntent(
            con_id=SHORT_ID,
            symbol="SPY",
            expiration=dt.date(2026, 9, 18),
            strike=D("450"),
            right=OptionRight.PUT,
            action=OrderAction.SELL,
            ratio=1,
            multiplier=multiplier,
            exchange="SMART",
        ),
        OptionLegIntent(
            con_id=LONG_ID,
            symbol="SPY",
            expiration=dt.date(2026, 9, 18),
            strike=D("445"),
            right=OptionRight.PUT,
            action=OrderAction.BUY,
            ratio=1,
            multiplier=multiplier,
            exchange="SMART",
        ),
    )
    return OptionStrategyIntent(
        strategy_id=uuid4(),
        strategy_type=StrategyType.PUT_CREDIT_SPREAD,
        strategy_action=StrategyAction.OPEN,
        underlying="SPY",
        quantity=1,
        legs=legs,
        expiration=dt.date(2026, 9, 18),
        limit_price=D("1.50"),
        price_effect=PriceEffect.CREDIT,
        # Width 5.00 minus credit 1.50, times the multiplier -- the domain
        # refuses an intent whose stated loss disagrees with its legs, so a
        # nonstandard-multiplier intent must still carry consistent arithmetic.
        maximum_loss_per_contract=D("3.5") * multiplier,
        configuration_version="test",
        created_at=NOW,
    )


def liquid_pair() -> tuple[OptionQuote, OptionQuote]:
    return (
        leg_quote(SHORT_ID, bid="15.00", ask="15.10"),
        leg_quote(LONG_ID, bid="13.55", ask="13.65"),
    )


class TestTheLiquidBaselinePasses:
    def test_tight_deep_two_sided_approves(self) -> None:
        result = check_liquidity(
            intent(), quotes=snapshot(*liquid_pair()), policy=POLICY
        )
        assert result.approved, result.detail
        assert result.check == CHECK_LIQUIDITY


class TestOneSided:
    def test_a_leg_missing_its_bid_refuses(self) -> None:
        short, long = liquid_pair()
        short = leg_quote(SHORT_ID, bid=None, ask="15.10")
        result = check_liquidity(intent(), quotes=snapshot(short, long), policy=POLICY)
        assert not result.approved
        assert result.reason is LiquidityRefusalReason.ONE_SIDED

    def test_an_unquoted_leg_refuses(self) -> None:
        _, long = liquid_pair()
        stranger = leg_quote(999999, bid="1.0", ask="1.1")
        result = check_liquidity(
            intent(), quotes=snapshot(stranger, long), policy=POLICY
        )
        assert not result.approved
        assert result.reason is LiquidityRefusalReason.ONE_SIDED

    def test_no_snapshot_is_unmeasurable(self) -> None:
        result = check_liquidity(intent(), quotes=None, policy=POLICY)
        assert not result.approved
        assert result.reason is LiquidityRefusalReason.UNMEASURABLE


class TestSpreadWidth:
    def test_a_dollar_wide_leg_refuses(self) -> None:
        short, long = liquid_pair()
        short = leg_quote(SHORT_ID, bid="15.00", ask="15.60")  # 0.60 > 0.50
        result = check_liquidity(intent(), quotes=snapshot(short, long), policy=POLICY)
        assert not result.approved
        assert result.reason is LiquidityRefusalReason.SPREAD_WIDE

    def test_a_fractionally_wide_cheap_leg_refuses(self) -> None:
        """0.30 wide on a 1.00 mid is 30% -- under the dollar cap, over the
        fractional one. Both caps exist because either alone misses a case."""
        short, long = liquid_pair()
        long = leg_quote(LONG_ID, bid="0.85", ask="1.15")
        result = check_liquidity(intent(), quotes=snapshot(short, long), policy=POLICY)
        assert not result.approved
        assert result.reason is LiquidityRefusalReason.SPREAD_WIDE

    def test_crossing_cost_over_a_quarter_of_the_credit_refuses(self) -> None:
        """Legs individually acceptable, structure uncrossable: mid credit
        0.50, natural 0.30 -- crossing eats 40% of the edge. (Exactly 25%
        passes; the cap is a ceiling, not a boundary refusal.)"""
        short = leg_quote(SHORT_ID, bid="15.00", ask="15.20")
        long = leg_quote(LONG_ID, bid="14.50", ask="14.70")
        result = check_liquidity(intent(), quotes=snapshot(short, long), policy=POLICY)
        assert not result.approved
        assert result.reason is LiquidityRefusalReason.SPREAD_WIDE
        assert "crossing" in result.detail


class TestDepth:
    def test_low_open_interest_refuses(self) -> None:
        short, long = liquid_pair()
        short = leg_quote(SHORT_ID, bid="15.00", ask="15.10", open_interest=12)
        result = check_liquidity(intent(), quotes=snapshot(short, long), policy=POLICY)
        assert not result.approved
        assert result.reason is LiquidityRefusalReason.THIN

    def test_unreported_open_interest_refuses_as_thin(self) -> None:
        """Unmeasured counts as insufficient -- absence of a depth figure is
        not evidence of depth. This is the case that bites live until the
        tick-101 fix is verified, and it is meant to."""
        short, long = liquid_pair()
        short = leg_quote(SHORT_ID, bid="15.00", ask="15.10", open_interest=None)
        result = check_liquidity(intent(), quotes=snapshot(short, long), policy=POLICY)
        assert not result.approved
        assert result.reason is LiquidityRefusalReason.THIN
        assert "unreported" in result.detail

    def test_low_volume_refuses(self) -> None:
        short, long = liquid_pair()
        long = leg_quote(LONG_ID, bid="13.55", ask="13.65", volume=3)
        result = check_liquidity(intent(), quotes=snapshot(short, long), policy=POLICY)
        assert not result.approved
        assert result.reason is LiquidityRefusalReason.THIN


class TestStructure:
    def test_nonstandard_multiplier_refuses_first(self) -> None:
        """A 10-multiplier mini or adjusted contract refuses even on a
        perfect book -- and outranks every other code in the verdict."""
        short, long = liquid_pair()
        short = leg_quote(SHORT_ID, bid=None, ask=None)  # also one-sided
        result = check_liquidity(
            intent(multiplier=10), quotes=snapshot(short, long), policy=POLICY
        )
        assert not result.approved
        assert result.reason is LiquidityRefusalReason.NONSTANDARD

    def test_sparse_chain_refuses(self) -> None:
        result = check_liquidity(
            intent(),
            quotes=snapshot(*liquid_pair(), filler_count=2),
            policy=POLICY,
        )
        assert not result.approved
        assert result.reason is LiquidityRefusalReason.SPARSE_CHAIN

    def test_every_problem_is_named_in_the_detail(self) -> None:
        short = leg_quote(SHORT_ID, bid="15.00", ask="15.60", open_interest=1)
        long = leg_quote(LONG_ID, bid=None, ask="13.65", volume=None)
        result = check_liquidity(
            intent(), quotes=snapshot(short, long, filler_count=0), policy=POLICY
        )
        assert not result.approved
        for fragment in ("spread", "open interest", "two-sided", "strikes quoted"):
            assert fragment in result.detail, (fragment, result.detail)
