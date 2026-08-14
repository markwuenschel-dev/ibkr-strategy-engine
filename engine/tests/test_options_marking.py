"""Marking an open position: the price, the direction, and the four states.

Every number in this file comes from a **real** position the engine held on the
paper account and could not mark::

    SPY 2026-09-18 721/720 put credit spread, quantity 1, entry credit 0.18
    orderId 1663  permId 1151642181

and from its book as observed at 15:57 ET::

    short 721.0P  bid 9.09 / ask 9.12      long 720.0P  bid 8.88 / ask 8.91

    closing midpoint debit 0.210    closing natural debit 0.240
    gross P&L -3.00 at midpoint     -6.00 at natural
    the 50% profit target needs a debit <= 0.090

Two tests here carry more weight than the rest.

**The direction test** (:class:`TestTheDirectionOfTheClose`) swaps the bid and the
ask in the closing calculation and asserts the result differs *and* flatters. On
this exact book the inverted pairing yields 0.18 -- precisely the entry credit --
so a position that is really down $6.00 reports as dead flat. Nothing about that
number looks wrong, it cannot be traded, and it is the same error that left a
real order resting for 160 minutes. The pairing is pinned here so it cannot be
quietly inverted again.

**The commission tests** assert that a missing commission produces
``COMMISSION_INCOMPLETE`` *with gross still reported*, and never a net computed
against an assumed zero. The real fill came back with ``commission=None`` and
nothing was persisted, which is why net profit is unstateable today. Zero is the
flattering substitution, and the type system is what refuses it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
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
from engine.options.executions import (
    CommissionEvidence,
    ExecutionRecord,
    commission_evidence_for,
    executions_from_fills,
)
from engine.options.marketdata import (
    MarketDataProvenance,
    MarketDataType,
    OptionQuote,
    UnderlyingQuote,
)
from engine.options.marking import (
    CloseProposal,
    MarkReason,
    MarkState,
    PositionMarkReport,
    QuoteSide,
    closing_debit,
    closing_midpoint_debit,
    closing_natural_debit,
    confirmed_remaining_quantity,
    mark_open_positions,
    mark_position,
    propose_close,
)
from engine.options.policy import RiskPolicy
from engine.options.ports import StrategyQuoteSnapshot
from engine.options.positions import OpenPosition, PositionState, PositionStore

D = Decimal

# ---------------------------------------------------------------------------
# The real position, and the real book
# ---------------------------------------------------------------------------

#: 15:57 ET, when the book below was observed.
NOW = dt.datetime(2026, 7, 30, 19, 57, tzinfo=dt.timezone.utc)
EXPIRY = dt.date(2026, 9, 18)

SHORT_CON_ID = 721721
LONG_CON_ID = 720720
SHORT_STRIKE = D("721")
LONG_STRIKE = D("720")

ENTRY_CREDIT = D("0.18")
OPEN_ORDER_ID = 1663
OPEN_PERM_ID = 1151642181

#: The observed book, leg by leg. Written as bid/ask rather than as a mid and a
#: half-spread so the two sides can be swapped independently -- which is exactly
#: what the direction test does.
SHORT_BID, SHORT_ASK = D("9.09"), D("9.12")
LONG_BID, LONG_ASK = D("8.88"), D("8.91")

#: What the whole file is about.
EXPECTED_MIDPOINT_DEBIT = D("0.210")
EXPECTED_NATURAL_DEBIT = D("0.240")
EXPECTED_GROSS_AT_MIDPOINT = D("-3.00")
EXPECTED_GROSS_AT_NATURAL = D("-6.00")
EXPECTED_PROFIT_TARGET = D("0.090")

UNDERLYING_SPOT = D("715.00")


def spread_intent(
    *, strategy_id: UUID, quantity: int = 1, credit: Decimal = ENTRY_CREDIT
) -> OptionStrategyIntent:
    """The 721/720 put credit spread, exactly as it was opened."""
    legs = (
        OptionLegIntent(
            con_id=SHORT_CON_ID,
            symbol="SPY",
            expiration=EXPIRY,
            strike=SHORT_STRIKE,
            right=OptionRight.PUT,
            action=OrderAction.SELL,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
        OptionLegIntent(
            con_id=LONG_CON_ID,
            symbol="SPY",
            expiration=EXPIRY,
            strike=LONG_STRIKE,
            right=OptionRight.PUT,
            action=OrderAction.BUY,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
    )
    return OptionStrategyIntent(
        strategy_id=strategy_id,
        strategy_type=StrategyType.PUT_CREDIT_SPREAD,
        strategy_action=StrategyAction.OPEN,
        underlying="SPY",
        quantity=quantity,
        legs=legs,
        expiration=EXPIRY,
        limit_price=credit,
        price_effect=PriceEffect.CREDIT,
        # The real numbers: max gross profit 18.00, max gross loss 82.00.
        maximum_loss_per_contract=(SHORT_STRIKE - LONG_STRIKE) * 100 - credit * 100,
        configuration_version="test",
        created_at=NOW - dt.timedelta(days=1),
    )


def position(
    *,
    quantity: int = 1,
    filled_quantity: Decimal | None = None,
    close_filled_quantity: Decimal = D("0"),
    state: PositionState = PositionState.OPEN,
    credit: Decimal = ENTRY_CREDIT,
    commission: Decimal | None = None,
    commission_complete: bool = False,
    strategy_id: UUID | None = None,
) -> OpenPosition:
    identifier = strategy_id or uuid4()
    filled = Decimal(quantity) if filled_quantity is None else filled_quantity
    extras: dict[str, Any] = {}
    if state is PositionState.CLOSED:
        extras = {"closed_at": NOW, "closing_debit": D("0.09")}
    return OpenPosition(
        strategy_id=identifier,
        intent=spread_intent(
            strategy_id=identifier, quantity=quantity, credit=credit
        ),
        opened_at=NOW - dt.timedelta(days=1),
        state=state,
        buying_power_reserved=D("82"),
        filled_credit=credit,
        filled_quantity=filled,
        close_filled_quantity=close_filled_quantity,
        commission=commission,
        commission_complete=commission_complete,
        open_order_id=OPEN_ORDER_ID,
        open_perm_id=OPEN_PERM_ID,
        **extras,
    )


def provenance(
    generation: UUID,
    *,
    reported: MarketDataType = MarketDataType.LIVE,
    at: dt.datetime = NOW,
    callback: bool = True,
) -> MarketDataProvenance:
    return MarketDataProvenance(
        requested_type=int(MarketDataType.LIVE),
        subscription_generation=generation,
        subscribed_at=at,
        reported_type=int(reported),
        callback_received=callback,
        last_provider_event_at=at,
        last_local_receive_at=at,
    )


def leg_quote(
    *,
    con_id: int,
    bid: Decimal | None,
    ask: Decimal | None,
    reported: MarketDataType = MarketDataType.LIVE,
    at: dt.datetime = NOW,
) -> OptionQuote:
    return OptionQuote(
        con_id=con_id,
        provenance=provenance(uuid4(), reported=reported, at=at),
        bid=bid,
        ask=ask,
    )


def book(
    *,
    short_bid: Decimal | None = SHORT_BID,
    short_ask: Decimal | None = SHORT_ASK,
    long_bid: Decimal | None = LONG_BID,
    long_ask: Decimal | None = LONG_ASK,
    reported: MarketDataType = MarketDataType.LIVE,
    at: dt.datetime = NOW,
    drop_leg: int | None = None,
) -> StrategyQuoteSnapshot:
    """The observed book, with one knob per thing a test needs to break."""
    legs = [
        leg_quote(
            con_id=SHORT_CON_ID, bid=short_bid, ask=short_ask, reported=reported, at=at
        ),
        leg_quote(
            con_id=LONG_CON_ID, bid=long_bid, ask=long_ask, reported=reported, at=at
        ),
    ]
    if drop_leg is not None:
        legs = [leg for leg in legs if leg.con_id != drop_leg]
    underlying_generation = uuid4()
    return StrategyQuoteSnapshot(
        underlying=UnderlyingQuote(
            symbol="SPY",
            provenance=provenance(underlying_generation, reported=reported, at=at),
            bid=UNDERLYING_SPOT - D("0.01"),
            ask=UNDERLYING_SPOT + D("0.01"),
        ),
        legs=tuple(legs),
        generations=(
            ("underlying", underlying_generation),
            *(
                (str(q.con_id), q.provenance.subscription_generation)
                for q in legs
            ),
        ),
    )


def book_at_natural(
    natural: Decimal, *, leg_spread: Decimal = D("0.01"), at: dt.datetime = NOW
) -> StrategyQuoteSnapshot:
    """A coherent book whose closing natural is exactly ``natural``.

    Both sides of both legs move together. Editing one side of one leg in
    isolation -- the obvious way to write a boundary test -- produces a leg whose
    ask is below its bid, which is a crossed book and is refused for a reason
    that has nothing to do with the threshold being tested.
    """
    short_ask = D("9.00")
    long_bid = short_ask - natural
    return book(
        short_bid=short_ask - leg_spread,
        short_ask=short_ask,
        long_bid=long_bid,
        long_ask=long_bid + leg_spread,
        at=at,
    )


# ---------------------------------------------------------------------------
# Fake broker fills, for the commission side
# ---------------------------------------------------------------------------


class FakeContract:
    def __init__(self, con_id: int) -> None:
        self.conId = con_id


class FakeExecution:
    def __init__(
        self,
        *,
        exec_id: str,
        side: str,
        shares: float,
        price: float,
        order_ref: str | None,
        order_id: int = OPEN_ORDER_ID,
        perm_id: int = OPEN_PERM_ID,
    ) -> None:
        self.execId = exec_id
        self.side = side
        self.shares = shares
        self.price = price
        self.orderId = order_id
        self.permId = perm_id
        self.orderRef = order_ref
        self.time = NOW


class FakeCommissionReport:
    """IBKR's report. ``commission`` defaults to 0.0 and ``execId`` to '' --
    which is the whole reason an unpopulated one must not be believed."""

    def __init__(self, *, exec_id: str = "", commission: float = 0.0) -> None:
        self.execId = exec_id
        self.commission = commission
        self.currency = "USD"


class FakeFill:
    def __init__(self, *, contract: Any, execution: Any, report: Any) -> None:
        self.contract = contract
        self.execution = execution
        self.commissionReport = report
        self.time = NOW


def fill(
    *,
    con_id: int,
    exec_id: str,
    side: str,
    price: float,
    strategy_id: UUID,
    commission: float | None,
    shares: float = 1.0,
) -> FakeFill:
    """One leg fill. ``commission=None`` means the report never arrived."""
    report = (
        FakeCommissionReport(exec_id=exec_id, commission=commission)
        if commission is not None
        else FakeCommissionReport()
    )
    return FakeFill(
        contract=FakeContract(con_id),
        execution=FakeExecution(
            exec_id=exec_id,
            side=side,
            shares=shares,
            price=price,
            order_ref=str(strategy_id),
        ),
        report=report,
    )


def both_legs_filled(
    strategy_id: UUID, *, commission: float | None = -0.65, shares: float = 1.0
) -> list[FakeFill]:
    return [
        fill(
            con_id=SHORT_CON_ID,
            exec_id="0001.a",
            side="SLD",
            price=9.10,
            strategy_id=strategy_id,
            commission=commission,
            shares=shares,
        ),
        fill(
            con_id=LONG_CON_ID,
            exec_id="0001.b",
            side="BOT",
            price=8.92,
            strategy_id=strategy_id,
            commission=commission,
            shares=shares,
        ),
    ]


def evidence_for(
    pos: OpenPosition, fills: list[FakeFill] | None = None
) -> CommissionEvidence:
    records = executions_from_fills(
        both_legs_filled(pos.strategy_id) if fills is None else fills
    )
    return commission_evidence_for(
        strategy_id=pos.strategy_id,
        legs=pos.legs,
        filled_quantity=pos.filled_quantity,
        executions=records,
        order_id=pos.open_order_id,
        perm_id=pos.open_perm_id,
    )


def mark(
    pos: OpenPosition | None = None,
    snapshot: StrategyQuoteSnapshot | None = None,
    *,
    policy: RiskPolicy | None = None,
    commission: CommissionEvidence | None = None,
    now: dt.datetime = NOW,
    sentinel: bool = True,
    **kwargs: Any,
) -> PositionMarkReport:
    pos = position() if pos is None else pos
    if sentinel and snapshot is None and "quotes_error" not in kwargs:
        snapshot = book()
    return mark_position(
        pos,
        snapshot,
        policy=policy if policy is not None else RiskPolicy(),
        now=now,
        commission=commission,
        **kwargs,
    )


# ===========================================================================
# The closing price, and the direction that produces it
# ===========================================================================


class TestTheClosingPrice:
    def test_the_natural_is_the_short_ask_minus_the_long_bid(self) -> None:
        """The stated definition, asserted literally against the real book.

        9.12 - 8.88 = 0.240. Both sides are the side of the book you would *hit*
        to get out right now, which is what makes this a price rather than a
        coincidence.
        """
        pos = position()
        assert closing_natural_debit(pos.legs, book()) == EXPECTED_NATURAL_DEBIT
        assert SHORT_ASK - LONG_BID == EXPECTED_NATURAL_DEBIT

    def test_the_midpoint_is_short_mid_minus_long_mid(self) -> None:
        """9.105 - 8.895 = 0.210. A valuation, not an exit price."""
        pos = position()
        assert closing_midpoint_debit(pos.legs, book()) == EXPECTED_MIDPOINT_DEBIT

    def test_crossing_the_market_costs_more_than_the_mid(self) -> None:
        """The natural is always at or above the midpoint on an uncrossed book.

        The difference is half the sum of the two leg spreads -- here
        (0.03 + 0.03) / 2 = 0.03 -- and it is what an immediate exit gives up.
        """
        pos = position()
        natural = closing_natural_debit(pos.legs, book())
        midpoint = closing_midpoint_debit(pos.legs, book())
        assert natural > midpoint
        assert natural - midpoint == (
            (SHORT_ASK - SHORT_BID) + (LONG_ASK - LONG_BID)
        ) / D("2")

    def test_a_missing_ask_on_the_short_leg_has_no_natural(self) -> None:
        """Buying back the short leg needs its ask. Without one there is no
        closing price, and the last trade would be a number backed by nothing."""
        pos = position()
        assert closing_natural_debit(pos.legs, book(short_ask=None)) is None

    def test_a_missing_bid_on_the_long_leg_has_no_natural(self) -> None:
        """Selling the long leg needs its bid. The mirror of the case above."""
        pos = position()
        assert closing_natural_debit(pos.legs, book(long_bid=None)) is None

    def test_no_snapshot_is_no_price(self) -> None:
        assert closing_natural_debit(position().legs, None) is None
        assert closing_midpoint_debit(position().legs, None) is None


class TestTheDirectionOfTheClose:
    """The pairing, pinned. This is the test the whole module exists to keep.

    Closing a credit spread BUYS it back: the short leg is bought at its **ask**
    and the long leg sold at its **bid**. The inverted pairing is not merely a
    different convention -- it is a number that cannot be traded.
    """

    def test_swapping_bid_and_ask_produces_a_different_number(self) -> None:
        pos = position()
        correct = closing_debit(
            pos.legs, book(), short_side=QuoteSide.ASK, long_side=QuoteSide.BID
        )
        inverted = closing_debit(
            pos.legs, book(), short_side=QuoteSide.BID, long_side=QuoteSide.ASK
        )
        assert correct != inverted
        assert correct == EXPECTED_NATURAL_DEBIT
        assert inverted == D("0.18")

    def test_the_inverted_pairing_flatters(self) -> None:
        """It always reports a *cheaper* buy-back than the one available.

        Cheaper is the dangerous direction: it moves the position toward the
        profit target, which is the direction that sends an order.
        """
        pos = position()
        correct = closing_debit(
            pos.legs, book(), short_side=QuoteSide.ASK, long_side=QuoteSide.BID
        )
        inverted = closing_debit(
            pos.legs, book(), short_side=QuoteSide.BID, long_side=QuoteSide.ASK
        )
        assert inverted < correct

    def test_the_inverted_pairing_reports_a_six_dollar_loss_as_flat(self) -> None:
        """On this exact book the flattering number is the entry credit itself.

        A position genuinely down $6.00 at the natural reports as dead flat.
        Nothing about 0.18 looks wrong; it is simply what you would pay if two
        independent counterparties both happened to come to you.
        """
        pos = position()
        inverted = closing_debit(
            pos.legs, book(), short_side=QuoteSide.BID, long_side=QuoteSide.ASK
        )
        contracts = D(pos.quantity) * D(pos.multiplier)
        assert (pos.filled_credit - inverted) * contracts == D("0.00")
        assert (
            pos.filled_credit - EXPECTED_NATURAL_DEBIT
        ) * contracts == EXPECTED_GROSS_AT_NATURAL

    def test_the_module_constants_name_the_correct_sides(self) -> None:
        """A reader must not have to reconstruct the pairing from arithmetic."""
        from engine.options.marking import CLOSING_LONG_SIDE, CLOSING_SHORT_SIDE

        assert CLOSING_SHORT_SIDE is QuoteSide.ASK
        assert CLOSING_LONG_SIDE is QuoteSide.BID

    def test_the_closing_debit_is_not_the_negated_opening_credit(self) -> None:
        """Both cross the market, in opposite directions. Neither negates the other.

        ``pricing.natural_credit`` sells the short at its bid and buys the long
        at its ask -- 9.09 - 8.91 = 0.18. Negating that gives -0.18, which is
        neither the closing debit (0.240) nor anything tradeable. A caller that
        "reused" the opening function and flipped the sign would be exactly one
        sign away from looking right.
        """
        from engine.options.pricing import natural_credit

        pos = position()
        opening = natural_credit(pos.intent, book())
        assert opening == D("0.18")
        assert -opening != EXPECTED_NATURAL_DEBIT


# ===========================================================================
# A complete fresh book -- the numbers, asserted exactly
# ===========================================================================


class TestACompleteFreshBookMarks:
    def test_the_state_is_marked(self) -> None:
        pos = position()
        report = mark(pos, commission=evidence_for(pos))
        assert report.state is MarkState.MARKED
        assert report.reason_code == MarkReason.OK.value

    def test_both_closing_prices_are_the_observed_ones(self) -> None:
        pos = position()
        report = mark(pos, commission=evidence_for(pos))
        assert report.closing_midpoint_debit == EXPECTED_MIDPOINT_DEBIT
        assert report.closing_natural_debit == EXPECTED_NATURAL_DEBIT

    def test_gross_unrealized_is_minus_three_at_mid_and_minus_six_at_natural(
        self,
    ) -> None:
        """(0.18 - 0.210) x 100 = -3.00 and (0.18 - 0.240) x 100 = -6.00.

        Per share times the multiplier times the contracts held. Marking per
        share and comparing against a per-contract figure would report every
        position as a hundred times away from its target.
        """
        pos = position()
        report = mark(pos, commission=evidence_for(pos))
        assert report.gross_at_midpoint == EXPECTED_GROSS_AT_MIDPOINT
        assert report.gross_at_natural == EXPECTED_GROSS_AT_NATURAL

    def test_net_is_gross_less_the_commission_actually_reported(self) -> None:
        pos = position()
        evidence = evidence_for(pos)
        report = mark(pos, commission=evidence)
        assert evidence.is_complete
        assert report.commission_paid == D("-1.30")
        assert report.net_at_midpoint == EXPECTED_GROSS_AT_MIDPOINT - D("-1.30")
        assert report.net_at_natural == EXPECTED_GROSS_AT_NATURAL - D("-1.30")

    def test_the_quantity_marked_is_what_is_held(self) -> None:
        pos = position()
        report = mark(pos, commission=evidence_for(pos))
        assert report.quantity_marked == 1
        assert report.confirmed_remaining == 1

    def test_the_liveness_and_timestamp_are_carried(self) -> None:
        """Provenance travels with the mark, not alongside it in a log line."""
        pos = position()
        report = mark(pos, commission=evidence_for(pos))
        assert report.liveness == "LIVE"
        assert report.quote_as_of == NOW

    def test_the_report_survives_a_round_trip_to_a_record(self) -> None:
        pos = position()
        record = mark(pos, commission=evidence_for(pos)).to_record()
        assert record["state"] == "MARKED"
        # Compared numerically: the record keeps the unrounded Decimal, and a
        # subtraction and a division legitimately produce different exponents
        # for the same value. Only ``describe`` normalizes precision.
        assert D(record["closing_midpoint_debit"]) == EXPECTED_MIDPOINT_DEBIT
        assert D(record["closing_natural_debit"]) == EXPECTED_NATURAL_DEBIT
        assert D(record["gross_at_natural"]) == EXPECTED_GROSS_AT_NATURAL
        assert D(record["profit_target_debit"]) == EXPECTED_PROFIT_TARGET

    def test_the_operator_line_names_the_state_and_both_prices(self) -> None:
        """Both prices to the same precision, so a column can be read."""
        pos = position()
        text = mark(pos, commission=evidence_for(pos)).describe()
        assert "MARKED" in text
        assert "0.210" in text and "0.240" in text
        assert "-3.00" in text and "-6.00" in text
        assert "0.090" in text


# ===========================================================================
# The 50% profit target, on both sides of 0.090
# ===========================================================================


class TestTheProfitTarget:
    def test_the_target_debit_is_half_the_credit(self) -> None:
        """0.18 credit, 50% of max profit, means buying it back for 0.090."""
        pos = position()
        report = mark(pos, commission=evidence_for(pos))
        assert report.profit_target_debit == EXPECTED_PROFIT_TARGET

    def test_the_real_book_is_nowhere_near_the_target(self) -> None:
        """0.240 against a 0.090 target. The rule must not fire, and until this
        module existed it could not even be evaluated."""
        pos = position()
        report = mark(pos, commission=evidence_for(pos))
        assert report.profit_target_reached is False

    @pytest.mark.parametrize(
        "natural,expected",
        [
            (D("0.110"), False),  # above the target
            (D("0.100"), False),  # still above
            (D("0.095"), False),  # a hair above
            (D("0.090"), True),  # exactly at it, and ``<=`` fires
            (D("0.085"), True),  # below
            (D("0.050"), True),  # well below
        ],
    )
    def test_the_threshold_evaluates_correctly_on_both_sides(
        self, natural: Decimal, expected: bool
    ) -> None:
        """``<=``, not ``<``: a target exactly reached is reached.

        The boundary case is the one an off-by-one comparison gets wrong, and it
        is the one where the rule either fires or sits on a position that has
        done exactly what it was asked to do. The book is moved coherently --
        both sides of both legs -- because a book with one side edited is a
        crossed book, which is refused for a different reason entirely.
        """
        pos = position()
        report = mark(
            pos, book_at_natural(natural), commission=evidence_for(pos)
        )
        assert report.closing_natural_debit == natural
        assert report.profit_target_reached is expected

    def test_the_target_is_measured_against_the_natural_not_the_midpoint(self) -> None:
        """A target "reached" only at the mid is a target that will not fill.

        Here the midpoint is 0.085 and the natural is 0.100: the mid is under the
        0.090 target and the price a close can actually be done at is not. Firing
        on the mid is how a profit-target order rests all day at a price nobody
        will trade.
        """
        pos = position()
        report = mark(
            pos,
            book(
                short_bid=D("8.99"),
                short_ask=D("9.00"),
                long_bid=D("8.90"),
                long_ask=D("8.92"),
            ),
            commission=evidence_for(pos),
        )
        assert report.closing_midpoint_debit == D("0.085")
        assert report.closing_natural_debit == D("0.100")
        assert report.profit_target_reached is False

    def test_a_non_default_fraction_is_not_the_inverted_formula(self) -> None:
        """At 0.75 the two candidate formulas differ; at 0.50 they agree.

        0.18 x (1 - 0.75) = 0.045, and the inverted 0.18 x 0.75 = 0.135.
        """
        pos = position()
        report = mark(
            pos,
            commission=evidence_for(pos),
            policy=RiskPolicy(profit_target_fraction=D("0.75")),
        )
        assert report.profit_target_debit == D("0.0450")
        assert report.profit_target_debit != D("0.135")


# ===========================================================================
# STALE -- a quote that exists and may not be used
# ===========================================================================


class TestStale:
    def test_an_aged_quote_yields_stale_and_no_number(self) -> None:
        """The four states are states. A price you may not act on must not be
        reported as one you may, so STALE carries no price at all."""
        pos = position()
        stale_at = NOW - dt.timedelta(minutes=5)
        report = mark(pos, book(at=stale_at), commission=evidence_for(pos))
        assert report.state is MarkState.STALE
        assert report.closing_midpoint_debit is None
        assert report.closing_natural_debit is None
        assert report.gross_at_midpoint is None
        assert report.gross_at_natural is None
        assert report.net_at_midpoint is None
        assert report.profit_target_reached is None

    def test_the_market_data_reason_is_carried_verbatim(self) -> None:
        """"Buy a subscription" and "the quote aged out" are different problems
        with the same state, so the precise code travels with it."""
        pos = position()
        report = mark(
            pos, book(at=NOW - dt.timedelta(minutes=5)), commission=evidence_for(pos)
        )
        assert report.reason_code == "MARKET_DATA_STALE"

    def test_delayed_data_is_refused_with_its_own_reason(self) -> None:
        """The account's actual blocker. Delayed quotes arrive promptly and would
        pass any check built on local receipt time -- and they must never mark a
        position that a profit target will be taken on."""
        pos = position()
        report = mark(
            pos, book(reported=MarketDataType.DELAYED), commission=evidence_for(pos)
        )
        assert report.state is MarkState.STALE
        assert report.reason_code == "OPTIONS_REALTIME_DATA_REQUIRED"
        assert report.closing_natural_debit is None

    def test_frozen_data_is_refused_too(self) -> None:
        pos = position()
        report = mark(
            pos, book(reported=MarketDataType.FROZEN), commission=evidence_for(pos)
        )
        assert report.state is MarkState.STALE
        assert report.closing_natural_debit is None

    def test_a_quote_just_inside_the_age_limit_still_marks(self) -> None:
        """The refusal must be the age, not the mere presence of an age check."""
        pos = position()
        policy = RiskPolicy(quote_maximum_age=dt.timedelta(seconds=10))
        report = mark(
            pos,
            book(at=NOW - dt.timedelta(seconds=9)),
            policy=policy,
            commission=evidence_for(pos),
        )
        assert report.state is MarkState.MARKED
        assert report.closing_natural_debit == EXPECTED_NATURAL_DEBIT

    def test_a_quote_the_provider_never_classified_is_not_live(self) -> None:
        """``Ticker.marketDataType`` defaults to 1, so silence is not evidence."""
        pos = position()
        quiet = book()
        legs = tuple(
            OptionQuote(
                con_id=leg.con_id,
                provenance=MarketDataProvenance(
                    requested_type=int(MarketDataType.LIVE),
                    subscription_generation=leg.provenance.subscription_generation,
                    subscribed_at=NOW,
                    reported_type=None,
                    callback_received=False,
                    last_provider_event_at=NOW,
                    last_local_receive_at=NOW,
                ),
                bid=leg.bid,
                ask=leg.ask,
            )
            for leg in quiet.legs
        )
        snapshot = StrategyQuoteSnapshot(
            underlying=quiet.underlying, legs=legs, generations=quiet.generations
        )
        report = mark(pos, snapshot, commission=evidence_for(pos))
        assert report.state is MarkState.STALE
        assert report.reason_code == "MARKET_DATA_TYPE_CALLBACK_MISSING"


# ===========================================================================
# UNAVAILABLE -- there is no usable book at all
# ===========================================================================


class TestUnavailable:
    def test_a_missing_leg_quote_yields_unavailable(self) -> None:
        """A structure cannot be marked from a subset of its legs. Marking the
        one leg that *is* quoted would price half a spread as a whole one."""
        pos = position()
        report = mark(
            pos, book(drop_leg=LONG_CON_ID), commission=evidence_for(pos)
        )
        assert report.state is MarkState.UNAVAILABLE
        assert report.reason_code == MarkReason.LEG_QUOTE_MISSING.value
        assert report.closing_midpoint_debit is None
        assert report.closing_natural_debit is None

    def test_the_missing_leg_is_named(self) -> None:
        pos = position()
        report = mark(pos, book(drop_leg=LONG_CON_ID), commission=evidence_for(pos))
        assert str(LONG_CON_ID) in report.detail

    def test_no_snapshot_at_all_yields_unavailable(self) -> None:
        pos = position()
        report = mark(pos, None, sentinel=False, commission=evidence_for(pos))
        assert report.state is MarkState.UNAVAILABLE
        assert report.reason_code == MarkReason.NO_SNAPSHOT.value

    def test_a_one_sided_market_yields_unavailable(self) -> None:
        """Every leg is quoted; the short leg simply has no ask. There is no
        closing price, and the close would be a number backed by nothing."""
        pos = position()
        report = mark(pos, book(short_ask=None), commission=evidence_for(pos))
        assert report.state is MarkState.UNAVAILABLE
        assert report.reason_code == MarkReason.ONE_SIDED_MARKET.value

    def test_an_adapter_refusal_carries_the_brokers_own_reason(self) -> None:
        from engine.errors import MarketDataRefusedError

        pos = position()
        report = mark(
            pos,
            None,
            sentinel=False,
            commission=evidence_for(pos),
            quotes_error=MarketDataRefusedError(
                "OPTIONS_REALTIME_DATA_REQUIRED", "delayed only"
            ),
        )
        assert report.state is MarkState.UNAVAILABLE
        assert report.reason_code == "OPTIONS_REALTIME_DATA_REQUIRED"

    def test_a_crossed_book_is_refused_rather_than_marked_cheap(self) -> None:
        """Natural below midpoint is algebraically impossible on a real book, so
        it is bad data -- and the direction it is wrong in is the cheap one."""
        pos = position()
        report = mark(
            pos,
            # The short leg's ask is below its bid, so crossing to exit appears
            # to cost less than the mid -- which cannot happen on a real book.
            book(short_bid=D("9.20"), short_ask=D("9.09")),
            commission=evidence_for(pos),
        )
        assert report.state is MarkState.UNAVAILABLE
        assert report.reason_code == MarkReason.CROSSED_BOOK.value

    def test_an_unavailable_position_still_reports_its_target(self) -> None:
        """The target needs no market data. Withholding it too would hide the
        one number that is still knowable."""
        pos = position()
        report = mark(pos, None, sentinel=False, commission=evidence_for(pos))
        assert report.profit_target_debit == EXPECTED_PROFIT_TARGET


# ===========================================================================
# COMMISSION_INCOMPLETE -- marked, but the fill was never costed
# ===========================================================================


class TestCommissionIncomplete:
    def test_a_missing_commission_yields_commission_incomplete(self) -> None:
        """The real fill: commission came back None and was never persisted."""
        pos = position()
        evidence = evidence_for(pos, both_legs_filled(pos.strategy_id, commission=None))
        assert evidence.is_complete is False
        report = mark(pos, commission=evidence)
        assert report.state is MarkState.COMMISSION_INCOMPLETE

    def test_gross_is_still_reported(self) -> None:
        """This is the whole point of the fourth state. The mark is good; only
        the cost is unknown, so gross stands and net is withheld."""
        pos = position()
        report = mark(
            pos,
            commission=evidence_for(
                pos, both_legs_filled(pos.strategy_id, commission=None)
            ),
        )
        assert report.closing_midpoint_debit == EXPECTED_MIDPOINT_DEBIT
        assert report.closing_natural_debit == EXPECTED_NATURAL_DEBIT
        assert report.gross_at_midpoint == EXPECTED_GROSS_AT_MIDPOINT
        assert report.gross_at_natural == EXPECTED_GROSS_AT_NATURAL

    def test_net_is_withheld_and_is_never_zero(self) -> None:
        """Substituting zero would make net equal gross and look complete."""
        pos = position()
        report = mark(
            pos,
            commission=evidence_for(
                pos, both_legs_filled(pos.strategy_id, commission=None)
            ),
        )
        assert report.net_at_midpoint is None
        assert report.net_at_natural is None
        assert report.commission_paid is None

    def test_the_profit_target_is_still_evaluated(self) -> None:
        """A missing commission does not stop the mark being usable for the rule
        the mark exists to feed; only net accounting needs the cost."""
        pos = position()
        report = mark(
            pos,
            commission=evidence_for(
                pos, both_legs_filled(pos.strategy_id, commission=None)
            ),
        )
        assert report.profit_target_reached is False

    def test_no_evidence_at_all_is_also_commission_incomplete(self) -> None:
        pos = position()
        report = mark(pos, commission=None)
        assert report.state is MarkState.COMMISSION_INCOMPLETE
        assert report.gross_at_natural == EXPECTED_GROSS_AT_NATURAL

    def test_the_gaps_say_what_is_missing(self) -> None:
        """An operator has to know what to go and fetch."""
        pos = position()
        report = mark(
            pos,
            commission=evidence_for(
                pos, both_legs_filled(pos.strategy_id, commission=None)
            ),
        )
        assert report.commission_gaps
        assert any("commission report" in gap for gap in report.commission_gaps)

    def test_one_costed_leg_is_not_complete_evidence(self) -> None:
        """Half a spread's cost is a real, finite, too-small number."""
        pos = position()
        fills = [
            fill(
                con_id=SHORT_CON_ID,
                exec_id="0001.a",
                side="SLD",
                price=9.10,
                strategy_id=pos.strategy_id,
                commission=-0.65,
            ),
            fill(
                con_id=LONG_CON_ID,
                exec_id="0001.b",
                side="BOT",
                price=8.92,
                strategy_id=pos.strategy_id,
                commission=None,
            ),
        ]
        evidence = evidence_for(pos, fills)
        assert evidence.is_complete is False
        assert evidence.total_commission is None
        assert evidence.observed_commission == D("-0.65")
        assert mark(pos, commission=evidence).state is MarkState.COMMISSION_INCOMPLETE

    def test_a_leg_with_no_execution_is_not_complete_evidence(self) -> None:
        pos = position()
        fills = [
            fill(
                con_id=SHORT_CON_ID,
                exec_id="0001.a",
                side="SLD",
                price=9.10,
                strategy_id=pos.strategy_id,
                commission=-0.65,
            )
        ]
        evidence = evidence_for(pos, fills)
        assert evidence.is_complete is False
        assert any(str(LONG_CON_ID) in gap for gap in evidence.gaps)

    def test_marked_outranks_commission_incomplete_only_when_costed(self) -> None:
        """The two states differ by exactly one fact."""
        pos = position()
        assert mark(pos, commission=evidence_for(pos)).state is MarkState.MARKED
        assert (
            mark(
                pos,
                commission=evidence_for(
                    pos, both_legs_filled(pos.strategy_id, commission=None)
                ),
            ).state
            is MarkState.COMMISSION_INCOMPLETE
        )


