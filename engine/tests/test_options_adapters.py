"""The market-data adapter's wait, and the strike window's shape.

Both properties here were defects found by *running* the engine against live
TWS on 2026-07-30 rather than by any test: two identical `options-run`
invocations, minutes apart on the same account, produced two different failures
-- one candidate that lost greeks on five legs, and one that could not find a
protective strike at all. The unit suite was green for both.

What the two defects had in common is that each substituted something cheap and
positional for something real: a fixed sleep standing in for "the data arrived",
and the middle of the strike ladder standing in for spot.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from engine.errors import InvalidStrategyError
from engine.options.adapters import IBKRLiveMarketDataAdapter
from engine.options.chain import QualifiedOption, narrow_strikes
from engine.options.selection import DeltaCandidate, OptionRight, select_vertical

D = Decimal

UNDERLYING_CON_ID = 9000
LEG_CON_IDS = (101, 102, 103)


# ---------------------------------------------------------------------------
# A fake TWS that delivers greeks late, and one leg at a time
# ---------------------------------------------------------------------------


class _Greeks:
    def __init__(self, delta: float) -> None:
        self.delta = delta
        self.gamma = 0.01
        self.vega = 0.10
        self.theta = -0.05
        self.impliedVol = 0.20
        self.optPrice = 1.0
        self.undPrice = 500.0


class _Contract:
    def __init__(self, con_id: int) -> None:
        self.conId = con_id
        self.symbol = "SPY"
        self.exchange = "SMART"


class _Ticker:
    def __init__(self, contract: _Contract) -> None:
        self.contract = contract
        self.modelGreeks: Any = None
        self.time = None


class _Wrapper:
    """Just enough of ib_async's wrapper for ``CallbackRecorder`` to attach."""

    def __init__(self) -> None:
        self.reqId2Ticker: dict[int, _Ticker] = {}

    def marketDataType(self, req_id: int, market_data_id: int) -> None:
        return None

    def tickOptionComputation(self, req_id: int, tick_type: int, *args: Any) -> None:
        return None


class FakeIB:
    """Delivers each leg's greeks after its own number of poll iterations.

    ``greek_after`` is the point of the fake: staggering arrival is what a real
    chain subscription does, and a single harvest after a fixed sleep captures
    only whatever happened to land by then.
    """

    def __init__(
        self,
        *,
        greek_after: dict[int, int],
        data_type: int = 1,
    ) -> None:
        self.wrapper = _Wrapper()
        self.greek_after = greek_after
        self.data_type = data_type
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.generic_tick_lists: list[str] = []
        self.subscribe_order: list[int] = []
        self.cancelled: list[int] = []
        self._req_id = 0
        self._ticks = 0

    # -- the clock the adapter measures its deadline against ----------------
    def clock(self) -> float:
        return self.now

    # -- ib_async surface ---------------------------------------------------
    def qualifyContracts(self, *contracts: Any) -> list[Any]:
        out = []
        for contract in contracts:
            con_id = getattr(contract, "conId", 0) or UNDERLYING_CON_ID
            out.append(_Contract(int(con_id)))
        return out

    def reqMarketDataType(self, market_data_type: int) -> None:
        return None

    def reqMktData(
        self, contract: Any, generic_tick_list: str, snapshot: bool, regulatory: bool
    ) -> _Ticker:
        self._req_id += 1
        ticker = _Ticker(contract)
        self.wrapper.reqId2Ticker[self._req_id] = ticker
        self.generic_tick_lists.append(generic_tick_list)
        self.subscribe_order.append(int(contract.conId))
        # Every subscription reports its market-data type promptly; it is the
        # model computation that lags.
        self.wrapper.marketDataType(self._req_id, self.data_type)
        return ticker

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        self._ticks += 1
        for req_id, ticker in self.wrapper.reqId2Ticker.items():
            con_id = int(ticker.contract.conId)
            due = self.greek_after.get(con_id)
            if due is not None and self._ticks >= due and ticker.modelGreeks is None:
                ticker.modelGreeks = _Greeks(delta=-0.30)
                self.wrapper.tickOptionComputation(req_id, 13)

    def cancelMktData(self, contract: Any) -> None:
        self.cancelled.append(int(contract.conId))


