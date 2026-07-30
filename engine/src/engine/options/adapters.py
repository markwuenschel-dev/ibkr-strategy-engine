"""IBKR implementations of the ports. The only options module that knows ib_async.

Everything above this file -- the risk checks, the governor, the scan's decision
logic -- is written against :mod:`engine.options.ports` and can be exercised in
full with no broker present. This is where that ends.

Nothing here can transmit an order. The adapters call ``reqSecDefOptParams``,
``reqContractDetails``, ``qualifyContracts``, ``reqHistoricalData``,
``reqMktData``, ``cancelMktData``, ``accountSummary``, ``positions`` and
``whatIfOrder``. ``placeOrder`` does not appear, and
``tests/test_options_no_transmit.py`` walks the AST of every module in this
package to keep it that way.

**The live adapter is honest about being blocked.** It is fully implemented and
it will refuse under the account's current entitlement, because it reports what
the server actually said rather than what was asked for. That is the intended
behaviour, not an unfinished path: running it against a delayed-only account
produces ``OPTIONS_REALTIME_DATA_REQUIRED`` from the entitlement gate, which is
the correct answer. It gives
:class:`engine.options.marketdata.MarketDataSubscription` its first production
caller -- until now the whole subscription state machine existed only in tests.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from .chain import (
    QualifiedOption,
    discover_expirations,
    enumerate_strikes,
    qualify_strikes,
)
from .domain import OptionStrategyIntent
from .execution import MarginAssessment, what_if
from .ivrank import IVObservation, observations_from_bars
from .marketdata import (
    IB_UNSET,
    MarketDataSubscription,
    MarketDataType,
    OptionQuote,
    UnderlyingQuote,
)
from .portfolio import PortfolioSnapshot, PositionExposure
from .ports import UNDERLYING_GENERATION_KEY, StrategyQuoteSnapshot

__all__ = [
    "IBKRContractDataAdapter",
    "IBKRVolatilityHistoryAdapter",
    "IBKRWhatIfAdapter",
    "IBKRLiveMarketDataAdapter",
    "IBKRPortfolioStateAdapter",
    "NET_LIQUIDATION_TAG",
    "INITIAL_MARGIN_TAG",
]

NET_LIQUIDATION_TAG = "NetLiquidation"
INITIAL_MARGIN_TAG = "FullInitMarginReq"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _price(value: Any) -> Decimal | None:
    """A usable price, or ``None``.

    Screens the same three things the rest of the package screens: NaN/infinity,
    IBKR's DBL_MAX "does not apply" marker, and non-positive values. A price of
    ``-1`` is IBKR saying "no data", not a negative market.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if abs(number) >= IB_UNSET:
        return None
    if number <= 0:
        return None
    try:
        return Decimal(str(number))
    except (InvalidOperation, ValueError):  # pragma: no cover - str(float) is safe
        return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if abs(value) >= IB_UNSET:
        return None
    return int(value)


def _aware(value: Any) -> dt.datetime | None:
    """A timezone-aware provider timestamp, or ``None``.

    A naive timestamp is discarded rather than assumed to be UTC. The provenance
    layer measures staleness from this field, and a wrong timezone would shift a
    quote's apparent age by hours in whichever direction happened to be
    convenient.
    """
    if not isinstance(value, dt.datetime):
        return None
    if value.tzinfo is None:
        return None
    return value