# ===========================================================================
# Precedence between the four states
# ===========================================================================


class TestStatePrecedence:
    def test_no_quotes_and_no_commission_reports_unavailable(self) -> None:
        """The missing commission is not what is stopping it being marked."""
        pos = position()
        report = mark(pos, None, sentinel=False, commission=None)
        assert report.state is MarkState.UNAVAILABLE

    def test_stale_quotes_and_no_commission_reports_stale(self) -> None:
        pos = position()
        report = mark(pos, book(reported=MarketDataType.DELAYED), commission=None)
        assert report.state is MarkState.STALE

    def test_exactly_four_states_exist(self) -> None:
        """The operator surface is a closed set, not an open vocabulary."""
        assert {s.value for s in MarkState} == {
            "MARKED",
            "STALE",
            "UNAVAILABLE",
            "COMMISSION_INCOMPLETE",
        }

    def test_only_the_two_priced_states_report_a_mark(self) -> None:
        assert MarkState.MARKED.has_mark
        assert MarkState.COMMISSION_INCOMPLETE.has_mark
        assert not MarkState.STALE.has_mark
        assert not MarkState.UNAVAILABLE.has_mark


class TestTheReportRefusesToLie:
    """The invariants that make a dishonest report unconstructable."""

    def _fields(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "position_id": uuid4(),
            "underlying": "SPY",
            "state": MarkState.STALE,
            "reason_code": "MARKET_DATA_STALE",
            "detail": "aged out",
            "evaluated_at": NOW,
            "entry_credit": ENTRY_CREDIT,
            "multiplier": 100,
            "quantity_marked": 0,
            "confirmed_remaining": 1,
            "profit_target_debit": EXPECTED_PROFIT_TARGET,
        }
        base.update(overrides)
        return base

    def test_a_stale_report_cannot_carry_a_price(self) -> None:
        with pytest.raises(ValueError, match="must not carry a price"):
            PositionMarkReport(
                **self._fields(closing_natural_debit=EXPECTED_NATURAL_DEBIT)
            )

    def test_a_marked_report_cannot_omit_a_net(self) -> None:
        with pytest.raises(ValueError, match="belongs in COMMISSION_INCOMPLETE"):
            PositionMarkReport(
                **self._fields(
                    state=MarkState.MARKED,
                    reason_code="MARK_OK",
                    closing_midpoint_debit=EXPECTED_MIDPOINT_DEBIT,
                    closing_natural_debit=EXPECTED_NATURAL_DEBIT,
                    gross_at_midpoint=EXPECTED_GROSS_AT_MIDPOINT,
                    gross_at_natural=EXPECTED_GROSS_AT_NATURAL,
                    profit_target_reached=False,
                )
            )

    def test_an_unmarked_report_cannot_carry_a_net(self) -> None:
        with pytest.raises(ValueError, match="must not carry a net"):
            PositionMarkReport(**self._fields(net_at_natural=D("-6.00")))

    def test_commission_incomplete_must_say_what_is_missing(self) -> None:
        with pytest.raises(ValueError, match="must say what is missing"):
            PositionMarkReport(
                **self._fields(
                    state=MarkState.COMMISSION_INCOMPLETE,
                    reason_code="MARK_COMMISSION_INCOMPLETE",
                    closing_midpoint_debit=EXPECTED_MIDPOINT_DEBIT,
                    closing_natural_debit=EXPECTED_NATURAL_DEBIT,
                    gross_at_midpoint=EXPECTED_GROSS_AT_MIDPOINT,
                    gross_at_natural=EXPECTED_GROSS_AT_NATURAL,
                    profit_target_reached=False,
                    commission_gaps=(),
                )
            )

    def test_every_report_carries_a_machine_readable_reason(self) -> None:
        with pytest.raises(ValueError, match="machine-readable reason"):
            PositionMarkReport(**self._fields(reason_code="  "))