def _adapter(ib: FakeIB, **kwargs: Any) -> IBKRLiveMarketDataAdapter:
    return IBKRLiveMarketDataAdapter(
        ib, clock=ib.clock, underlying_lead_seconds=0.0, **kwargs
    )


def _snapshot(ib: FakeIB, **kwargs: Any) -> Any:
    return _adapter(ib, **kwargs).strategy_quotes(
        underlying_symbol="SPY", con_ids=LEG_CON_IDS
    )


# ---------------------------------------------------------------------------
# The wait
# ---------------------------------------------------------------------------


class TestTheWaitIsOnDataNotOnAClock:
    def test_a_leg_whose_greeks_arrive_late_is_still_captured(self) -> None:
        """The defect, directly: one slow leg used to be dropped silently.

        Under the old fixed 6s sleep with a single harvest, leg 103 -- whose
        computation lands well after the others -- contributed no greeks, and
        the entitlement gate refused the whole candidate with
        OPTION_GREEKS_MISSING naming exactly that kind of straggler.
        """
        ib = FakeIB(greek_after={101: 1, 102: 2, 103: 20})
        snapshot = _snapshot(ib, settle_seconds=60.0, poll_seconds=1.0)

        with_greeks = [leg.con_id for leg in snapshot.legs if leg.greeks is not None]
        assert sorted(with_greeks) == sorted(LEG_CON_IDS)

    def test_it_exits_as_soon_as_every_leg_has_greeks(self) -> None:
        """A ceiling, not a duration -- otherwise nobody dares raise it."""
        ib = FakeIB(greek_after={101: 1, 102: 1, 103: 2})
        _snapshot(ib, settle_seconds=60.0, poll_seconds=1.0)

        # Two polls to collect all three, and it must not have burned the
        # remaining 58 seconds of its allowance.
        assert ib.now - 1000.0 < 10.0

    def test_the_deadline_still_bounds_a_leg_that_never_arrives(self) -> None:
        """Patience is not the same as hanging."""
        ib = FakeIB(greek_after={101: 1, 102: 1})  # 103 never computes
        snapshot = _snapshot(ib, settle_seconds=10.0, poll_seconds=1.0)

        assert ib.now - 1000.0 <= 11.0
        missing = [leg.con_id for leg in snapshot.legs if leg.greeks is None]
        assert missing == [103]

    def test_the_underlying_is_subscribed_before_the_legs(self) -> None:
        """IBKR computes option greeks from the underlying price.

        Subscribing the chain in one burst asks for computations whose input has
        not arrived. The probe -- which works -- gives the underlying a head
        start, and this is the adapter matching it.
        """
        ib = FakeIB(greek_after={101: 1, 102: 1, 103: 1})
        _snapshot(ib, settle_seconds=10.0, poll_seconds=1.0)

        assert ib.subscribe_order[0] == UNDERLYING_CON_ID
        assert set(ib.subscribe_order[1:]) == set(LEG_CON_IDS)

    def test_it_requests_the_default_tick_list(self) -> None:
        """Generic tick 106 is implied volatility, not model greeks.

        The adapter used to pass "106" under a comment claiming IBKR sends no
        model computation without it. ``modelGreeks`` comes from
        tickOptionComputation on a bare request -- which is what the probe asks
        for, and the probe is the path that reliably gets greeks.
        """
        ib = FakeIB(greek_after={101: 1, 102: 1, 103: 1})
        _snapshot(ib, settle_seconds=10.0, poll_seconds=1.0)

        assert set(ib.generic_tick_lists) == {""}

    def test_greeks_on_the_ticker_are_used_when_the_recorder_missed_them(self) -> None:
        """The reqId mapping is a second thing that can fail independently.

        ib_async writes the computation onto the ticker regardless, so falling
        back to it turns a mapping miss from a lost leg into a non-event.
        """
        ib = FakeIB(greek_after={101: 1, 102: 1, 103: 1})
        # Silence the recorder's hook so nothing reaches ``latest_greeks``,
        # leaving the ticker as the only source.
        ib.wrapper.tickOptionComputation = lambda *args, **kwargs: None

        snapshot = _snapshot(ib, settle_seconds=10.0, poll_seconds=1.0)

        with_greeks = [leg.con_id for leg in snapshot.legs if leg.greeks is not None]
        assert sorted(with_greeks) == sorted(LEG_CON_IDS)

    def test_every_subscription_is_cancelled(self) -> None:
        ib = FakeIB(greek_after={101: 1, 102: 1, 103: 1})
        _snapshot(ib, settle_seconds=10.0, poll_seconds=1.0)

        assert set(ib.cancelled) == {UNDERLYING_CON_ID, *LEG_CON_IDS}


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