class IBKRContractDataAdapter:
    """:class:`~engine.options.ports.ContractDataPort` over ``ib_async``."""

    def __init__(self, ib: Any) -> None:
        self.ib = ib
        self._con_ids: dict[str, int] = {}

    def underlying_con_id(self, symbol: str) -> int:
        """Qualify the underlying once and remember its contract id."""
        key = symbol.strip().upper()
        if key not in self._con_ids:
            from ib_async import Stock  # noqa: PLC0415 - optional dependency

            qualified = self.ib.qualifyContracts(Stock(key, "SMART", "USD"))
            if not qualified:
                raise LookupError(f"IBKR did not qualify the underlying {key}")
            self._con_ids[key] = int(getattr(qualified[0], "conId", 0))
        return self._con_ids[key]

    def expirations(self, symbol: str) -> Sequence[str]:
        return discover_expirations(
            self.ib, symbol.strip().upper(), self.underlying_con_id(symbol)
        )

    def strikes(self, symbol: str, expiry: str, right: str) -> Sequence[Decimal]:
        return enumerate_strikes(self.ib, symbol.strip().upper(), expiry, right)

    def qualify(
        self,
        symbol: str,
        expiry: str,
        strikes: Sequence[Decimal],
        right: str,
    ) -> Sequence[QualifiedOption]:
        return qualify_strikes(self.ib, symbol.strip().upper(), expiry, strikes, right)


class IBKRVolatilityHistoryAdapter:
    """:class:`~engine.options.ports.VolatilityHistoryPort` over ``ib_async``.

    Uses ``whatToShow="OPTION_IMPLIED_VOLATILITY"``, which returns real bars on an
    account with no market-data subscription -- the one input to this strategy
    that is not blocked on the entitlement.
    """

    def __init__(self, ib: Any, contract_data: IBKRContractDataAdapter) -> None:
        self.ib = ib
        self.contract_data = contract_data

    def implied_volatility_history(
        self, symbol: str, *, duration: str = "1 Y"
    ) -> Sequence[IVObservation]:
        from ib_async import Stock  # noqa: PLC0415 - optional dependency

        key = symbol.strip().upper()
        qualified = self.ib.qualifyContracts(Stock(key, "SMART", "USD"))
        if not qualified:
            return []
        bars = self.ib.reqHistoricalData(
            qualified[0],
            endDateTime="",
            durationStr=duration,
            barSizeSetting="1 day",
            whatToShow="OPTION_IMPLIED_VOLATILITY",
            useRTH=True,
            formatDate=1,
        )
        return observations_from_bars(bars or [])


class IBKRWhatIfAdapter:
    """:class:`~engine.options.ports.BrokerWhatIfPort` over ``ib_async``.

    A one-line delegation to :func:`engine.options.execution.what_if`, and worth
    the file it takes up: it is what lets the risk checks be handed a fake margin
    assessment in tests without any of them importing ``ib_async``.
    """

    def __init__(self, ib: Any) -> None:
        self.ib = ib

    def what_if(
        self, intent: OptionStrategyIntent, *, observed_at: dt.datetime
    ) -> MarginAssessment:
        return what_if(self.ib, intent, observed_at=observed_at)