# ===========================================================================
# The close proposal, and the quantity it must never exceed
# ===========================================================================


class TestTheCloseProposal:
    def test_a_marked_position_carries_a_proposal(self) -> None:
        pos = position()
        report = mark(pos, commission=evidence_for(pos))
        assert report.close_proposal is not None
        assert report.close_proposal.quantity == 1

    def test_the_proposal_is_priced_at_the_natural(self) -> None:
        """The price a close can actually be done at. A proposal at the midpoint
        is the order that rested unfilled for 101 minutes, again."""
        pos = position()
        report = mark(pos, commission=evidence_for(pos))
        assert report.close_proposal is not None
        assert report.close_proposal.limit_price == EXPECTED_NATURAL_DEBIT

    def test_the_proposal_closes_the_exact_contracts_that_were_opened(self) -> None:
        """Inverted legs, same con_ids. A close cannot land on a contract the
        position never held."""
        pos = position()
        report = mark(pos, commission=evidence_for(pos))
        assert report.close_proposal is not None
        intent = report.close_proposal.intent
        assert intent.strategy_action is StrategyAction.CLOSE
        assert {leg.con_id for leg in intent.legs} == {SHORT_CON_ID, LONG_CON_ID}
        short = next(leg for leg in intent.legs if leg.con_id == SHORT_CON_ID)
        assert short.action is OrderAction.BUY  # bought back
        assert intent.price_effect is PriceEffect.DEBIT
        assert intent.closes_strategy_id == pos.strategy_id

    # -- the quantity bound ------------------------------------------------

    def test_a_partial_fill_proposes_only_what_filled(self) -> None:
        """Ledger C21: a three-contract order that filled one must propose one.
        Closing the ordered size sells contracts that were never bought."""
        pos = position(quantity=3, filled_quantity=D("1"))
        report = mark(pos, commission=evidence_for(pos))
        assert report.confirmed_remaining == 1
        assert report.close_proposal is not None
        assert report.close_proposal.quantity == 1
        assert report.close_proposal.intent.quantity == 1

    def test_a_partly_closed_position_proposes_only_what_remains(self) -> None:
        """Three bought, two already retired by a cancelled-after-partial exit.
        One is held. ``manageable_quantity`` says three; only ``remaining`` is
        the truth, and sizing off the former sells two contracts nobody holds."""
        pos = position(
            quantity=3, filled_quantity=D("3"), close_filled_quantity=D("2")
        )
        assert pos.manageable_quantity == 3
        assert confirmed_remaining_quantity(pos) == 1
        report = mark(pos, commission=evidence_for(pos))
        assert report.close_proposal is not None
        assert report.close_proposal.quantity == 1
        assert report.close_proposal.confirmed_remaining == 1

    def test_a_fully_closed_position_proposes_nothing(self) -> None:
        pos = position(
            quantity=2, filled_quantity=D("2"), close_filled_quantity=D("2")
        )
        assert confirmed_remaining_quantity(pos) == 0
        assert (
            propose_close(
                pos,
                limit_price=EXPECTED_NATURAL_DEBIT,
                basis="NATURAL",
                created_at=NOW,
                configuration_version="test",
            )
            is None
        )

    def test_asking_for_more_than_is_held_is_capped_not_honoured(self) -> None:
        pos = position(quantity=3, filled_quantity=D("1"))
        proposal = propose_close(
            pos,
            limit_price=EXPECTED_NATURAL_DEBIT,
            basis="NATURAL",
            created_at=NOW,
            configuration_version="test",
            quantity=3,
        )
        assert proposal is not None
        assert proposal.quantity == 1

    def test_the_bound_is_enforced_by_the_type_not_only_by_the_builder(self) -> None:
        """A bound merely *satisfied* at one call site stops holding when a
        second appears. This is the one that keeps holding."""
        pos = position(quantity=3, filled_quantity=D("3"))
        intent = pos.intent.closing_intent(
            strategy_id=uuid4(),
            limit_price=EXPECTED_NATURAL_DEBIT,
            created_at=NOW,
            configuration_version="test",
            quantity=3,
        )
        with pytest.raises(InvalidStrategyError, match="confirmed remaining"):
            CloseProposal(
                position_id=pos.strategy_id,
                quantity=3,
                confirmed_remaining=1,
                limit_price=EXPECTED_NATURAL_DEBIT,
                basis="NATURAL",
                intent=intent,
            )

    def test_a_proposal_and_its_intent_cannot_disagree_about_size(self) -> None:
        pos = position(quantity=3, filled_quantity=D("3"))
        intent = pos.intent.closing_intent(
            strategy_id=uuid4(),
            limit_price=EXPECTED_NATURAL_DEBIT,
            created_at=NOW,
            configuration_version="test",
            quantity=2,
        )
        with pytest.raises(InvalidStrategyError, match="its intent says"):
            CloseProposal(
                position_id=pos.strategy_id,
                quantity=3,
                confirmed_remaining=3,
                limit_price=EXPECTED_NATURAL_DEBIT,
                basis="NATURAL",
                intent=intent,
            )

    def test_a_zero_or_negative_quantity_is_refused(self) -> None:
        pos = position()
        intent = pos.intent.closing_intent(
            strategy_id=uuid4(),
            limit_price=EXPECTED_NATURAL_DEBIT,
            created_at=NOW,
            configuration_version="test",
            quantity=1,
        )
        with pytest.raises(InvalidStrategyError, match="must be positive"):
            CloseProposal(
                position_id=pos.strategy_id,
                quantity=0,
                confirmed_remaining=1,
                limit_price=EXPECTED_NATURAL_DEBIT,
                basis="NATURAL",
                intent=intent,
            )

    def test_a_position_that_is_not_open_gets_no_proposal(self) -> None:
        """A CLOSING position already has a working order; a second close against
        it is how one lot becomes two."""
        pos = position(state=PositionState.CLOSING)
        report = mark(pos, commission=evidence_for(pos))
        assert report.state is MarkState.MARKED
        assert report.close_proposal is None

    def test_a_refused_mark_carries_no_proposal(self) -> None:
        """Nothing is proposed off a price that could not be established."""
        pos = position()
        report = mark(pos, book(reported=MarketDataType.DELAYED))
        assert report.state is MarkState.STALE
        assert report.close_proposal is None

    def test_the_proposal_can_be_switched_off_entirely(self) -> None:
        pos = position()
        report = mark(pos, commission=evidence_for(pos), propose=False)
        assert report.state is MarkState.MARKED
        assert report.close_proposal is None