#: A ladder deliberately not centred on spot: 600..900, spot at 737. Its
#: positional median is 750, which is 13 strikes away from the money.
SKEWED_LADDER = [D(strike) for strike in range(600, 901)]
SPOT = D("737")


class TestTheStrikeWindowIsCentredOnSpot:
    def test_a_reference_price_beats_the_positional_median(self) -> None:
        by_spot = narrow_strikes(SKEWED_LADDER, reference_price=SPOT, width=24)
        positional = narrow_strikes(SKEWED_LADDER, reference_price=None, width=24)

        assert SPOT in by_spot
        assert by_spot != positional
        # The fallback centres on 750 and its floor sits above the strikes a
        # 737 spot would actually sell.
        assert min(positional) > min(by_spot)

    def test_a_put_window_reaches_below_spot_by_the_full_width(self) -> None:
        """The failure this fixes is structural, not cosmetic.

        A symmetric window spends half its budget above spot on strikes a put
        spread will never select, so it reaches down only half as far. On a
        1-point ladder that put the 0.30-delta short strike on the window floor
        with no protective strike beneath it -- "no strike pair with a usable
        delta and a protective leg was available".
        """
        window = narrow_strikes(SKEWED_LADDER, reference_price=SPOT, width=24, right="P")

        assert min(window) <= SPOT - 24
        assert max(window) < SPOT + 12  # only a cushion above

    def test_a_call_window_is_the_mirror_image(self) -> None:
        window = narrow_strikes(SKEWED_LADDER, reference_price=SPOT, width=24, right="C")

        assert max(window) >= SPOT + 24
        assert min(window) > SPOT - 12

    def test_the_symmetric_default_is_unchanged(self) -> None:
        """The shadow scan relies on it, so the new argument is opt-in."""
        window = narrow_strikes(SKEWED_LADDER, reference_price=SPOT, width=24)

        assert len(window) == 25
        assert min(window) == SPOT - 12
        assert max(window) == SPOT + 12

    def test_an_empty_ladder_stays_empty(self) -> None:
        assert narrow_strikes([], reference_price=SPOT, width=24, right="P") == []


# ---------------------------------------------------------------------------
# The protective leg's width bound
# ---------------------------------------------------------------------------


def _contract(strike: Decimal, *, right: OptionRight) -> QualifiedOption:
    return QualifiedOption(
        con_id=int(strike),
        symbol="SPY",
        expiration=dt.date(2026, 9, 11),
        strike=strike,
        right=right.value,
        multiplier=100,
        exchange="SMART",
        trading_class="SPY",
    )


def _chain(
    deltas: dict[Decimal, Decimal], *, right: OptionRight
) -> list[DeltaCandidate]:
    """A chain with each strike's delta stated outright.

    Preferred over a formula wherever the test depends on *which* strike the
    delta target selects -- a generated ladder that stops being monotonic in the
    wing silently moves the short somewhere the test did not intend.
    """
    return [
        DeltaCandidate(contract=_contract(strike, right=right), delta=delta)
        for strike, delta in deltas.items()
    ]


def _candidates(strikes: list[Decimal], *, right: OptionRight) -> list[DeltaCandidate]:
    """A chain where |delta| falls as the strike goes further out of the money."""
    out = []
    for index, strike in enumerate(sorted(strikes, reverse=True)):
        contract = QualifiedOption(
            con_id=int(strike),
            symbol="SPY",
            expiration=dt.date(2026, 9, 11),
            strike=strike,
            right=right.value,
            multiplier=100,
            exchange="SMART",
            trading_class="SPY",
        )
        delta = D("0.50") - D("0.02") * index
        out.append(
            DeltaCandidate(
                contract=contract,
                delta=-delta if right is OptionRight.PUT else delta,
            )
        )
    return out