class IBKRLiveMarketDataAdapter:
    """:class:`~engine.options.ports.LiveMarketDataPort` over ``ib_async``.

    Subscribes to the underlying and every requested leg, records what the server
    reports, and returns frozen snapshots stamped with the subscription
    generation that was in force. Strategy code never sees a ``Ticker``.

    ``requested_type`` defaults to :attr:`MarketDataType.LIVE`. It is a request,
    not an assertion: if the account is not entitled, TWS answers with a
    different type, the provenance records what it actually said, and the
    entitlement gate refuses. There is deliberately no way to make this adapter
    *claim* live data -- the reported type is only ever written from the server's
    own callback, via
    :meth:`engine.options.marketdata.MarketDataSubscription.record_data_type`.
    """

    def __init__(
        self,
        ib: Any,
        *,
        requested_type: int = int(MarketDataType.LIVE),
        settle_seconds: float = 20.0,
        underlying_lead_seconds: float = 2.0,
        poll_seconds: float = 0.25,
        clock: Any = time.monotonic,
    ) -> None:
        self.ib = ib
        # A seam, so the deadline can be exercised without a test spending the
        # real seconds. ``ib.sleep`` is the only thing that yields to the event
        # loop; this only measures.
        self.clock = clock
        self.requested_type = requested_type
        # A ceiling, not a duration: the wait below exits as soon as every leg
        # has greeks, so raising this costs nothing on a healthy run and buys
        # patience on a slow one.
        self.settle_seconds = settle_seconds
        self.underlying_lead_seconds = underlying_lead_seconds
        self.poll_seconds = poll_seconds

    def strategy_quotes(
        self,
        *,
        underlying_symbol: str,
        con_ids: Sequence[int],
    ) -> StrategyQuoteSnapshot:
        from ib_async import Contract, Stock  # noqa: PLC0415 - optional dependency

        from .probe import CallbackRecorder  # noqa: PLC0415 - avoids a cycle

        symbol = underlying_symbol.strip().upper()
        subscribed_at = _utcnow()

        underlying_contract = self.ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
        if not underlying_contract:
            raise LookupError(f"IBKR did not qualify the underlying {symbol}")
        underlying_contract = underlying_contract[0]
        underlying_con_id = int(getattr(underlying_contract, "conId", 0))

        leg_contracts = self.ib.qualifyContracts(
            *[Contract(conId=int(con_id), exchange="SMART") for con_id in con_ids]
        )

        subscriptions: dict[int, MarketDataSubscription] = {
            underlying_con_id: MarketDataSubscription(
                requested_type=self.requested_type, subscribed_at=subscribed_at
            )
        }
        for contract in leg_contracts:
            subscriptions[int(getattr(contract, "conId", 0))] = MarketDataSubscription(
                requested_type=self.requested_type, subscribed_at=subscribed_at
            )

        recorder = CallbackRecorder(self.ib)
        recorder.install()
        tickers: dict[int, Any] = {}
        contracts: dict[int, Any] = {underlying_con_id: underlying_contract}
        for contract in leg_contracts:
            contracts[int(getattr(contract, "conId", 0))] = contract

        try:
            self.ib.reqMarketDataType(self.requested_type)
            # The underlying goes first and is given a head start. IBKR computes
            # option model greeks from the underlying price, so subscribing the
            # whole chain in one burst asks for computations whose input has not
            # arrived yet.
            tickers[underlying_con_id] = self.ib.reqMktData(
                underlying_contract, "", False, False
            )
            self.ib.sleep(min(self.underlying_lead_seconds, self.settle_seconds))
            option_ids = [c for c in contracts if c != underlying_con_id]
            for con_id in option_ids:
                tickers[con_id] = self.ib.reqMktData(contracts[con_id], "", False, False)

            # Wait for the greeks themselves rather than for a fixed number of
            # seconds. A flat sleep is a bet that every model computation lands
            # inside it, and the size of that bet scales with the number of legs
            # -- so the same command produces a different subset of the chain on
            # each run, and strike selection becomes a race. Polling the
            # recorder (rather than ``waitOnUpdate``, which drops ticks) lets a
            # good run finish early and a slow one keep waiting.
            deadline = self.clock() + self.settle_seconds
            while self.clock() < deadline:
                self.ib.sleep(self.poll_seconds)
                if all(recorder.latest_greeks.get(c) is not None for c in option_ids):
                    break

            observed_at = _utcnow()
            for con_id, subscription in subscriptions.items():
                for reported in recorder.data_types.get(con_id, []):
                    subscription.record_data_type(reported, at=observed_at)
                # The recorder maps callbacks by reqId; if that mapping missed,
                # the ticker still carries the computation ib_async wrote to it.
                greeks = recorder.latest_greeks.get(con_id) or getattr(
                    tickers.get(con_id), "modelGreeks", None
                )
                if greeks is not None:
                    subscription.record_greeks(
                        greeks, at=observed_at, generation=subscription.generation
                    )
                provider_time = _aware(getattr(tickers.get(con_id), "time", None))
                if provider_time is not None:
                    subscription.record_provider_event(provider_time)
        finally:
            for contract in contracts.values():
                try:
                    self.ib.cancelMktData(contract)
                except Exception:  # noqa: BLE001 - teardown must not mask a result
                    pass
            recorder.remove()

        underlying_ticker = tickers.get(underlying_con_id)
        underlying = UnderlyingQuote(
            symbol=symbol,
            provenance=subscriptions[underlying_con_id].provenance(),
            bid=_price(getattr(underlying_ticker, "bid", None)),
            ask=_price(getattr(underlying_ticker, "ask", None)),
            last=_price(getattr(underlying_ticker, "last", None)),
            close=_price(getattr(underlying_ticker, "close", None)),
        )

        legs: list[OptionQuote] = []
        generations: list[tuple[str, UUID]] = [
            (UNDERLYING_GENERATION_KEY, subscriptions[underlying_con_id].generation)
        ]
        for con_id in (int(c) for c in con_ids):
            subscription = subscriptions.get(con_id)
            if subscription is None:
                # A leg IBKR would not qualify. Skipping it silently would hand
                # the gate a snapshot that is coherent but incomplete, so the
                # missing-generation check in StrategyQuoteSnapshot is left to
                # refuse it -- which is why nothing is appended for it here.
                continue
            ticker = tickers.get(con_id)
            legs.append(
                OptionQuote(
                    con_id=con_id,
                    provenance=subscription.provenance(),
                    bid=_price(getattr(ticker, "bid", None)),
                    ask=_price(getattr(ticker, "ask", None)),
                    last=_price(getattr(ticker, "last", None)),
                    close=_price(getattr(ticker, "close", None)),
                    open_interest=_int_or_none(getattr(ticker, "putOpenInterest", None)),
                    volume=_int_or_none(getattr(ticker, "volume", None)),
                    greeks=subscription.current_greeks(),
                )
            )
            generations.append((str(con_id), subscription.generation))

        return StrategyQuoteSnapshot(
            underlying=underlying,
            legs=tuple(legs),
            generations=tuple(generations),
        )