# ===========================================================================
# Executions and commissions
# ===========================================================================


class TestExecutionCapture:
    def test_an_unpopulated_commission_report_is_not_evidence(self) -> None:
        """``CommissionReport.commission`` defaults to 0.0 and ``execId`` to ''.
        A fill whose report never arrived therefore presents a perfectly finite,
        perfectly plausible commission of zero."""
        pos = position()
        records = executions_from_fills(
            both_legs_filled(pos.strategy_id, commission=None)
        )
        assert len(records) == 2
        assert all(r.commission is None for r in records)
        assert not any(r.has_commission for r in records)

    def test_a_matching_report_is_evidence(self) -> None:
        pos = position()
        records = executions_from_fills(both_legs_filled(pos.strategy_id))
        assert all(r.commission == D("-0.65") for r in records)

    def test_a_report_naming_a_different_execution_is_not_evidence(self) -> None:
        """Cross-checked on execId, not merely on presence."""
        pos = position()
        bad = FakeFill(
            contract=FakeContract(SHORT_CON_ID),
            execution=FakeExecution(
                exec_id="0001.a",
                side="SLD",
                shares=1.0,
                price=9.10,
                order_ref=str(pos.strategy_id),
            ),
            report=FakeCommissionReport(exec_id="9999.z", commission=-0.65),
        )
        records = executions_from_fills([bad])
        assert records[0].commission is None

    def test_dbl_max_is_not_a_commission(self) -> None:
        pos = position()
        record = executions_from_fills(
            [
                fill(
                    con_id=SHORT_CON_ID,
                    exec_id="0001.a",
                    side="SLD",
                    price=9.10,
                    strategy_id=pos.strategy_id,
                    commission=1.7976931348623157e308,
                )
            ]
        )[0]
        assert record.commission is None

    def test_the_same_execution_delivered_twice_is_counted_once(self) -> None:
        """``ib.fills()`` accumulates and a reconnect re-delivers. Double
        counting overstates the cost, which understates net profit."""
        pos = position()
        fills = both_legs_filled(pos.strategy_id) + both_legs_filled(pos.strategy_id)
        records = executions_from_fills(fills)
        assert len(records) == 2

    def test_a_later_delivery_with_a_commission_supersedes_one_without(self) -> None:
        """Evidence is learned, never un-learned."""
        pos = position()
        fills = both_legs_filled(pos.strategy_id, commission=None) + both_legs_filled(
            pos.strategy_id
        )
        records = executions_from_fills(fills)
        assert len(records) == 2
        assert all(r.commission == D("-0.65") for r in records)

    def test_another_tools_execution_is_not_ours(self) -> None:
        """This engine does not own the account."""
        pos = position()
        stranger = FakeFill(
            contract=FakeContract(SHORT_CON_ID),
            execution=FakeExecution(
                exec_id="8888.x",
                side="SLD",
                shares=1.0,
                price=9.10,
                order_ref="somebody-elses-ticket",
                order_id=999,
                perm_id=888,
            ),
            report=FakeCommissionReport(exec_id="8888.x", commission=-99.0),
        )
        evidence = evidence_for(pos, both_legs_filled(pos.strategy_id) + [stranger])
        assert evidence.total_commission == D("-1.30")
        assert all(e.exec_id != "8888.x" for e in evidence.executions)

    def test_partial_share_coverage_is_a_gap(self) -> None:
        """A three-lot whose executions only account for one contract."""
        pos = position(quantity=3, filled_quantity=D("3"))
        evidence = evidence_for(pos, both_legs_filled(pos.strategy_id, shares=1.0))
        assert evidence.is_complete is False
        assert any("covered for" in gap for gap in evidence.gaps)

    def test_full_share_coverage_is_complete(self) -> None:
        pos = position(quantity=3, filled_quantity=D("3"))
        evidence = evidence_for(pos, both_legs_filled(pos.strategy_id, shares=3.0))
        assert evidence.is_complete is True
        assert evidence.total_commission == D("-1.30")

    def test_incomplete_evidence_never_carries_a_total(self) -> None:
        """A partial sum is a real number in the right units that is too small."""
        with pytest.raises(ValueError, match="must not carry a total"):
            CommissionEvidence(
                strategy_id=uuid4(),
                executions=(),
                expected_con_ids=(SHORT_CON_ID,),
                is_complete=False,
                total_commission=D("-0.65"),
                gaps=("missing",),
            )

    def test_complete_evidence_must_carry_a_total(self) -> None:
        with pytest.raises(ValueError, match="must carry the total"):
            CommissionEvidence(
                strategy_id=uuid4(),
                executions=(),
                expected_con_ids=(SHORT_CON_ID,),
                is_complete=True,
                total_commission=None,
            )

    def test_an_execution_needs_a_qualified_contract(self) -> None:
        """A con_id of 0 is what an unqualified contract carries."""
        with pytest.raises(ValueError, match="must be positive"):
            ExecutionRecord(
                exec_id="0001.a",
                con_id=0,
                side="SLD",
                shares=D("1"),
                price=D("9.10"),
            )