class TestTheProtectiveLegCannotBeArbitrarilyFar:
    #: The ladder observed live on 2026-07-30: one far outlier, then a dense
    #: band. A short at the bottom of the band has exactly one strike beneath it.
    SPARSE = [D("672")] + [D(s) for s in range(722, 751)]

    def test_a_hole_in_the_ladder_yields_no_structure(self) -> None:
        """50 wide against a 5-wide target is not a near miss.

        The delta target is chosen so the short lands on 722, the **bottom** of
        the dense band -- which is the only arrangement that puts the outlier in
        play. A short anywhere higher has ordinary strikes beneath it and never
        consults 672, so a test targeting the usual 0.30 delta would pass with
        the bound removed and prove nothing.

        Two downstream gates would also refuse this structure -- the
        defined-loss cap, and the missing two-sided market on a strike nobody
        quotes. Refusing where the width is chosen is what makes the reason
        legible.
        """
        # |delta| rises with the strike across the band, and the outlier is far
        # enough out to carry almost none -- so a 0.02 target lands squarely on
        # 722 and the only strike beneath it is 672.
        deltas = {D("672"): D("-0.005")}
        for strike in range(722, 751):
            deltas[D(strike)] = -(D("0.02") + (D(strike) - D("722")) * D("0.015"))

        selection = select_vertical(
            _chain(deltas, right=OptionRight.PUT),
            target_delta=D("0.02"),
            right=OptionRight.PUT,
            target_width=D("5"),
        )
        assert selection is None, "a 50-wide wing must not be accepted"

    def test_a_coarse_ladder_may_still_exceed_the_target(self) -> None:
        """The bound is spacing-aware, not a ratio.

        A chain listing strikes every 5 cannot do better than 5 against a 2-wide
        target, and refusing that would be refusing the whole chain for being
        coarse. One increment past the target is the worst a complete ladder can
        do, and it is allowed.
        """
        coarse = [D(s) for s in range(600, 801, 5)]
        selection = select_vertical(
            _candidates(coarse, right=OptionRight.PUT),
            target_delta=D("0.30"),
            right=OptionRight.PUT,
            target_width=D("2"),
        )
        assert selection is not None
        assert selection.width == D("5")

    def test_a_dense_ladder_still_hits_the_target_exactly(self) -> None:
        dense = [D(s) for s in range(700, 761)]
        selection = select_vertical(
            _candidates(dense, right=OptionRight.PUT),
            target_delta=D("0.30"),
            right=OptionRight.PUT,
            target_width=D("5"),
        )
        assert selection is not None
        assert selection.width == D("5")

    def test_an_explicit_bound_tighter_than_the_derived_one_is_honoured(self) -> None:
        """A ladder spaced 7 apart derives a bound of 5+7=12, which admits a
        7-wide. An explicit 6 must exclude it and yield no structure."""
        spaced = [D(s) for s in range(700, 841, 7)]
        candidates = _candidates(spaced, right=OptionRight.PUT)

        derived = select_vertical(
            candidates,
            target_delta=D("0.30"),
            right=OptionRight.PUT,
            target_width=D("5"),
        )
        assert derived is not None and derived.width == D("7")

        explicit = select_vertical(
            candidates,
            target_delta=D("0.30"),
            right=OptionRight.PUT,
            target_width=D("5"),
            maximum_width=D("6"),
        )
        assert explicit is None

    def test_an_unvalidated_bound_cannot_silently_disable_the_check(self) -> None:
        """``Decimal("Infinity")`` is an ordinary finite-looking Decimal.

        Passed as a bound it reads like a limit and enforces nothing, which is
        strictly worse than no bound at all -- and ``NaN`` turns the width
        comparison into an uncaught InvalidOperation inside the selector rather
        than a refusal at the boundary.
        """
        dense = [D(s) for s in range(700, 761)]
        candidates = _candidates(dense, right=OptionRight.PUT)
        for bad in (D("Infinity"), D("NaN"), D("-Infinity")):
            with pytest.raises(InvalidStrategyError):
                select_vertical(
                    candidates,
                    target_delta=D("0.30"),
                    right=OptionRight.PUT,
                    target_width=D("5"),
                    maximum_width=bad,
                )

    def test_a_bound_below_the_target_is_refused_as_contradictory(self) -> None:
        dense = [D(s) for s in range(700, 761)]
        with pytest.raises(InvalidStrategyError):
            select_vertical(
                _candidates(dense, right=OptionRight.PUT),
                target_delta=D("0.30"),
                right=OptionRight.PUT,
                target_width=D("5"),
                maximum_width=D("2"),
            )
