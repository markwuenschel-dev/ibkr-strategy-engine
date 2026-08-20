"""Unit coverage for the bulk entitlement prober.

No real ib_async connection: a fake IB stands in exactly at the boundary
:class:`engine.options.probe.CallbackRecorder` already patches, so these
tests exercise the same wrapper-callback path the live probe uses.
"""

from __future__ import annotations

import datetime as dt

from engine.options.entitlement_probe import (
    measure_catalog_entitlement,
    measure_symbol_entitlement,
)

TODAY = dt.date(2026, 8, 18)


class _FakeContract:
    def __init__(self, symbol: str, con_id: int, primary_exchange: str = "ARCA") -> None:
        self.symbol = symbol
        self.conId = con_id
        self.primaryExchange = primary_exchange


class _FakeEvent:
    """Minimal ``+=``/``-=`` event, matching ib_async's ``Event`` shape."""

    def __init__(self) -> None:
        self._handlers: list = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
        return self

    def fire(self, *args) -> None:
        for handler in list(self._handlers):
            handler(*args)


class _FakeWrapper:
    reqId2Ticker: dict = {}

    def marketDataType(self, req_id, market_data_id):  # noqa: N802
        return None

    def tickOptionComputation(self, req_id, tick_type, *args):  # noqa: N802
        return None


class _FakeIB:
    """Reports a fixed marketDataType for whichever contract is subscribed,
    on the next call to ``sleep`` -- simulating the callback arriving after
    one poll tick, the common case on a live connection."""

    def __init__(self, *, reported_type: int | None, qualifies: bool = True) -> None:
        self.wrapper = _FakeWrapper()
        self.errorEvent = _FakeEvent()
        self.reported_type = reported_type
        self.qualifies = qualifies
        self.cancelled: list = []
        self._pending_con_id: int | None = None
        self._req_id = 0

    def qualifyContracts(self, contract):
        if not self.qualifies:
            return []
        return [_FakeContract(contract.symbol, con_id=hash(contract.symbol) % 100000)]

    def reqMarketDataType(self, requested_type) -> None:
        pass

    def reqMktData(self, contract, *args) -> None:
        self._pending_con_id = contract.conId
        self._req_id += 1
        self.wrapper.reqId2Ticker = {
            self._req_id: type("T", (), {"contract": contract})()
        }

    def cancelMktData(self, contract) -> None:
        self.cancelled.append(contract.conId)

    def sleep(self, seconds: float) -> None:
        if self.reported_type is not None and self._pending_con_id is not None:
            self.wrapper.marketDataType(self._req_id, self.reported_type)
            self._pending_con_id = None  # deliver once


class TestMeasureSymbolEntitlement:
    def test_live_callback_is_a_hard_pass(self) -> None:
        ib = _FakeIB(reported_type=1)  # MarketDataType.LIVE

        result = measure_symbol_entitlement(ib, "spy", now=TODAY)

        assert result.entry_allowed is True
        assert result.readiness == "VERIFIED"
        assert result.reported_types == (1,)
        assert result.listing_venue == "ARCA"
        assert "live type-1" in result.reason

    def test_delayed_callback_is_a_hard_denial(self) -> None:
        ib = _FakeIB(reported_type=3)  # MarketDataType.DELAYED

        result = measure_symbol_entitlement(ib, "AAPL", now=TODAY)

        assert result.entry_allowed is False
        assert result.readiness == "UNVERIFIED"
        assert result.reported_types == (3,)
        assert "DELAYED" in result.reason

    def test_no_callback_is_inconclusive_not_a_denial(self) -> None:
        ib = _FakeIB(reported_type=None)

        result = measure_symbol_entitlement(
            ib, "XYZ", now=TODAY, timeout_seconds=0.5, poll_seconds=0.25
        )

        assert result.entry_allowed is None
        assert "no marketDataType callback" in result.reason

    def test_broker_error_is_captured_in_the_reason(self) -> None:
        ib = _FakeIB(reported_type=None)

        def raise_error_during_sleep(seconds: float) -> None:
            ib.errorEvent.fire(1, 10089, "requires additional subscription")

        ib.sleep = raise_error_during_sleep  # type: ignore[method-assign]

        result = measure_symbol_entitlement(
            ib, "MSFT", now=TODAY, timeout_seconds=0.25, poll_seconds=0.25
        )

        assert result.entry_allowed is None
        assert "10089" in (result.error or "")

    def test_failed_qualification_is_inconclusive_with_a_named_reason(self) -> None:
        ib = _FakeIB(reported_type=1, qualifies=False)

        result = measure_symbol_entitlement(ib, "NOPE", now=TODAY)

        assert result.entry_allowed is None
        assert result.listing_venue is None
        assert "did not qualify" in result.reason

    def test_market_data_is_always_cancelled(self) -> None:
        ib = _FakeIB(reported_type=1)

        measure_symbol_entitlement(ib, "SPY", now=TODAY)

        assert len(ib.cancelled) == 1

    def test_callback_recorder_is_removed_even_on_a_hard_denial(self) -> None:
        ib = _FakeIB(reported_type=3)
        original_mdt = ib.wrapper.marketDataType

        measure_symbol_entitlement(ib, "AAPL", now=TODAY)

        assert ib.wrapper.marketDataType == original_mdt


class TestMeasureCatalogEntitlement:
    def test_measures_every_symbol_in_order(self) -> None:
        ib = _FakeIB(reported_type=1)

        results = measure_catalog_entitlement(ib, ["SPY", "IWM", "GLD"], now=TODAY)

        assert [r.symbol for r in results] == ["SPY", "IWM", "GLD"]
        assert all(r.entry_allowed is True for r in results)

    def test_one_symbols_failure_does_not_abort_the_batch(self) -> None:
        calls: list[str] = []

        class SometimesFailingIB(_FakeIB):
            def qualifyContracts(self, contract):
                calls.append(contract.symbol)
                if contract.symbol == "BAD":
                    return []
                return super().qualifyContracts(contract)

        ib = SometimesFailingIB(reported_type=1)

        results = measure_catalog_entitlement(ib, ["SPY", "BAD", "IWM"], now=TODAY)

        assert calls == ["SPY", "BAD", "IWM"]
        assert results[0].entry_allowed is True
        assert results[1].entry_allowed is None
        assert results[2].entry_allowed is True