# ===========================================================================
# Persistence -- the fact the store could not previously hold
# ===========================================================================


class TestCommissionPersistence:
    def _store(self, tmp_path: Any) -> PositionStore:
        return PositionStore(tmp_path / "positions.jsonl")

    def test_a_complete_capture_is_persisted_and_replays(self, tmp_path: Any) -> None:
        store = self._store(tmp_path)
        pos = position()
        store.record_open_submitted(
            pos.intent, at=NOW, buying_power_reserved=D("82")
        )
        store.record_open_filled(
            pos.strategy_id, at=NOW, filled_credit=ENTRY_CREDIT, filled_quantity=D("1")
        )
        evidence = evidence_for(pos)
        store.record_executions(
            pos.strategy_id,
            at=NOW,
            executions=[e.to_record() for e in evidence.executions],
            total_commission=evidence.total_commission,
            complete=True,
        )
        replayed = store.get(pos.strategy_id)
        assert replayed is not None
        assert replayed.commission == D("-1.30")
        assert replayed.commission_complete is True

    def test_an_incomplete_capture_is_persisted_as_incomplete(
        self, tmp_path: Any
    ) -> None:
        """The durable difference between "cost nothing" and "nobody asked"."""
        store = self._store(tmp_path)
        pos = position()
        store.record_open_submitted(pos.intent, at=NOW, buying_power_reserved=D("82"))
        store.record_open_filled(
            pos.strategy_id, at=NOW, filled_credit=ENTRY_CREDIT, filled_quantity=D("1")
        )
        evidence = evidence_for(
            pos, both_legs_filled(pos.strategy_id, commission=None)
        )
        store.record_executions(
            pos.strategy_id,
            at=NOW,
            executions=[e.to_record() for e in evidence.executions],
            total_commission=None,
            complete=False,
            gaps=evidence.gaps,
        )
        replayed = store.get(pos.strategy_id)
        assert replayed is not None
        assert replayed.commission is None
        assert replayed.commission_complete is False

    def test_a_completeness_claim_without_a_total_is_refused(
        self, tmp_path: Any
    ) -> None:
        from engine.errors import InvalidPortfolioStateError

        store = self._store(tmp_path)
        pos = position()
        store.record_open_submitted(pos.intent, at=NOW, buying_power_reserved=D("82"))
        with pytest.raises(InvalidPortfolioStateError, match="must record the total"):
            store.record_executions(
                pos.strategy_id,
                at=NOW,
                executions=[],
                total_commission=None,
                complete=True,
            )

    def test_a_later_failed_capture_does_not_demote_proven_evidence(
        self, tmp_path: Any
    ) -> None:
        """A transient query failure must not make a net figure disappear."""
        store = self._store(tmp_path)
        pos = position()
        store.record_open_submitted(pos.intent, at=NOW, buying_power_reserved=D("82"))
        store.record_open_filled(
            pos.strategy_id, at=NOW, filled_credit=ENTRY_CREDIT, filled_quantity=D("1")
        )
        store.record_executions(
            pos.strategy_id,
            at=NOW,
            executions=[],
            total_commission=D("-1.30"),
            complete=True,
        )
        store.record_executions(
            pos.strategy_id,
            at=NOW,
            executions=[],
            total_commission=None,
            complete=False,
            gaps=("the broker could not be asked",),
        )
        replayed = store.get(pos.strategy_id)
        assert replayed is not None
        assert replayed.commission_complete is True
        assert replayed.commission == D("-1.30")

    def test_a_complete_flag_with_no_commission_is_unconstructable(self) -> None:
        from engine.errors import InvalidPortfolioStateError

        identifier = uuid4()
        with pytest.raises(InvalidPortfolioStateError, match="no commission is recorded"):
            OpenPosition(
                strategy_id=identifier,
                intent=spread_intent(strategy_id=identifier),
                opened_at=NOW,
                state=PositionState.OPEN,
                buying_power_reserved=D("82"),
                filled_credit=ENTRY_CREDIT,
                commission=None,
                commission_complete=True,
            )