class IBKRPortfolioStateAdapter:
    """:class:`~engine.options.ports.PortfolioStatePort` over :class:`engine.broker.Broker`.

    Reads net liquidation and the broker's own reserved-margin figure from
    ``accountSummary``.

    **Known incompleteness, stated rather than hidden.** ``exposures`` is passed
    in by the caller and defaults to empty, because this engine has no persisted
    store of open option structures yet -- ``ib.positions()`` reports contracts
    and average cost, not the buying power reserved against each combo, and there
    is no way to recover a structure's per-position BPR from it. The consequence
    is precise and worth being clear about:

    * **total** buying-power capping is correct even with no exposures, because
      :attr:`engine.options.portfolio.PortfolioSnapshot.total_buying_power_reserved`
      takes the larger of the derived sum and the broker's reported figure;
    * **per-underlying, sector and correlation** capping counts only the
      candidate until a position store exists.

    So the governor is conservative on the aggregate and incomplete on the
    buckets. That is a real gap in the strategy, not a bug in this adapter, and
    it closes when open structures are persisted.
    """

    def __init__(self, broker: Any) -> None:
        self.broker = broker

    def _summary_value(self, rows: Sequence[tuple[str, str, str]], tag: str) -> Decimal | None:
        for row_tag, value, _currency in rows:
            if row_tag == tag:
                try:
                    parsed = Decimal(str(value))
                except InvalidOperation:
                    return None
                return parsed if parsed.is_finite() else None
        return None

    def snapshot(
        self,
        *,
        as_of: dt.datetime,
        exposures: Sequence[PositionExposure] = (),
    ) -> PortfolioSnapshot:
        rows = self.broker.account_summary()
        net_liquidation = self._summary_value(rows, NET_LIQUIDATION_TAG)
        if net_liquidation is None:
            # Fail closed at the boundary rather than letting the governor decide
            # what a missing net liquidation means. PortfolioSnapshot refuses a
            # non-positive value, so this raises InvalidPortfolioStateError with
            # a message naming the tag that was absent.
            raise LookupError(
                f"the broker returned no {NET_LIQUIDATION_TAG} in its account "
                "summary; portfolio limits cannot be sized without it"
            )
        reserved = self._summary_value(rows, INITIAL_MARGIN_TAG)
        if reserved is not None and reserved < 0:
            reserved = None
        return PortfolioSnapshot(
            as_of=as_of,
            net_liquidation=net_liquidation,
            positions=tuple(exposures),
            reported_buying_power_reserved=reserved,
        )
