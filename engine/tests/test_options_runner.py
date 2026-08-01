"""One pass of the strategy, wired end to end: reconcile, manage, enter.

Every other options test proves one layer in isolation. This one proves the
*wiring*, which is where the properties that matter actually live:

* an unarmed run reaches the broker fake and still sends nothing;
* delayed data cannot transmit however good everything else looks;
* the intent is on disk **before** the order leaves the process;
* a failed send is recorded, not raised, so no phantom OPEN survives it;
* reconciliation disagreement stops entries and deliberately does **not** stop
  exits;
* the kill switch stops the whole pass before anything is evaluated.

``FakeIB.placeOrder`` is present and records every call. That is not decoration:
a fake missing the method would make every "nothing was transmitted" assertion
pass by ``AttributeError``, proving the opposite of what it claims.

The management tests deliberately leave the broker reporting no positions, so
reconciliation disagrees and the entry half of the pass is blocked. That keeps
``placed`` to exactly the exit under test -- and is itself the asymmetry
``TestReconciliationBlocksEntriesNotExits`` asserts head on.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

import reviewer
from engine.config import EngineConfig
from engine.errors import HaltedError
from engine.journal import OrderJournal
from engine.options.domain import (
    OptionLegIntent,
    OptionRight,
    OptionStrategyIntent,
    OrderAction,
    PriceEffect,
    StrategyAction,
    StrategyType,
)
from engine.options.lifecycle import ManagementAction
from engine.options.marketdata import (
    MarketDataProvenance,
    MarketDataType,
    OptionGreeks,
    OptionQuote,
    UnderlyingQuote,
)
from engine.options.policy import RiskPolicy
from engine.options.portfolio import PortfolioSnapshot, PositionExposure
from engine.options.ports import StrategyQuoteSnapshot
from engine.options.positions import (
    OpenPosition,
    PositionEvent,
    PositionState,
    PositionStore,
    ReconciliationOutcome,
)
from engine.options.runner import RunReport, mark_from_snapshot, run_once
from engine.options.selection import Bias
from engine.safety import SafetyGate

D = Decimal

NOW = dt.datetime(2026, 7, 29, 13, 0, tzinfo=dt.timezone.utc)
TODAY = NOW.date()

#: The underlying the whole file trades. Mid is 500.00, which is what the stress
#: check prices its adverse move against.
SPOT = D("500")
UNDERLYING_CON_ID = 9000

#: Listed strikes, 400..600 in fives. ``narrow_strikes`` takes the middle 25 of
#: them with no reference price, which is 440..560 -- and both legs below are
#: inside that window, so delta selection really has a chain to choose from.
STRIKES = [D(strike) for strike in range(400, 601, 5)]

#: The pair delta selection must land on: |delta| is exactly 0.30 at 450, which
#: is ``RiskPolicy.directional_target_delta``, and 445 is the listed strike
#: nearest ``target_width`` below it.
SHORT_STRIKE = D("450")
LONG_STRIKE = D("445")

#: Contract ids ARE the strikes. The market-data fake decodes a strike back out
#: of the con_id it is asked to quote, which is what lets one port serve both the
#: chain scan and the management mark without a lookup table.
SHORT_CON_ID = 450
LONG_CON_ID = 445

HALF_SPREAD = D("0.05")

#: What the broker's what-if reserves against a 5-wide spread, matching the live
#: figure recorded in ``engine.options.execution``.
BPR = D("500")


# ---------------------------------------------------------------------------
# The market the fakes present
# ---------------------------------------------------------------------------


def leg_mid(strike: Decimal) -> Decimal:
    """A put mid that rises with the strike, so the two legs differ.

    A flat price for every leg would make every structure's credit zero and
    every mark zero -- which reads as "at the profit target" the instant a
    position is opened.
    """
    return max(D("0.10"), (strike - D("400")) * D("0.30"))


def leg_delta(strike: Decimal) -> Decimal:
    """Put delta, negative, spread across the chain and exactly -0.30 at 450."""
    return -(D("0.50") + (strike - SPOT) * D("0.004"))


def provenance(
    generation: UUID, *, reported: MarketDataType, at: dt.datetime
) -> MarketDataProvenance:
    return MarketDataProvenance(
        requested_type=int(MarketDataType.LIVE),
        subscription_generation=generation,
        subscribed_at=at,
        reported_type=int(reported),
        callback_received=True,
        last_provider_event_at=at,
        last_local_receive_at=at,
    )


def option_quote(
    *,
    con_id: int,
    mid: Decimal,
    delta: Decimal | None = None,
    reported: MarketDataType = MarketDataType.LIVE,
    at: dt.datetime = NOW,
    open_interest: int | None = 5000,
    volume: int | None = 1000,
) -> OptionQuote:
    """One leg quote whose greeks carry the same generation as the quote.

    Same generation on purpose: ``require_uniform_live_provenance`` refuses
    greeks from a superseded subscription, so a mismatch here would refuse every
    candidate for a reason no test is about.

    Open interest and volume default comfortably above the liquidity floors
    (OI 500, volume 100): the liquidity gate treats unmeasured as insufficient,
    and a ``None`` here would refuse every candidate for a reason no test in
    this file is about. Tests about thin markets pass their own values.
    """
    generation = uuid4()
    return OptionQuote(
        con_id=con_id,
        provenance=provenance(generation, reported=reported, at=at),
        bid=mid - HALF_SPREAD,
        ask=mid + HALF_SPREAD,
        open_interest=open_interest,
        volume=volume,
        greeks=OptionGreeks(
            received_at=at, subscription_generation=generation, delta=delta
        ),
    )


def quote_snapshot(
    legs: tuple[OptionQuote, ...],
    *,
    symbol: str = "SPY",
    underlying_reported: MarketDataType = MarketDataType.LIVE,
    at: dt.datetime = NOW,
) -> StrategyQuoteSnapshot:
    underlying_generation = uuid4()
    return StrategyQuoteSnapshot(
        underlying=UnderlyingQuote(
            symbol=symbol,
            provenance=provenance(
                underlying_generation, reported=underlying_reported, at=at
            ),
            bid=SPOT - D("0.10"),
            ask=SPOT + D("0.10"),
        ),
        legs=legs,
        generations=(
            ("underlying", underlying_generation),
            *((str(q.con_id), q.provenance.subscription_generation) for q in legs),
        ),
    )


# ---------------------------------------------------------------------------
# Broker fakes
# ---------------------------------------------------------------------------


class _Contract:
    def __init__(self, *, con_id: int, strike: float, right: str) -> None:
        self.conId = con_id  # noqa: N815
        self.strike = strike
        self.right = right
        self.multiplier = "100"
        self.exchange = "SMART"
        self.tradingClass = "SPY"  # noqa: N815


class _Bar:
    def __init__(self, when: dt.date, close: float) -> None:
        self.date = when
        self.close = close


class _Chain:
    strikes = [float(strike) for strike in STRIKES]

    def __init__(self, today: dt.date) -> None:
        self.expirations = [
            (today + dt.timedelta(days=days)).strftime("%Y%m%d")
            for days in (14, 45, 80)
        ]


class _Detail:
    def __init__(self, strike: float) -> None:
        self.contract = _Contract(con_id=int(strike), strike=strike, right="P")


class _OrderState:
    initMarginChange = 500.0  # noqa: N815
    maintMarginChange = 500.0  # noqa: N815
    equityWithLoanChange = -2.0  # noqa: N815
    commission = 1.30
    warningText = ""  # noqa: N815


class _Order:
    orderId = 77  # noqa: N815


class _Status:
    def __init__(self, average: float) -> None:
        self.status = "Filled"
        self.filled = 1.0
        self.avgFillPrice = average  # noqa: N815


class _Trade:
    def __init__(self, average: float) -> None:
        self.order = _Order()
        self.orderStatus = _Status(average)  # noqa: N815

    def isDone(self) -> bool:  # noqa: N802
        return True


class FakeIB:
    """Everything ``run_once`` calls on ``broker.ib``, and nothing else.

    ``placeOrder`` records every call. A fake without it would make
    ``placed == []`` true by AttributeError, which proves nothing at all.
    """

    def __init__(
        self,
        *,
        today: dt.date = TODAY,
        fill_price: float | None = None,
        place_error: str | None = None,
    ) -> None:
        self.today = today
        self.fill_price = fill_price
        self.place_error = place_error
        self.placed: list[tuple[Any, Any]] = []
        self.slept = 0.0

    def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
        out: list[Any] = []
        for contract in contracts:
            if getattr(contract, "secType", "") == "STK":
                out.append(
                    _Contract(con_id=UNDERLYING_CON_ID, strike=0.0, right="")
                )
                continue
            strike = float(getattr(contract, "strike", 0.0))
            out.append(
                _Contract(
                    con_id=int(strike),
                    strike=strike,
                    right=getattr(contract, "right", "P"),
                )
            )
        return out

    def reqHistoricalData(self, *_a: Any, **_k: Any) -> list[Any]:  # noqa: N802
        """A year of implied volatility ending at the top of its own range, so
        IV Rank clears the 50 entry filter."""
        start = self.today - dt.timedelta(days=365)
        return [_Bar(start + dt.timedelta(days=i), 0.10 + 0.001 * i) for i in range(260)]

    def reqSecDefOptParams(self, *_a: Any, **_k: Any) -> list[Any]:  # noqa: N802
        return [_Chain(self.today)]

    def reqContractDetails(self, _contract: Any) -> list[Any]:  # noqa: N802
        return [_Detail(float(strike)) for strike in STRIKES]

    def whatIfOrder(self, _contract: Any, _order: Any) -> Any:  # noqa: N802
        return _OrderState()

    def placeOrder(self, contract: Any, order: Any) -> Any:  # noqa: N802
        """Fill at the submitted limit, sign and all.

        A credit is submitted as a BUY at a **negative** limit, so a credit fill
        comes back negative and a closing debit comes back positive. A fake that
        always answered with a positive price would never exercise the sign
        handling on either side of that.
        """
        self.placed.append((contract, order))
        if self.place_error is not None:
            raise RuntimeError(self.place_error)
        average = (
            float(order.lmtPrice) if self.fill_price is None else self.fill_price
        )
        return _Trade(average)

    def sleep(self, seconds: float) -> None:
        self.slept += seconds


class FakeBroker:
    def __init__(
        self,
        *,
        ib: FakeIB | None = None,
        positions: tuple[tuple[str, int, float], ...] = (),
    ) -> None:
        self.ib = ib if ib is not None else FakeIB()
        self._positions = positions

    def positions(self) -> tuple[tuple[str, int, float], ...]:
        return self._positions


class FakeMarketDataPort:
    """A coherent snapshot at whatever liveness and price level a test asks for.

    ``price_factor`` scales every leg mid together, which is how a position is
    walked to its profit target without touching the position itself.
    """

    def __init__(
        self,
        *,
        reported: MarketDataType = MarketDataType.LIVE,
        price_factor: Decimal = D("1"),
        at: dt.datetime = NOW,
    ) -> None:
        self.reported = reported
        self.price_factor = price_factor
        self.at = at
        self.calls: list[tuple[str, tuple[int, ...]]] = []

    def strategy_quotes(
        self,
        *,
        underlying_symbol: str,
        con_ids: Any,
        require_two_sided: bool = False,
    ) -> StrategyQuoteSnapshot:
        """``require_two_sided`` is accepted (the runner's management path sends
        it) and ignored: this fake always quotes both sides anyway."""
        con_ids = tuple(int(c) for c in con_ids)
        self.calls.append((underlying_symbol, con_ids))
        legs = tuple(
            option_quote(
                con_id=con_id,
                mid=leg_mid(D(con_id)) * self.price_factor,
                delta=leg_delta(D(con_id)),
                reported=self.reported,
                at=self.at,
            )
            for con_id in con_ids
        )
        return quote_snapshot(
            legs,
            symbol=underlying_symbol,
            underlying_reported=self.reported,
            at=self.at,
        )


class FakePortfolioPort:
    def __init__(
        self,
        *,
        net_liquidation: str = "1000000",
        positions: tuple[PositionExposure, ...] = (),
        reported: str | None = None,
    ) -> None:
        self.net_liquidation = D(net_liquidation)
        self.positions = positions
        self.reported = D(reported) if reported is not None else None

    def snapshot(self, *, as_of: dt.datetime) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            as_of=as_of,
            net_liquidation=self.net_liquidation,
            positions=self.positions,
            reported_buying_power_reserved=self.reported,
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def gate_for(tmp_path: Path, **overrides: Any) -> SafetyGate:
    settings: dict[str, Any] = {
        "account_id": "DU1234567",
        "port": 7497,
        "state_dir": tmp_path / "state",
        "symbol_allowlist": ("SPY", "AAPL"),
    }
    settings.update(overrides)
    config = EngineConfig(**settings)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return SafetyGate(config, OrderJournal(config.journal_path))


def store_for(tmp_path: Path) -> PositionStore:
    return PositionStore(tmp_path / "state" / "positions.jsonl")


def spread_intent(
    *,
    expiration: dt.date,
    credit: str = "1.50",
    underlying: str = "SPY",
    quantity: int = 1,
) -> OptionStrategyIntent:
    """The same 450/445 put credit spread the chain scan selects."""
    legs = (
        OptionLegIntent(
            con_id=SHORT_CON_ID,
            symbol=underlying,
            expiration=expiration,
            strike=SHORT_STRIKE,
            right=OptionRight.PUT,
            action=OrderAction.SELL,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
        OptionLegIntent(
            con_id=LONG_CON_ID,
            symbol=underlying,
            expiration=expiration,
            strike=LONG_STRIKE,
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
        expiration=expiration,
        limit_price=D(credit),
        price_effect=PriceEffect.CREDIT,
        maximum_loss_per_contract=(D("5") - D(credit)) * 100,
        configuration_version="test",
        created_at=NOW,
    )


def seed_open_position(
    store: PositionStore,
    *,
    dte: int,
    credit: str = "1.50",
    underlying: str = "SPY",
) -> OptionStrategyIntent:
    """Submit and fill one position, the same two writes the runner makes."""
    intent = spread_intent(
        expiration=TODAY + dt.timedelta(days=dte),
        credit=credit,
        underlying=underlying,
    )
    store.record_open_submitted(intent, at=NOW, buying_power_reserved=BPR)
    store.record_open_filled(intent.strategy_id, at=NOW, filled_credit=D(credit))
    return intent


def open_position(*, dte: int, credit: str = "1.50") -> OpenPosition:
    """An OPEN position with no store behind it, for the pure-function tests."""
    intent = spread_intent(expiration=TODAY + dt.timedelta(days=dte), credit=credit)
    return OpenPosition(
        strategy_id=intent.strategy_id,
        intent=intent,
        opened_at=NOW,
        state=PositionState.OPEN,
        buying_power_reserved=BPR,
        filled_credit=D(credit),
    )


def run_pass(
    broker: FakeBroker,
    gate: SafetyGate,
    store: PositionStore,
    *,
    armed: bool,
    market_data: Any = None,
    portfolio: Any = None,
    policy: RiskPolicy | None = None,
    verifier: Any = None,
    approval_context: Any = None,
    **extra: Any,
) -> RunReport:
    """One pass, with a real reviewed verifier gate unless one is supplied.

    The runner refuses an entry outright when no verifier is configured, so the
    default here is a real :class:`~reviewer.ReviewedGate` over a temp collab --
    a real request, a real reviewer, a real answer -- rather than an argument
    that waves the gate through. Its collab and its ledger hang off the same
    ``tmp_path`` the safety gate's state directory does, so two passes in one
    test share one ledger and the single-use rule really applies to them. A
    test that wants a second, independent approval passes its own ``verifier``.
    """
    if verifier is None:
        verifier = reviewer.approving_gate(gate.config.state_dir.parent / "verifier")
    if approval_context is None:
        approval_context = reviewer.approval_context()
    return run_once(
        broker,
        gate=gate,
        journal=gate.journal,
        store=store,
        policy=policy if policy is not None else RiskPolicy(),
        armed=armed,
        symbol="SPY",
        bias=Bias.BULLISH,
        market_data=market_data if market_data is not None else FakeMarketDataPort(),
        portfolio=portfolio if portfolio is not None else FakePortfolioPort(),
        now=NOW,
        today=TODAY,
        account="DU1234567",
        verifier=verifier,
        approval_context=approval_context,
        **extra,
    )


def event_names(store: PositionStore) -> list[str]:
    return [str(event.get("event")) for event in store.events()]


# ===========================================================================
# The fakes are good enough to reach the transmission decision
# ===========================================================================


class TestTheHappyPathIsReallyReached:
    """If these fail, every "nothing was transmitted" assertion below is
    passing because the pass died early rather than because a gate held."""

    def test_a_candidate_is_built_from_the_delta_selected_strikes(
        self, tmp_path: Path
    ) -> None:
        report = run_pass(
            FakeBroker(), gate_for(tmp_path), store_for(tmp_path), armed=False
        )
        assert report.candidate is not None, report.describe()
        strikes = sorted(leg.strike for leg in report.candidate.legs)
        assert strikes == [LONG_STRIKE, SHORT_STRIKE]

    def test_every_gate_other_than_arming_approves(self, tmp_path: Path) -> None:
        report = run_pass(
            FakeBroker(), gate_for(tmp_path), store_for(tmp_path), armed=False
        )
        assert report.risk is not None and report.risk.approved, report.describe()
        assert report.governor is not None and report.governor.approved
        assert report.iv_rank is not None and report.iv_rank.meets(D("50"))
        assert report.reconciliation is not None and report.reconciliation.agrees


# ===========================================================================
# Unarmed transmits nothing. The headline safety property.
# ===========================================================================


class TestUnarmedTransmitsNothing:
    def test_a_perfect_unarmed_run_still_places_no_order(self, tmp_path: Path) -> None:
        """With live data, a healthy book, high IV Rank and an empty position
        store, the ONLY thing standing between this pass and a live order is
        ``--arm``. If arming ever stops being checked, this is the test that
        notices before the broker does."""
        ib = FakeIB()
        report = run_pass(
            FakeBroker(ib=ib), gate_for(tmp_path), store_for(tmp_path), armed=False
        )

        assert report.entered is False
        assert ib.placed == []
        assert any("armed" in blocker for blocker in report.blockers), report.blockers

    def test_an_unarmed_run_writes_no_position(self, tmp_path: Path) -> None:
        """``record_open_submitted`` happens before the send, so an unarmed run
        that got as far as the store would leave an OPENING record for a spread
        that was never sent -- and the reconciler would then hunt for it."""
        store = store_for(tmp_path)
        run_pass(FakeBroker(), gate_for(tmp_path), store, armed=False)

        assert event_names(store) == []
        assert store.open_positions() == []


# ===========================================================================
# Armed and fully gated: exactly one order
# ===========================================================================


class TestArmedEntry:
    def test_one_order_is_transmitted_and_recorded_open(self, tmp_path: Path) -> None:
        """Exactly one. A pass that sends the same structure twice is the
        failure a retry loop or a duplicated call site would produce."""
        ib = FakeIB()
        store = store_for(tmp_path)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        assert len(ib.placed) == 1, report.describe()
        assert report.entered is True

        positions = store.open_positions()
        assert len(positions) == 1
        assert positions[0].state is PositionState.OPEN
        assert positions[0].filled_credit > 0

    def test_the_transmitted_order_is_the_candidate_that_was_gated(
        self, tmp_path: Path
    ) -> None:
        """An approval is for one specific structure. The order that goes must
        be that structure, not a re-derived one."""
        ib = FakeIB()
        store = store_for(tmp_path)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        assert report.candidate is not None
        _bag, order = ib.placed[0]
        assert order.orderRef == str(report.candidate.strategy_id)
        assert order.transmit is True
        assert store.open_positions()[0].strategy_id == report.candidate.strategy_id

    def test_a_negative_credit_fill_is_stored_as_a_positive_credit(
        self, tmp_path: Path
    ) -> None:
        """A credit is submitted as a BUY at a negative limit and fills negative.

        A screen that rejected the negative average price would hand the runner
        ``None``, which it reads as "did not fill" -- and it would then record
        OPEN_FAILED for a spread that is live in the market. That is exactly the
        unrecorded position the store exists to make impossible, arriving through
        the one field nobody thinks to check the sign on.
        """
        ib = FakeIB()
        store = store_for(tmp_path)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        _bag, order = ib.placed[0]
        assert order.lmtPrice == -1.5
        assert report.entered is True
        assert store.open_positions()[0].filled_credit == D("1.5")

    def test_a_fill_with_no_usable_price_becomes_an_uncertain_position(
        self, tmp_path: Path
    ) -> None:
        """A fill price of exactly zero is an unpopulated field, not a price --
        but the order still filled, so contracts are in the market.

        Three options, one defensible. Recording nothing is the
        unrecorded-position failure the store exists to prevent. Recording the
        limit price as the credit puts a number the broker never confirmed into
        every downstream profit-target calculation. So: a real position whose
        economics are unknown, which blocks new entries until reconciled.
        """
        ib = FakeIB(fill_price=0.0)
        store = store_for(tmp_path)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        assert len(ib.placed) == 1
        assert report.entered is False
        assert any("without a usable price" in blocker for blocker in report.blockers)
        # OPEN_PARTIAL sits between them because the lifecycle sink durably
        # records the fill *quantity* the moment it is observed, before anything
        # discovers the price is unusable. The contracts are real and the log
        # says so; only the credit is unknown.
        assert event_names(store) == [
            PositionEvent.OPEN_SUBMITTED.value,
            PositionEvent.OPEN_ACKNOWLEDGED.value,
            PositionEvent.OPEN_PARTIAL.value,
            PositionEvent.OPEN_UNCERTAIN.value,
        ]

        held = store.open_positions()
        assert len(held) == 1, "a filled order must leave a position on the book"
        assert held[0].is_uncertain is True
        assert held[0].is_live is True
        assert "no usable fill price" in (held[0].uncertainty or "")


# ===========================================================================
# Delayed data cannot transmit, armed or not
# ===========================================================================


class TestDelayedDataCannotTransmit:
    def test_delayed_quotes_block_an_armed_entry(self, tmp_path: Path) -> None:
        """Delayed data is good enough to build a candidate and price a stress
        scenario. It must never be good enough to send an order."""
        ib = FakeIB()
        report = run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store_for(tmp_path),
            armed=True,
            market_data=FakeMarketDataPort(reported=MarketDataType.DELAYED),
        )

        assert ib.placed == []
        assert report.entered is False
        assert "OPTIONS_REALTIME_DATA_REQUIRED" in report.refusal_codes

    def test_the_entitlement_gate_is_the_only_thing_refusing(
        self, tmp_path: Path
    ) -> None:
        """Without this, the test above could pass on the strength of some
        unrelated refusal and keep passing with the entitlement gate removed."""
        report = run_pass(
            FakeBroker(),
            gate_for(tmp_path),
            store_for(tmp_path),
            armed=True,
            market_data=FakeMarketDataPort(reported=MarketDataType.DELAYED),
        )
        assert report.risk is not None
        assert report.risk.reason_codes == ("OPTIONS_REALTIME_DATA_REQUIRED",)
        assert report.governor is not None and report.governor.approved


# ===========================================================================
# Record before transmit
# ===========================================================================


class TestRecordBeforeTransmit:
    def test_open_submitted_is_written_before_open_filled(
        self, tmp_path: Path
    ) -> None:
        """The crash-safety property, asserted on the log's actual order.

        A crash between the two leaves an OPENING record the reconciler resolves
        against the broker. The reverse order leaves a live spread nothing knows
        about, and there is nothing to resolve it against.
        """
        ib = FakeIB()
        store = store_for(tmp_path)
        run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        names = event_names(store)
        # OPEN_ACKNOWLEDGED now sits between the two: the broker identifiers are
        # persisted the moment they are known, so a restart mid-flight can match
        # the store to a live order instead of guessing whether anything was sent.
        assert names == [
            PositionEvent.OPEN_SUBMITTED.value,
            PositionEvent.OPEN_ACKNOWLEDGED.value,
            PositionEvent.OPEN_FILLED.value,
        ]
        assert names.index(PositionEvent.OPEN_SUBMITTED.value) < names.index(
            PositionEvent.OPEN_FILLED.value
        )
        assert names.index(PositionEvent.OPEN_ACKNOWLEDGED.value) < names.index(
            PositionEvent.OPEN_FILLED.value
        )

    def test_a_close_is_submitted_before_it_is_filled(self, tmp_path: Path) -> None:
        """The same ordering on the exit half, which has its own two writes."""
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=10)
        run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        names = event_names(store)
        # CLOSE_ACKNOWLEDGED sits between them for the same reason it does on the
        # open: the broker identifiers are persisted as soon as they exist, so a
        # restart mid-exit can match the store to a live order.
        assert names[-3:] == [
            PositionEvent.CLOSE_SUBMITTED.value,
            PositionEvent.CLOSE_ACKNOWLEDGED.value,
            PositionEvent.CLOSE_FILLED.value,
        ]
        assert names.index(PositionEvent.CLOSE_SUBMITTED.value) < names.index(
            PositionEvent.CLOSE_FILLED.value
        )


# ===========================================================================
# A failed send is recorded, not raised
# ===========================================================================


class TestAFailedSendIsRecorded:
    def test_the_pass_reports_the_failure_and_leaves_no_phantom_position(
        self, tmp_path: Path
    ) -> None:
        """A send that raises after the OPENING record must not leave that record
        standing. An OPEN position the broker never accepted would be managed,
        exited, and counted against every concentration cap forever."""
        ib = FakeIB(place_error="socket closed mid-send")
        store = store_for(tmp_path)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        assert isinstance(report, RunReport)
        assert report.entered is False
        assert any("socket closed mid-send" in error for error in report.errors), (
            report.errors
        )
        assert PositionEvent.OPEN_FAILED.value in event_names(store)
        assert store.open_positions() == []

    def test_a_failed_close_leaves_the_position_open(self, tmp_path: Path) -> None:
        """CLOSE_FAILED returns the position to OPEN, because the close did not
        happen and the position is still live and still needs managing."""
        ib = FakeIB(place_error="TWS rejected the combo")
        store = store_for(tmp_path)
        seed_open_position(store, dte=10)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        assert any("close failed" in error for error in report.errors), report.errors
        assert PositionEvent.CLOSE_FAILED.value in event_names(store)
        positions = store.open_positions()
        assert len(positions) == 1
        assert positions[0].state is PositionState.OPEN


# ===========================================================================
# Management
# ===========================================================================


class TestManagement:
    def test_a_position_inside_the_dte_window_is_closed(self, tmp_path: Path) -> None:
        """The 21-DTE rule needs only a calendar, and it produces a real order."""
        ib = FakeIB()
        store = store_for(tmp_path)
        intent = seed_open_position(store, dte=10)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        actions = [decision.action for decision in report.decisions]
        assert actions == [ManagementAction.CLOSE_DTE], report.describe()
        assert report.decisions[0].position_id == intent.strategy_id
        assert len(ib.placed) == 1
        assert [t.action for t in report.transmissions] == [StrategyAction.CLOSE]

    def test_the_dte_exit_is_priced_from_the_mark(self, tmp_path: Path) -> None:
        """A DTE exit carries no target debit, so it must be priced against the
        book. Sending it as a market order on a wide combo is the shortcut this
        pins shut."""
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=10)
        run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        _bag, order = ib.placed[0]
        # 15.00 short mid minus 13.50 long mid, paid as a debit.
        assert order.lmtPrice == 1.5
        assert order.orderType == "LMT"

    def test_a_healthy_position_outside_the_window_holds(self, tmp_path: Path) -> None:
        """40 DTE with the mark far above the target: nothing to do, and nothing
        transmitted. A management pass that acts here is one that closes every
        position it ever opens."""
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=40)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        actions = [decision.action for decision in report.decisions]
        assert actions == [ManagementAction.HOLD], report.describe()
        assert report.decisions[0].reason_code == "LIFECYCLE_PROFIT_TARGET_NOT_REACHED"
        assert ib.placed == []
        assert store.open_positions()[0].state is PositionState.OPEN

    def test_the_profit_target_closes_at_the_computed_limit(
        self, tmp_path: Path
    ) -> None:
        """Half the 1.50 credit is a 0.75 buy-back. At exactly the target the
        rule must fire -- ``<=``, not ``<``."""
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=40, credit="1.50")
        report = run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            market_data=FakeMarketDataPort(price_factor=D("0.5")),
        )

        actions = [decision.action for decision in report.decisions]
        assert actions == [ManagementAction.CLOSE_PROFIT_TARGET], report.describe()
        assert report.decisions[0].target_debit == D("0.750")
        assert len(ib.placed) == 1
        _bag, order = ib.placed[0]
        assert order.lmtPrice == 0.75

    def test_an_unarmed_run_manages_nothing_into_the_market(
        self, tmp_path: Path
    ) -> None:
        """The exit path is exempt from the governor and the daily cap, but not
        from arming."""
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=10)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=False)

        assert ib.placed == []
        assert any("exit refused" in blocker for blocker in report.blockers), (
            report.blockers
        )
        assert store.open_positions()[0].state is PositionState.OPEN


# ===========================================================================
# Reconciliation blocks entries and deliberately not exits
# ===========================================================================


class TestReconciliationBlocksEntriesNotExits:
    def test_a_disagreeing_book_refuses_new_risk(self, tmp_path: Path) -> None:
        """The store holds a SPY spread the broker does not report. The engine
        does not know the book, so it must not add to it."""
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=40)
        report = run_pass(
            FakeBroker(ib=ib, positions=()), gate_for(tmp_path), store, armed=True
        )

        assert report.reconciliation is not None
        assert report.reconciliation.agrees is False
        assert report.reconciliation.missing_at_broker != ()
        assert report.entered is False
        assert report.candidate is None
        assert any("disagree" in blocker for blocker in report.blockers), (
            report.blockers
        )

    def test_management_still_runs_while_the_book_disagrees(
        self, tmp_path: Path
    ) -> None:
        """The disagreement may be exactly the position that needs closing. A
        reconciler that locked the exits would turn a bookkeeping problem into a
        market problem."""
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=40)
        report = run_pass(
            FakeBroker(ib=ib, positions=()), gate_for(tmp_path), store, armed=True
        )

        assert report.reconciliation is not None and not report.reconciliation.agrees
        assert report.decisions != []

    def test_an_exit_transmits_while_the_book_disagrees(self, tmp_path: Path) -> None:
        """The asymmetry with teeth: the same disagreement that refused the entry
        above does not stop a due exit reaching the broker."""
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=10)
        report = run_pass(
            FakeBroker(ib=ib, positions=()), gate_for(tmp_path), store, armed=True
        )

        assert report.reconciliation is not None and not report.reconciliation.agrees
        assert report.entered is False
        assert len(ib.placed) == 1
        assert [t.action for t in report.transmissions] == [StrategyAction.CLOSE]

    def test_a_book_the_broker_agrees_with_does_not_block(
        self, tmp_path: Path
    ) -> None:
        """Proves the blocker above is really the reconciler and not the mere
        presence of an open position."""
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=40)
        report = run_pass(
            FakeBroker(ib=ib, positions=(("SPY", 1, 100.0),)),
            gate_for(tmp_path),
            store,
            armed=True,
        )

        assert report.reconciliation is not None and report.reconciliation.agrees
        assert report.candidate is not None


# ===========================================================================
# The kill switch
# ===========================================================================


class TestKillSwitch:
    def test_a_halted_engine_raises_and_sends_nothing(self, tmp_path: Path) -> None:
        """The one condition that reaches the caller as an exception rather than
        a line in a report: the operator said stop."""
        ib = FakeIB()
        gate = gate_for(tmp_path)
        gate.config.halt_file.write_text("stopped by hand", encoding="utf-8")

        with pytest.raises(HaltedError):
            run_pass(FakeBroker(ib=ib), gate, store_for(tmp_path), armed=True)

        assert ib.placed == []

    def test_the_kill_switch_stops_management_too(self, tmp_path: Path) -> None:
        """It is checked before reconciliation, so nothing at all runs -- not
        even the exits that every other refusal deliberately permits."""
        ib = FakeIB()
        gate = gate_for(tmp_path)
        store = store_for(tmp_path)
        seed_open_position(store, dte=10)
        gate.config.halt_file.write_text("stop", encoding="utf-8")

        with pytest.raises(HaltedError):
            run_pass(FakeBroker(ib=ib), gate, store, armed=True)

        assert ib.placed == []
        assert PositionEvent.CLOSE_SUBMITTED.value not in event_names(store)


# ===========================================================================
# An unanswered reconciliation is doubt, never permission
# ===========================================================================


class SilentBroker:
    """A broker that cannot answer, in the two ways a real one cannot.

    ``error`` raises out of ``positions()`` -- a dropped socket, a rejected
    request. ``None`` returns no data at all, which is what a query that failed
    inside the adapter looks like from here and is emphatically **not** the same
    as a flat account: a flat account answers with an empty sequence.

    Both are on the same fake because the defect they prove is the same one --
    the absence of an answer being read as an answer of "you hold nothing".
    """

    def __init__(
        self, *, ib: FakeIB | None = None, error: Exception | None = None
    ) -> None:
        self.ib = ib if ib is not None else FakeIB()
        self.error = error
        self.calls = 0

    def positions(self) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return None


def corrupt_the_store(store: PositionStore, strategy_id: Any) -> None:
    """Append a CLOSE_FILLED whose timestamp is naive, so the replay skips it.

    A skipped transition is the dangerous corruption, not a torn line: the
    position stays in its last good state, so the book *looks* readable while
    saying something the log does not. ``integrity_errors`` is what reports it.
    """
    line = '{"v": 1, "event": "CLOSE_FILLED", "at": "2026-07-29T14:00:00", '
    line += f'"strategy_id": "{strategy_id}", "closing_debit": "0.40"}}'
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


class TestOnlyAReconciledBookAuthorisesAnEntry:
    """The four outcomes, and which of them may open risk.

    The defect these close: the entry gate used to ask ``if
    report.reconciliation is not None and not ...agrees``. When the broker threw,
    the reconciler left ``reconciliation`` as ``None``, that condition was false,
    and the pass proceeded to transmit -- so a restart while a spread was held
    sent the same spread again. Absence of a disagreement was being read as
    evidence of agreement.

    Every test here asserts ``ib.placed == []``, not merely a blocker. A blocker
    beside a transmitted order would be the exact bug wearing a warning label.
    """

    def test_a_broker_that_raises_blocks_the_entry_and_transmits_nothing(
        self, tmp_path: Path
    ) -> None:
        ib = FakeIB()
        broker = SilentBroker(ib=ib, error=RuntimeError("socket closed"))
        report = run_pass(broker, gate_for(tmp_path), store_for(tmp_path), armed=True)

        assert broker.calls == 1
        assert report.reconciliation_outcome is ReconciliationOutcome.UNAVAILABLE
        assert report.reconciliation is None
        assert report.entered is False
        assert ib.placed == []
        assert "RUNNER_RECONCILIATION_UNAVAILABLE" in report.refusal_codes

    def test_a_broker_that_answers_with_no_data_blocks_the_entry(
        self, tmp_path: Path
    ) -> None:
        """The quieter half of the same defect. ``None`` back from the broker
        used to reach the reconciler, which read it as an empty account, agreed
        with an empty store and authorised an entry against a book nobody had
        actually checked."""
        ib = FakeIB()
        broker = SilentBroker(ib=ib)
        report = run_pass(broker, gate_for(tmp_path), store_for(tmp_path), armed=True)

        assert broker.calls == 1
        assert report.reconciliation_outcome is ReconciliationOutcome.UNAVAILABLE
        assert report.entered is False
        assert ib.placed == []
        assert "RUNNER_RECONCILIATION_UNAVAILABLE" in report.refusal_codes

    def test_a_broker_that_cannot_report_positions_at_all_blocks_the_entry(
        self, tmp_path: Path
    ) -> None:
        """A missing capability is still an unanswered question."""

        class NoPositionsBroker:
            def __init__(self) -> None:
                self.ib = FakeIB()

        broker = NoPositionsBroker()
        report = run_pass(broker, gate_for(tmp_path), store_for(tmp_path), armed=True)

        assert report.reconciliation_outcome is ReconciliationOutcome.UNAVAILABLE
        assert report.entered is False
        assert broker.ib.placed == []

    def test_an_unreplayable_store_blocks_the_entry_even_when_the_broker_answers(
        self, tmp_path: Path
    ) -> None:
        """CORRUPT, not DISAGREEMENT: the broker is fine, the log on disk is not,
        and the classification has to name the half the operator must go and fix.
        The position is at 40 DTE so nothing is due -- this isolates the store."""
        ib = FakeIB()
        store = store_for(tmp_path)
        intent = seed_open_position(store, dte=40)
        corrupt_the_store(store, intent.strategy_id)

        report = run_pass(
            FakeBroker(ib=ib, positions=(("SPY", 1, 100.0),)),
            gate_for(tmp_path),
            store,
            armed=True,
        )

        assert store.integrity_errors() != ()
        assert report.reconciliation_outcome is ReconciliationOutcome.CORRUPT
        assert report.entered is False
        assert ib.placed == []
        assert "RUNNER_RECONCILIATION_CORRUPT" in report.refusal_codes

    def test_an_unreplayable_store_blocks_even_when_the_broker_is_silent_too(
        self, tmp_path: Path
    ) -> None:
        """Both halves broken at once. Under the old gate this was the worst
        case: the throwing broker erased the reconciliation entirely, so the
        unreadable book never got a chance to refuse anything."""
        ib = FakeIB()
        store = store_for(tmp_path)
        intent = seed_open_position(store, dte=40)
        corrupt_the_store(store, intent.strategy_id)

        report = run_pass(
            SilentBroker(ib=ib, error=RuntimeError("socket closed")),
            gate_for(tmp_path),
            store,
            armed=True,
        )

        assert report.reconciliation_outcome is ReconciliationOutcome.CORRUPT
        assert report.entered is False
        assert ib.placed == []

    def test_a_genuine_disagreement_blocks_the_entry_and_transmits_nothing(
        self, tmp_path: Path
    ) -> None:
        """The store holds a 40 DTE SPY spread the broker does not report. At 40
        DTE nothing is due, so the only order this pass could send is the entry
        -- and an empty ``placed`` therefore means the entry, specifically."""
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=40)

        report = run_pass(
            FakeBroker(ib=ib, positions=()), gate_for(tmp_path), store, armed=True
        )

        assert report.reconciliation_outcome is ReconciliationOutcome.DISAGREEMENT
        assert report.entered is False
        assert ib.placed == []
        assert "RUNNER_RECONCILIATION_DISAGREEMENT" in report.refusal_codes

    def test_the_control_a_clean_book_still_transmits_an_entry(
        self, tmp_path: Path
    ) -> None:
        """Without this the five refusals above prove nothing: a gate that
        refuses everything refuses correctly by accident."""
        ib = FakeIB()
        report = run_pass(
            FakeBroker(ib=ib, positions=()),
            gate_for(tmp_path),
            store_for(tmp_path),
            armed=True,
        )

        assert report.reconciliation_outcome is ReconciliationOutcome.RECONCILED
        assert report.blockers == [], report.describe()
        assert report.entered is True
        assert len(ib.placed) == 1

    def test_the_default_outcome_is_the_refusing_one(self) -> None:
        """A report that never reached the reconciler has established nothing.
        Constructed fail-closed so no future early return can leak an entry."""
        report = RunReport(started_at=NOW, armed=True, symbol="SPY")

        assert report.reconciliation_outcome is ReconciliationOutcome.UNAVAILABLE
        assert report.reconciliation_outcome.may_open_new_risk is False

    def test_only_reconciled_may_open_new_risk(self) -> None:
        permitted = [o for o in ReconciliationOutcome if o.may_open_new_risk]
        assert permitted == [ReconciliationOutcome.RECONCILED]


class TestExitsStillRunWhenTheBookIsNotUnderstood:
    """The asymmetry, held under the new outcomes.

    Closing is what reduces risk, and the reason the book is not understood may
    be exactly the position that needs closing. A fix that blocked exits along
    with entries would trap a position, which is a worse failure than the
    duplicate send it set out to prevent.
    """

    def test_an_exit_transmits_while_the_broker_is_unreachable(
        self, tmp_path: Path
    ) -> None:
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=10)

        report = run_pass(
            SilentBroker(ib=ib, error=RuntimeError("socket closed")),
            gate_for(tmp_path),
            store,
            armed=True,
        )

        assert report.reconciliation_outcome is ReconciliationOutcome.UNAVAILABLE
        assert report.entered is False
        assert len(ib.placed) == 1
        assert [t.action for t in report.transmissions] == [StrategyAction.CLOSE]

    def test_an_exit_transmits_while_the_broker_returns_no_data(
        self, tmp_path: Path
    ) -> None:
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=10)

        report = run_pass(
            SilentBroker(ib=ib), gate_for(tmp_path), store, armed=True
        )

        assert report.reconciliation_outcome is ReconciliationOutcome.UNAVAILABLE
        assert [t.action for t in report.transmissions] == [StrategyAction.CLOSE]

    def test_an_exit_transmits_while_the_book_disagrees(self, tmp_path: Path) -> None:
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=10)

        report = run_pass(
            FakeBroker(ib=ib, positions=()), gate_for(tmp_path), store, armed=True
        )

        assert report.reconciliation_outcome is ReconciliationOutcome.DISAGREEMENT
        assert [t.action for t in report.transmissions] == [StrategyAction.CLOSE]

    def test_management_still_evaluates_every_position_when_reconciliation_fails(
        self, tmp_path: Path
    ) -> None:
        """Not just the due ones: the whole management pass has to run, or a
        position would go unevaluated for as long as the broker stayed silent."""
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=40)

        report = run_pass(
            SilentBroker(ib=ib, error=RuntimeError("socket closed")),
            gate_for(tmp_path),
            store,
            armed=True,
        )

        assert report.reconciliation_outcome is ReconciliationOutcome.UNAVAILABLE
        assert len(report.decisions) == 1
        assert report.decisions[0].action is ManagementAction.HOLD


# ===========================================================================
# mark_from_snapshot arithmetic
# ===========================================================================


class TestMarkFromSnapshot:
    def test_the_mark_is_short_mids_minus_long_mids(self) -> None:
        """Buying back what was sold costs its mid; selling what was bought
        returns its mid. Inverting the sign would report every winning position
        as a loser and take no profits at all."""
        position = open_position(dte=40)
        snapshot = quote_snapshot(
            (
                option_quote(con_id=SHORT_CON_ID, mid=D("2.00")),
                option_quote(con_id=LONG_CON_ID, mid=D("0.50")),
            )
        )
        mark = mark_from_snapshot(position, snapshot)

        assert mark is not None
        assert mark.debit_to_close == D("1.50")
        assert mark.as_of == NOW

    def test_a_missing_leg_quote_yields_no_mark(self) -> None:
        """A partial structure priced as if the missing leg were free would
        report a debit that is wrong in the direction of taking a profit."""
        position = open_position(dte=40)
        snapshot = quote_snapshot((option_quote(con_id=SHORT_CON_ID, mid=D("2.00")),))

        assert mark_from_snapshot(position, snapshot) is None

    def test_no_snapshot_yields_no_mark(self) -> None:
        """"No mark at all" is a distinct state from "a mark we may not act on",
        and the lifecycle rules branch on the difference."""
        assert mark_from_snapshot(open_position(dte=40), None) is None

    def test_a_live_structure_marks_live(self) -> None:
        position = open_position(dte=40)
        snapshot = quote_snapshot(
            (
                option_quote(con_id=SHORT_CON_ID, mid=D("2.00")),
                option_quote(con_id=LONG_CON_ID, mid=D("0.50")),
            )
        )
        mark = mark_from_snapshot(position, snapshot)

        assert mark is not None and mark.is_live is True

    def test_one_delayed_leg_makes_the_whole_mark_not_live(self) -> None:
        """Liveness is a property of the structure, not of the best leg in it."""
        position = open_position(dte=40)
        snapshot = quote_snapshot(
            (
                option_quote(
                    con_id=SHORT_CON_ID,
                    mid=D("2.00"),
                    reported=MarketDataType.DELAYED,
                ),
                option_quote(con_id=LONG_CON_ID, mid=D("0.50")),
            )
        )
        mark = mark_from_snapshot(position, snapshot)

        assert mark is not None and mark.is_live is False

    def test_a_delayed_underlying_makes_the_mark_not_live(self) -> None:
        """IBKR computes greeks from the underlying, so a delayed underlying
        beside live legs describes a different market. Reading that as live is
        the failure this prevents."""
        position = open_position(dte=40)
        snapshot = quote_snapshot(
            (
                option_quote(con_id=SHORT_CON_ID, mid=D("2.00")),
                option_quote(con_id=LONG_CON_ID, mid=D("0.50")),
            ),
            underlying_reported=MarketDataType.DELAYED,
        )
        mark = mark_from_snapshot(position, snapshot)

        assert mark is not None and mark.is_live is False


# ===========================================================================
# The packet's evidence section is complete
# ===========================================================================


class TestEntryEvidenceIsComplete:
    """The evidence section is what the reviewer judges freshness and
    eligibility from, and an absent field renders as MISSING -- which is
    grounds for UNAVAILABLE and blocks the open.

    The 2026-07-31 UNAVAILABLE (handoff 20260731T133434Z-786403) named four
    fields the runner silently dropped: ``market_data_provenance`` and
    ``quote_timestamps`` read attributes the snapshot never had, and the
    trailing None-filter pruned them without a trace; ``greek_timestamps`` and
    ``portfolio_exposure_after`` were never assembled at all. These tests pin
    every expected field present on a live pass, so the next wrong attribute
    path fails a named test instead of a live review.
    """

    def _captured_evidence(self, tmp_path: Path) -> tuple[Any, RunReport]:
        captured: list[Any] = []

        class CapturingGate(reviewer.ReviewedGate):
            def require(self, packet: Any, *, now: dt.datetime) -> Any:
                captured.append(packet)
                return super().require(packet, now=now)

        root = reviewer.collab_at(tmp_path / "verifier")
        verifier = CapturingGate(
            root=root,
            ledger=tmp_path / "verifier" / "state" / "verification",
            reviewer=reviewer.ScriptedReviewer(root=root),
        )
        report = run_pass(
            FakeBroker(),
            gate_for(tmp_path),
            store_for(tmp_path),
            armed=False,
            verifier=verifier,
        )
        assert captured, report.describe()
        return captured[-1], report

    def test_every_expected_evidence_field_reaches_the_packet(
        self, tmp_path: Path
    ) -> None:
        from engine.options.approval import VerificationPacket

        packet, report = self._captured_evidence(tmp_path)
        missing = [
            name
            for name in VerificationPacket.EXPECTED_EVIDENCE
            if packet.evidence.get(name) is None
        ]
        assert missing == [], f"evidence dropped {missing}\n{report.describe()}"
        assert "**MISSING**" not in packet.render()

    def test_provenance_and_timestamps_name_the_selected_legs(
        self, tmp_path: Path
    ) -> None:
        """Per-leg fields must cover the structure proposed -- underlying and
        both selected legs -- not the whole scanned chain window."""
        packet, _report = self._captured_evidence(tmp_path)
        for name in ("market_data_provenance", "quote_timestamps", "greek_timestamps"):
            value = str(packet.evidence[name])
            assert str(SHORT_CON_ID) in value, f"{name}: {value}"
            assert str(LONG_CON_ID) in value, f"{name}: {value}"
        assert "underlying" in str(packet.evidence["market_data_provenance"])
        assert "LIVE" in str(packet.evidence["market_data_provenance"])

    def test_the_filter_field_states_the_threshold_it_enforced(
        self, tmp_path: Path
    ) -> None:
        """"ENFORCED" without a number reads as the default. A proof run at a
        deliberately lower threshold must say so in the packet, or the reviewer
        is asked to approve under a false label."""
        packet, _report = self._captured_evidence(tmp_path)
        assert packet.evidence["iv_rank_filter"] == "ENFORCED at minimum 50"


# ===========================================================================
# The volatility-regime gate: shadow records, live gates
# ===========================================================================


class TestRegimeGate:
    """Shadow must be behavior-preserving; live must gate and scale sizing.

    The DEPRESSED cases use a policy whose boundaries sit above any possible
    IV Rank (rank is capped at 100), so the fake's rich history lands in the
    bottom tier without inventing a second market fixture.
    """

    UNREACHABLE = dict(
        low_minimum_iv_rank=Decimal("101"),
        medium_minimum_iv_rank=Decimal("102"),
        high_minimum_iv_rank=Decimal("103"),
    )

    def test_every_pass_records_a_regime_decision(self, tmp_path: Path) -> None:
        from engine.options.regime import VolatilityRegime

        report = run_pass(
            FakeBroker(), gate_for(tmp_path), store_for(tmp_path), armed=False
        )
        assert report.regime is not None
        assert report.regime.regime is VolatilityRegime.HIGH, report.regime.describe()
        assert report.to_record()["regime"]["reasons"]

    def test_shadow_mode_never_gates_on_the_regime(self, tmp_path: Path) -> None:
        """DEPRESSED in shadow: the decision says refuse, the pass does not."""
        from engine.options.regime import VolatilityRegime, VolatilityRegimePolicy

        report = run_pass(
            FakeBroker(),
            gate_for(tmp_path),
            store_for(tmp_path),
            armed=False,
            regime_policy=VolatilityRegimePolicy(**self.UNREACHABLE),
            regime_live=False,
        )
        assert report.regime is not None
        assert report.regime.regime is VolatilityRegime.DEPRESSED
        assert "OPTIONS_REGIME_DEPRESSED_REFUSED" not in report.refusal_codes
        assert report.candidate is not None, report.describe()

    def test_live_mode_refuses_depressed_with_the_named_code(
        self, tmp_path: Path
    ) -> None:
        from engine.options.regime import VolatilityRegimePolicy

        report = run_pass(
            FakeBroker(),
            gate_for(tmp_path),
            store_for(tmp_path),
            armed=False,
            regime_policy=VolatilityRegimePolicy(**self.UNREACHABLE),
            regime_live=True,
        )
        assert "OPTIONS_REGIME_DEPRESSED_REFUSED" in report.refusal_codes
        assert report.candidate is None, "a refused tier must not build a candidate"

    def test_live_mode_replaces_the_flat_iv_wall(self, tmp_path: Path) -> None:
        """HIGH tier in live mode: entry proceeds even with the old filter set
        impossibly high -- the wall is no longer consulted."""
        report = run_pass(
            FakeBroker(),
            gate_for(tmp_path),
            store_for(tmp_path),
            armed=False,
            regime_live=True,
            minimum_iv_rank=Decimal("99"),
        )
        assert report.regime is not None and report.regime.permits_entry
        assert not any("entry filter" in b for b in report.blockers), report.blockers

    def test_live_allocation_scales_the_sizing_budget(self, tmp_path: Path) -> None:
        """A vanishing allocation must reach size_position: the same market
        that builds a candidate at full allocation sizes to nothing at 1e-4,
        which proves the multiplier is wired into the build, not just recorded."""
        from engine.options.regime import VolatilityRegimePolicy

        full = run_pass(
            FakeBroker(),
            gate_for(tmp_path / "full"),
            store_for(tmp_path / "full"),
            armed=False,
            regime_live=True,
        )
        assert full.candidate is not None, full.describe()

        starved = run_pass(
            FakeBroker(),
            gate_for(tmp_path / "starved"),
            store_for(tmp_path / "starved"),
            armed=False,
            regime_policy=VolatilityRegimePolicy(
                high_allocation=Decimal("0.0001"),
                medium_allocation=Decimal("0.0001"),
                low_allocation=Decimal("0.0001"),
            ),
            regime_live=True,
        )
        assert starved.candidate is None, starved.describe()