class TestPartialCloseQuantitySurvivesReplay:
    """C24's field, and the replay defect that quietly emptied it.

    ``_replace`` carried a hand-written list of fields that never gained
    ``close_filled_quantity``, so every transition that did not pass it
    explicitly reset it to zero. A ``CLOSE_PARTIAL`` followed by a
    ``CLOSE_FAILED`` -- an exit cancelled after filling part of the way -- came
    back claiming to hold everything it had ever bought.
    """

    def _seeded(self, tmp_path: Any) -> tuple[PositionStore, OpenPosition]:
        store = PositionStore(tmp_path / "positions.jsonl")
        pos = position(quantity=3, filled_quantity=D("3"))
        store.record_open_submitted(pos.intent, at=NOW, buying_power_reserved=D("246"))
        store.record_open_filled(
            pos.strategy_id, at=NOW, filled_credit=ENTRY_CREDIT, filled_quantity=D("3")
        )
        store.record_partial_fill(
            pos.strategy_id, at=NOW, filled_quantity=D("2"), closing=True
        )
        return store, pos

    def test_a_cancelled_exit_after_a_partial_still_knows_what_is_held(
        self, tmp_path: Any
    ) -> None:
        store, pos = self._seeded(tmp_path)
        store.record_close_failed(pos.strategy_id, at=NOW, reason="cancelled")
        replayed = store.get(pos.strategy_id)
        assert replayed is not None
        assert replayed.state is PositionState.OPEN
        assert replayed.close_filled_quantity == D("2")
        assert replayed.remaining_quantity == D("1")

    def test_the_proposal_off_that_position_closes_one_not_three(
        self, tmp_path: Any
    ) -> None:
        """The whole reason the replay defect mattered: an exit sized off it
        would have sold two contracts that were already out of the market."""
        store, pos = self._seeded(tmp_path)
        store.record_close_failed(pos.strategy_id, at=NOW, reason="cancelled")
        replayed = store.get(pos.strategy_id)
        assert replayed is not None
        report = mark(replayed, commission=evidence_for(replayed))
        assert report.close_proposal is not None
        assert report.close_proposal.quantity == 1

    def test_an_acknowledgement_does_not_empty_it_either(self, tmp_path: Any) -> None:
        """Every transition, not only the one that was noticed."""
        store, pos = self._seeded(tmp_path)
        store.record_acknowledged(
            pos.strategy_id, at=NOW, closing=True, order_id=1664, perm_id=1151642182
        )
        replayed = store.get(pos.strategy_id)
        assert replayed is not None
        assert replayed.close_filled_quantity == D("2")


