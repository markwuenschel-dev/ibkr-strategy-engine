"""The broker wrapper, driven entirely by a fake IB. No sockets are opened."""

from __future__ import annotations

import pytest

from engine.broker import (
    IB_UNSET,
    MARKET_DATA_DELAYED,
    MARKET_DATA_LIVE,
    Broker,
    _as_float,
)
from engine.config import EngineConfig
from engine.errors import ConnectionError_, EngineError
from engine.journal import OrderJournal
from engine.safety import BUY, SELL, OrderIntent
from fakes import FakeContract, FakeIB, FakeOrderState, FakePosition, FakeTicker, FakeTrade


def make(config: EngineConfig, journal: OrderJournal, **kwargs: object) -> Broker:
    return Broker(config, journal, ib=FakeIB(**kwargs))  # type: ignore[arg-type]


class TestAccountInterlock:
    def test_connecting_to_the_expected_account_succeeds(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        broker = make(config, journal, accounts=[config.account_id])
        assert broker.connect() == config.account_id

    def test_a_different_account_is_refused_and_disconnected(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        # The case this exists for: TWS reconfigured to serve a live account on
        # the paper port. The port gate cannot catch that; this must.
        broker = make(config, journal, accounts=["U7654321"])
        with pytest.raises(ConnectionError_) as caught:
            broker.connect()
        assert "U7654321" in str(caught.value)
        assert broker.ib.connected is False, "must not stay connected to an unexpected account"

    def test_no_accounts_at_all_is_refused(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        broker = make(config, journal, accounts=[])
        with pytest.raises(ConnectionError_):
            broker.connect()

    def test_a_connection_failure_is_wrapped_with_an_actionable_hint(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        broker = make(config, journal, connect_error=OSError("refused"))
        with pytest.raises(ConnectionError_) as caught:
            broker.connect()
        assert "Enable ActiveX and Socket Clients" in str(caught.value)

    def test_a_successful_connection_is_journalled(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        make(config, journal, accounts=[config.account_id]).connect()
        assert any(r["event"] == "connected" for r in journal.records())


class TestMarketData:
    def test_delayed_data_is_requested_and_labelled(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        broker = make(config, journal)
        broker.connect()
        quote = broker.quote("SPY")
        assert broker.ib.market_data_types == [MARKET_DATA_DELAYED]
        # A delayed price must never be presented as if it were live.
        assert quote.source == "delayed"
        assert "delayed" in quote.describe()

    def test_the_mid_price_is_used_when_both_sides_are_present(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        broker = make(config, journal, ticker=FakeTicker(bid=100.0, ask=102.0))
        broker.connect()
        assert broker.quote("SPY").price == 101.0

    def test_a_nan_price_reads_as_unavailable_not_as_a_number(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        # IBKR uses NaN for absent prices. A NaN reaching a notional check
        # compares false against every cap -- silently passing it.
        broker = make(config, journal, ticker=FakeTicker())
        broker.connect()
        assert broker.quote("SPY").price is None

    def test_the_subscription_is_cancelled(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        broker = make(config, journal)
        broker.connect()
        broker.quote("SPY")
        assert broker.ib.cancelled, "market data subscriptions must not be left open"

    def test_the_label_comes_from_the_provider_not_the_request(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        # The two were one field, holding the requested constant -- so a quote
        # could read "live" purely because live was asked for. If the provider
        # says something other than what was requested, the provider wins.
        ticker = FakeTicker(bid=1.0, ask=2.0, marketDataType=MARKET_DATA_LIVE)
        ticker.confirms_data_type = False
        broker = make(config, journal, ticker=ticker)
        broker.connect()
        quote = broker.quote("SPY")
        assert quote.requested_market_data_type == MARKET_DATA_DELAYED
        assert quote.reported_market_data_type == MARKET_DATA_LIVE
        assert quote.source == "live"

    def test_a_silent_provider_is_labelled_unconfirmed_not_assumed(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        # A real ib_async Ticker defaults marketDataType to 1, so a server that
        # never answered is indistinguishable from one that confirmed live.
        # Where the fake can express silence, the label must admit to it.
        ticker = FakeTicker(bid=1.0, ask=2.0)
        ticker.confirms_data_type = False
        broker = make(config, journal, ticker=ticker)
        broker.connect()
        quote = broker.quote("SPY")
        assert quote.reported_market_data_type is None
        assert quote.source == "delayed (requested, unconfirmed)"
        assert "unconfirmed" in quote.describe()


class TestPositions:
    def test_position_qty_finds_the_symbol(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        broker = make(
            config,
            journal,
            positions=[FakePosition(FakeContract("SPY"), 7.0, 100.0)],
        )
        broker.connect()
        assert broker.position_qty("spy") == 7

    def test_an_unheld_symbol_is_zero(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        broker = make(config, journal)
        broker.connect()
        assert broker.position_qty("AAPL") == 0


class TestPreview:
    def test_whatif_reports_margin_and_transmits_nothing(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        broker = make(config, journal)
        broker.connect()
        preview = broker.preview(OrderIntent("SPY", 1, BUY))
        assert preview.margin_impact == 100.0
        assert preview.commission == 1.0
        assert broker.ib.what_ifs, "whatIfOrder should have been called"
        assert broker.ib.placed == [], "preview must never place an order"

    def test_initial_margin_is_preferred_over_maintenance(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        broker = make(
            config,
            journal,
            order_state=FakeOrderState(initMarginChange=250.0, maintMarginChange=10.0),
        )
        broker.connect()
        assert broker.preview(OrderIntent("SPY", 1, BUY)).margin_impact == 250.0

    def test_a_rejected_whatif_is_a_broker_error_not_a_silent_unknown(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        # ib_async ends a rejected request by resolving it with its empty result
        # container, so the caller gets `[]` where an OrderState was promised.
        # Read field-by-field that degrades into "margin not reported", which
        # gate_margin refuses -- the right outcome reported as the wrong cause.
        broker = make(config, journal, order_state=[])
        broker.connect()
        with pytest.raises(EngineError) as caught:
            broker.preview(OrderIntent("SPY", 1, BUY))
        assert "no order state" in str(caught.value)

    def test_an_unset_ibkr_field_is_not_read_as_a_number(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        # IBKR sends DBL_MAX for a field that does not apply. It is finite, so
        # only an explicit screen keeps it out of the margin cap comparison.
        broker = make(
            config,
            journal,
            order_state=FakeOrderState(
                initMarginChange=IB_UNSET, maintMarginChange=IB_UNSET
            ),
        )
        broker.connect()
        assert broker.preview(OrderIntent("SPY", 1, BUY)).margin_impact is None


class TestPlace:
    def test_the_order_carries_the_configured_account(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        # If this session ever manages more than one account, the order still
        # cannot land anywhere but the configured one.
        broker = make(config, journal)
        broker.connect()
        broker.place(OrderIntent("SPY", 1, BUY))
        _contract, order = broker.ib.placed[0]
        assert order.account == config.account_id

    def test_a_fill_is_reported(self, config: EngineConfig, journal: OrderJournal) -> None:
        broker = make(config, journal)
        broker.connect()
        result = broker.place(OrderIntent("SPY", 1, BUY))
        assert result["status"] == "Filled"
        assert result["avg_fill_price"] == 100.0
        assert result["timed_out"] is False

    def test_an_order_that_never_settles_is_reported_as_timed_out(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        trade = FakeTrade(done=False)
        broker = make(config, journal, trade=trade)
        broker.connect()
        result = broker.place(OrderIntent("SPY", 1, BUY), timeout=1.0)
        assert result["timed_out"] is True

    def test_a_sell_is_transmitted_as_sell(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        broker = make(config, journal)
        broker.connect()
        broker.place(OrderIntent("SPY", 1, SELL))
        _contract, order = broker.ib.placed[0]
        assert order.action == "SELL"

    def test_the_order_names_its_time_in_force(
        self, config: EngineConfig, journal: OrderJournal
    ) -> None:
        # An unset TIF lets a TWS order preset supply one and announce it with
        # error 10349, which ib_async classifies as fatal and which kills an
        # in-flight whatIf. Naming DAY leaves the preset nothing to override.
        broker = make(config, journal)
        broker.connect()
        broker.place(OrderIntent("SPY", 1, BUY))
        _contract, order = broker.ib.placed[0]
        assert order.tif == "DAY"


class TestFloatCoercion:
    @pytest.mark.parametrize(
        "value",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            None,
            "abc",
            IB_UNSET,
            -IB_UNSET,
            str(IB_UNSET),
        ],
    )
    def test_unusable_values_become_none(self, value: object) -> None:
        assert _as_float(value) is None

    @pytest.mark.parametrize("value,expected", [(1, 1.0), ("2.5", 2.5), (0, 0.0)])
    def test_usable_values_convert(self, value: object, expected: float) -> None:
        assert _as_float(value) == expected