# ===========================================================================
# The pass over the whole book
# ===========================================================================


class FakeMarketData:
    """A :class:`~engine.options.ports.LiveMarketDataPort` that records what it
    was asked for, so the "only its own legs" claim can be asserted."""

    def __init__(self, *, snapshot: StrategyQuoteSnapshot | None = None) -> None:
        self.snapshot = snapshot if snapshot is not None else book()
        self.calls: list[tuple[str, tuple[int, ...]]] = []
        self.two_sided_requests: list[bool] = []

    def strategy_quotes(
        self,
        *,
        underlying_symbol: str,
        con_ids: Any,
        require_two_sided: bool = False,
    ) -> StrategyQuoteSnapshot:
        self.calls.append((underlying_symbol, tuple(int(c) for c in con_ids)))
        self.two_sided_requests.append(require_two_sided)
        return self.snapshot


class ExplodingMarketData:
    def __init__(self) -> None:
        self.calls = 0

    def strategy_quotes(
        self,
        *,
        underlying_symbol: str,
        con_ids: Any,
        require_two_sided: bool = False,
    ) -> Any:
        self.calls += 1
        raise RuntimeError("the socket went away")


class TestMarkingTheWholeBook:
    def test_held_structure_requests_two_sided_wait(self) -> None:
        pos = position()
        port = FakeMarketData()

        mark_open_positions(
            [pos], market_data=port, policy=RiskPolicy(), now=NOW
        )

        assert port.calls == [("SPY", (SHORT_CON_ID, LONG_CON_ID))]
        assert port.two_sided_requests == [True]

    def test_only_the_positions_own_underlying_and_legs_are_subscribed(self) -> None:
        """Not a chain window. A marking pass has no strike to select, so every
        extra subscription is quota spent for nothing."""
        pos = position()
        port = FakeMarketData()
        mark_open_positions(
            [pos], market_data=port, policy=RiskPolicy(), now=NOW
        )
        assert port.calls == [("SPY", (SHORT_CON_ID, LONG_CON_ID))]

    def test_an_adapter_exception_becomes_unavailable_not_an_outage(self) -> None:
        pos = position()
        reports = mark_open_positions(
            [pos], market_data=ExplodingMarketData(), policy=RiskPolicy(), now=NOW
        )
        assert len(reports) == 1
        assert reports[0].state is MarkState.UNAVAILABLE
        assert "the socket went away" in reports[0].detail

    def test_one_unquotable_position_does_not_stop_the_others(self) -> None:
        """A pass that aborts halfway is indistinguishable from one that found
        nothing."""
        good = position()
        bad = position()
        port = ExplodingMarketData()
        reports = mark_open_positions(
            [good, bad], market_data=port, policy=RiskPolicy(), now=NOW
        )
        assert len(reports) == 2
        assert port.calls == 2

    def test_no_market_data_port_marks_nothing_and_says_so(self) -> None:
        pos = position()
        reports = mark_open_positions(
            [pos], market_data=None, policy=RiskPolicy(), now=NOW
        )
        assert reports[0].state is MarkState.UNAVAILABLE

    def test_commission_evidence_is_matched_per_position(self) -> None:
        pos = position()
        reports = mark_open_positions(
            [pos],
            market_data=FakeMarketData(),
            policy=RiskPolicy(),
            now=NOW,
            commission_by_position={pos.strategy_id: evidence_for(pos)},
        )
        assert reports[0].state is MarkState.MARKED
        assert reports[0].commission_paid == D("-1.30")

    def test_the_whole_pass_is_reproducible_from_its_parameters(self) -> None:
        """No clock read anywhere; ``now`` is a parameter, so a report can be
        rebuilt from the record it wrote."""
        pos = position()
        first = mark_open_positions(
            [pos], market_data=FakeMarketData(), policy=RiskPolicy(), now=NOW
        )
        second = mark_open_positions(
            [pos], market_data=FakeMarketData(), policy=RiskPolicy(), now=NOW
        )
        assert first[0].to_record()["gross_at_natural"] == (
            second[0].to_record()["gross_at_natural"]
        )
        assert first[0].evaluated_at == second[0].evaluated_at == NOW


# ===========================================================================
# The adapter that reads executions, and the operator command
# ===========================================================================


class FakeIBWithFills:
    """The two read-only calls :class:`IBKRExecutionReportAdapter` makes."""

    def __init__(self, fills: list[FakeFill], *, refill_raises: bool = False) -> None:
        self._fills = fills
        self.refill_raises = refill_raises
        self.requested = 0

    def reqExecutions(self) -> None:  # noqa: N802 - ib_async's spelling
        self.requested += 1
        if self.refill_raises:
            raise RuntimeError("not connected")

    def fills(self) -> list[FakeFill]:
        return self._fills


class TestTheExecutionAdapter:
    def test_it_asks_the_server_before_reading_the_local_fill_set(self) -> None:
        """``ib.fills()`` holds only what this session received. After a restart
        that set is empty, and empty is indistinguishable from an uncosted fill."""
        from engine.options.adapters import IBKRExecutionReportAdapter

        pos = position()
        ib = FakeIBWithFills(both_legs_filled(pos.strategy_id))
        records = IBKRExecutionReportAdapter(ib).executions()
        assert ib.requested == 1
        assert len(records) == 2
        assert all(r.commission == D("-0.65") for r in records)

    def test_a_failed_refill_still_returns_what_is_locally_known(self) -> None:
        """A refill failure is not a result. Anything genuinely absent shows up
        downstream as a coverage gap, which is the honest answer."""
        from engine.options.adapters import IBKRExecutionReportAdapter

        pos = position()
        ib = FakeIBWithFills(both_legs_filled(pos.strategy_id), refill_raises=True)
        assert len(IBKRExecutionReportAdapter(ib).executions()) == 2

    def test_a_client_with_no_fill_reader_yields_nothing_rather_than_raising(
        self,
    ) -> None:
        from engine.options.adapters import IBKRExecutionReportAdapter

        assert IBKRExecutionReportAdapter(object()).executions() == ()


class ExplodingBroker:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("options-mark connected when it should not have")


class TestTheOperatorCommand:
    def _seed(self, state_dir: Any) -> UUID:
        store = PositionStore(state_dir / "positions.jsonl")
        pos = position()
        store.record_open_submitted(pos.intent, at=NOW, buying_power_reserved=D("82"))
        store.record_open_filled(
            pos.strategy_id,
            at=NOW,
            filled_credit=ENTRY_CREDIT,
            filled_quantity=D("1"),
            order_id=OPEN_ORDER_ID,
            perm_id=OPEN_PERM_ID,
        )
        return pos.strategy_id

    def _run(self, state_dir: Any, extra: list[str], broker: Any = ExplodingBroker) -> int:
        """Drive the handler directly, exactly as ``test_cli.run_trade`` does.

        ``broker_factory`` is the injection seam every options handler carries,
        and ``ExplodingBroker`` is the default here on purpose: a marking pass
        that opens a socket when it was told not to fails loudly rather than
        quietly succeeding.
        """
        from engine import cli

        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "--account",
                "DU1234567",
                "--state-dir",
                str(state_dir),
                "--no-alerts",
                "options-mark",
                *extra,
            ]
        )
        return cli.cmd_options_mark(args, broker_factory=broker)

    def test_no_connect_fails_closed_rather_than_printing_a_number(
        self, state_dir: Any, capsys: Any
    ) -> None:
        """Without quotes there is no mark, and no-mark is a non-zero exit."""
        from engine.errors import EXIT_ERROR

        self._seed(state_dir)
        assert self._run(state_dir, ["--no-connect"]) == EXIT_ERROR
        printed = capsys.readouterr()
        assert "UNAVAILABLE" in printed.out
        assert "MARK_NO_SNAPSHOT" in printed.out

    def test_a_halted_engine_never_connects_to_mark(self, state_dir: Any) -> None:
        """Read-only is not a reason to skip the kill switch."""
        self._seed(state_dir)
        (state_dir / "HALT").write_text("stop", encoding="utf-8")
        with pytest.raises(Exception) as caught:
            self._run(state_dir, [])
        assert caught.value.__class__.__name__ == "HaltedError"

    def test_an_empty_book_is_not_an_error(self, state_dir: Any) -> None:
        from engine.errors import EXIT_OK

        assert self._run(state_dir, ["--no-connect"]) == EXIT_OK

    def test_an_unknown_strategy_id_is_reported_not_silently_empty(
        self, state_dir: Any
    ) -> None:
        from engine.errors import EXIT_ERROR

        self._seed(state_dir)
        assert (
            self._run(state_dir, ["--no-connect", "--strategy-id", str(uuid4())])
            == EXIT_ERROR
        )

    def test_a_malformed_strategy_id_is_a_usage_error(self, state_dir: Any) -> None:
        from engine.errors import EXIT_USAGE

        self._seed(state_dir)
        assert (
            self._run(state_dir, ["--no-connect", "--strategy-id", "not-a-uuid"])
            == EXIT_USAGE
        )

    def test_the_mark_is_journalled(self, state_dir: Any) -> None:
        """The record, not just the print. An operator surface that leaves no
        trace cannot be audited after the fact."""
        import json

        from engine.config import EngineConfig

        self._seed(state_dir)
        self._run(state_dir, ["--no-connect"])
        journal_path = EngineConfig.from_env(
            account_id="DU1234567", state_dir=state_dir
        ).journal_path
        lines = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        marks = [row for row in lines if row.get("event") == "position_mark"]
        assert marks
        assert marks[-1]["state"] == "UNAVAILABLE"


# ===========================================================================
# The lane sends nothing
# ===========================================================================


class TestThisLaneSendsNothing:
    def test_the_marking_module_has_no_broker_write(self) -> None:
        """Asserted structurally rather than trusted. The package-wide AST proof
        lives in ``test_options_no_transmit.py``; this is the module-local
        statement that a proposal is not an order."""
        import ast
        import inspect

        from engine.options import marking

        tree = ast.parse(inspect.getsource(marking))
        forbidden = {"placeOrder", "cancelOrder", "cancelOrderAsync", "submit", "send"}
        names = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert not names & forbidden

    def test_the_cli_command_cannot_be_armed(self) -> None:
        """There is no ``--arm`` on this command and no path that could use one.

        Also pinned package-wide in ``test_options_no_transmit.py``; asserted
        here too because that file's parametrized list is easy to forget to
        extend when a command is added.
        """
        from engine.cli import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["options-mark", "--arm"])

    def test_a_proposal_carries_no_authorization(self) -> None:
        """It is an intent and a size, not permission. The authorization type it
        would need lives behind the single door in ``transmit`` and cannot be
        minted here."""
        pos = position()
        report = mark(pos, commission=evidence_for(pos))
        assert report.close_proposal is not None
        fields = set(vars(report.close_proposal))
        assert not fields & {"authorization", "token", "armed", "authorized"}


class TestTheTransmittingExitIsSizedToWhatIsHeld:
    """The send path, not the proposal. Ledger C21 by a different road.

    ``lifecycle.decide_management_action`` holds a position while it is
    CLOSING, so a second close cannot double the order -- and that guard is
    why this looked safe. But a ``CLOSE_FAILED`` after a partial fill returns
    the position to OPEN, where it is re-decided from scratch. At that point
    ``manageable_quantity`` still reports the *whole original fill*, because it
    subtracts nothing that the close already retired.

    Three filled, two already closed, and the exit is sized three. Two of those
    contracts are not held, so the "defensive" exit opens a naked short.

    The guard was added and the entire suite stayed green, which is the C12
    failure mode this repo has now hit five times. This test is the thing that
    makes it load-bearing: it fails the moment the sizing goes back to
    ``manageable_quantity``.
    """

    def _reopened_after_partial_close(self, tmp_path: Any) -> Any:
        store = PositionStore(tmp_path / "positions.jsonl")
        pos = position(quantity=3)
        store.record_open_submitted(pos.intent, at=NOW, buying_power_reserved=D("246"))
        store.record_open_filled(
            pos.strategy_id, at=NOW, filled_credit=ENTRY_CREDIT, filled_quantity=D("3")
        )
        store.record_close_submitted(pos.strategy_id, at=NOW, target_debit=D("0.09"))
        store.record_partial_fill(
            pos.strategy_id, at=NOW, filled_quantity=D("2"), closing=True
        )
        store.record_close_failed(
            pos.strategy_id, at=NOW, reason="the closing order was cancelled"
        )
        reloaded = store.get(pos.strategy_id)
        assert reloaded is not None
        return reloaded

    def test_only_the_unclosed_remainder_may_be_sold(self, tmp_path: Any) -> None:
        held = self._reopened_after_partial_close(tmp_path)

        # The premise: the position is OPEN again, so it WILL be re-decided.
        assert held.state is PositionState.OPEN
        # And the older number still says three.
        assert held.manageable_quantity == 3

        assert confirmed_remaining_quantity(held) == 1, (
            "three filled minus two closed is one; sizing an exit off "
            "manageable_quantity would sell two contracts nobody holds"
        )

    def test_the_two_numbers_genuinely_disagree_here(self, tmp_path: Any) -> None:
        """Otherwise the test above passes for the wrong reason.

        If the fixture ever stops producing a partial close, both numbers
        collapse to the same value and the assertion becomes vacuous -- exactly
        how ledger C21's own coverage passed while the defect was live.
        """
        held = self._reopened_after_partial_close(tmp_path)
        assert held.manageable_quantity != confirmed_remaining_quantity(held)
        assert held.close_filled_quantity == D("2")
