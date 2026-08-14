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
from .executions import executions_from_fills
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

#: How the broker is asked what it is still working, in order of preference.
#:
#: ``openTrades`` first, because a ``Trade`` carries the ``Order`` -- and so the
#: ``orderRef`` this engine stamps with the strategy id, which is the only
#: identifier that both survives a reconnect and proves the order is ours.
#: ``openOrders`` is the same information without the wrapper. ``trades`` is
#: last and is a superset that includes finished orders; the reconciler matches
#: on identity rather than on liveness, so a completed order in the list can
#: only make a position look *more* accounted for, never less.
OPEN_ORDER_READERS = ("openTrades", "openOrders", "trades")


def read_open_orders(
    ib: Any,
    *,
    budget: Any = None,
    budget_priority: Any = None,
) -> tuple[Any, ...] | None:
    """What the broker is working, or ``None`` if it could not be asked.

    ``None`` and ``()`` are different answers and
    :meth:`engine.options.positions.PositionStore.reconcile_against_broker`
    treats them differently. An engine that cannot enumerate open orders must
    not be allowed to report, of an order the broker is working, that the broker
    is not working it -- that false claim is what this function exists to make
    unrepresentable.

    Raises whatever the client raises. A caller that cannot tolerate that is
    telling itself the query succeeded, which is the same mistake one layer up.

    ``budget`` is optional for manual commands and legacy callers. The
    persistent worker supplies the connection-scoped shared budget so fallback
    readers cannot quietly spend unreserved working-order capacity.
    """
    for name in OPEN_ORDER_READERS:
        reader = getattr(ib, name, None)
        if not callable(reader):
            continue
        if budget is not None:
            from .pacing import Priority, RequestKind  # noqa: PLC0415

            budget.acquire(
                RequestKind.GENERAL,
                priority=budget_priority or Priority.WORKING_ORDERS,
            )
        answer = reader()
        if answer is None:
            continue
        return tuple(answer)
    return None


__all__ = [
    "OPEN_ORDER_READERS",
    "read_open_orders",
    "IBKRContractDataAdapter",
    "IBKRVolatilityHistoryAdapter",
    "IBKRWhatIfAdapter",
    "IBKRLiveMarketDataAdapter",
    "IBKRExecutionReportAdapter",
    "IBKRPortfolioStateAdapter",
    "NET_LIQUIDATION_TAG",
    "INITIAL_MARGIN_TAG",
]

NET_LIQUIDATION_TAG = "NetLiquidation"
INITIAL_MARGIN_TAG = "FullInitMarginReq"


def quote_priority(
    *, require_two_sided: bool, per_call: Any = None, instance_default: Any = None
) -> Any:
    """Which pacing priority one ``strategy_quotes`` call draws at.

    An **explicit per-call priority outranks the two-sided heuristic**. The
    heuristic -- two-sided means a held structure's own legs, so spend at
    ``EXITS_MANAGEMENT`` -- is only right for the management path. The runner's
    binding revalidation also demands a two-sided book for a candidate that is
    *not* held, and letting the heuristic win there would spend the 25%
    management reserve at priority 1 on work the audit places at
    ``AUTHORIZATION`` (docs/INTEGRATION-M3-M4.md section 6). The heuristic
    stays as the default only for callers that state nothing.

    Pure and module-level so the resolution order is pinned by a test rather
    than living inline where a refactor could silently reorder it.
    """
    from .pacing import Priority  # noqa: PLC0415 - keeps import optional

    if per_call is not None:
        return per_call
    if require_two_sided:
        return Priority.EXITS_MANAGEMENT
    return instance_default or Priority.CANDIDATE_CONSTRUCTION


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


def _open_interest_for(ticker: Any) -> int | None:
    """The right side's open interest, chosen by the ticker's own contract.

    ib_async exposes ``callOpenInterest`` and ``putOpenInterest`` as separate
    fields (tick 101 fills exactly one of them per contract). Reading
    ``putOpenInterest`` unconditionally -- the pre-2026-08-01 behaviour --
    reports None for every call. Falling back to the other field when the
    right cannot be read keeps a mislabeled ticker from erasing a real figure;
    both absent is honestly None.
    """
    right = str(getattr(getattr(ticker, "contract", None), "right", "")).upper()
    call_oi = _int_or_none(getattr(ticker, "callOpenInterest", None))
    put_oi = _int_or_none(getattr(ticker, "putOpenInterest", None))
    if right.startswith("C"):
        return call_oi if call_oi is not None else put_oi
    if right.startswith("P"):
        return put_oi if put_oi is not None else call_oi
    return put_oi if put_oi is not None else call_oi


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

    def __init__(self, ib: Any, *, budget: Any = None, budget_priority: Any = None) -> None:
        self.ib = ib
        # Contract discovery is broker traffic too.  The original adapter was
        # a pacing hole because qualification and option-chain requests went
        # straight to ib_async while market/history adapters used the shared
        # connection budget.  Keep the dependency structural and optional so
        # manual/test adapters retain their old behaviour.
        self.budget = budget
        self.budget_priority = budget_priority
        self._con_ids: dict[str, int] = {}

    def _acquire(self) -> None:
        if self.budget is None:
            return
        from .pacing import Priority, RequestKind  # noqa: PLC0415

        self.budget.acquire(
            RequestKind.GENERAL,
            priority=self.budget_priority or Priority.DISCOVERY,
        )

    def underlying_con_id(self, symbol: str) -> int:
        """Qualify the underlying once and remember its contract id."""
        key = symbol.strip().upper()
        if key not in self._con_ids:
            from ib_async import Stock  # noqa: PLC0415 - optional dependency

            self._acquire()
            qualified = self.ib.qualifyContracts(Stock(key, "SMART", "USD"))
            if not qualified:
                raise LookupError(f"IBKR did not qualify the underlying {key}")
            self._con_ids[key] = int(getattr(qualified[0], "conId", 0))
        return self._con_ids[key]

    def expirations(self, symbol: str) -> Sequence[str]:
        self._acquire()
        return discover_expirations(
            self.ib, symbol.strip().upper(), self.underlying_con_id(symbol)
        )

    def strikes(self, symbol: str, expiry: str, right: str) -> Sequence[Decimal]:
        self._acquire()
        return enumerate_strikes(self.ib, symbol.strip().upper(), expiry, right)

    def qualify(
        self,
        symbol: str,
        expiry: str,
        strikes: Sequence[Decimal],
        right: str,
    ) -> Sequence[QualifiedOption]:
        self._acquire()
        return qualify_strikes(self.ib, symbol.strip().upper(), expiry, strikes, right)


class IBKRVolatilityHistoryAdapter:
    """:class:`~engine.options.ports.VolatilityHistoryPort` over ``ib_async``.

    Uses ``whatToShow="OPTION_IMPLIED_VOLATILITY"``, which returns real bars on an
    account with no market-data subscription -- the one input to this strategy
    that is not blocked on the entitlement.
    """

    def __init__(
        self,
        ib: Any,
        contract_data: IBKRContractDataAdapter,
        *,
        budget: Any = None,
        budget_priority: Any = None,
    ) -> None:
        self.ib = ib
        self.contract_data = contract_data
        # The connection-scoped request budget, when the caller runs inside
        # one. Historical pulls are the hard-limited request class; a scanner
        # constructs this adapter with DISCOVERY priority so ninety pulls
        # queue behind anything the held book needs.
        self.budget = budget
        self.budget_priority = budget_priority

    def implied_volatility_history(
        self, symbol: str, *, duration: str = "1 Y"
    ) -> Sequence[IVObservation]:
        from ib_async import Stock  # noqa: PLC0415 - optional dependency

        key = symbol.strip().upper()
        if self.budget is not None:
            from .pacing import Priority, RequestKind  # noqa: PLC0415

            self.budget.acquire(
                RequestKind.GENERAL,
                priority=self.budget_priority or Priority.DISCOVERY,
            )
        qualified = self.ib.qualifyContracts(Stock(key, "SMART", "USD"))
        if not qualified:
            return []
        if self.budget is not None:
            from .pacing import Priority, RequestKind  # noqa: PLC0415

            self.budget.acquire(
                RequestKind.HISTORICAL,
                priority=self.budget_priority or Priority.DISCOVERY,
            )
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

    def __init__(
        self, ib: Any, *, budget: Any = None, budget_priority: Any = None
    ) -> None:
        self.ib = ib
        self.budget = budget
        self.budget_priority = budget_priority

    def _acquire(self) -> None:
        if self.budget is None:
            return
        from .pacing import Priority, RequestKind  # noqa: PLC0415

        self.budget.acquire(
            RequestKind.GENERAL,
            priority=self.budget_priority or Priority.AUTHORIZATION,
        )

    def what_if(
        self, intent: OptionStrategyIntent, *, observed_at: dt.datetime
    ) -> MarginAssessment:
        self._acquire()
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
        budget: Any = None,
        budget_priority: Any = None,
    ) -> None:
        self.ib = ib
        # Connection-scoped pacing, when the caller runs inside a budget.
        # A ``require_two_sided`` call is by definition about a held
        # structure's own legs, so it draws at EXITS_MANAGEMENT priority
        # regardless of the constructor default -- managing what exists
        # outranks whatever this adapter instance was built for.
        self.budget = budget
        self.budget_priority = budget_priority
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
        require_two_sided: bool = False,
        budget_priority: Any = None,
    ) -> StrategyQuoteSnapshot:
        from ib_async import Contract, Stock  # noqa: PLC0415 - optional dependency

        from .probe import CallbackRecorder  # noqa: PLC0415 - avoids a cycle

        symbol = underlying_symbol.strip().upper()
        subscribed_at = _utcnow()

        if self.budget is not None:
            from .pacing import RequestKind  # noqa: PLC0415

            # Explicit per-call priority outranks the two-sided heuristic:
            # binding revalidation demands two-sided for a candidate that is
            # NOT held and must draw at AUTHORIZATION, not spend the
            # management reserve. See quote_priority.
            priority = quote_priority(
                require_two_sided=require_two_sided,
                per_call=budget_priority,
                instance_default=self.budget_priority,
            )
            # One token per subscription line this call will open, acquired up
            # front: the underlying plus every leg. Acquiring before the first
            # request keeps a paced scan from half-subscribing a structure.
            for _ in range(1 + len(con_ids)):
                self.budget.acquire(RequestKind.GENERAL, priority=priority)
            # Qualification is broker traffic too, and it happens before the
            # subscription calls below. Reserve both qualification calls so a
            # large candidate probe cannot consume untracked general-message
            # capacity before the advertised subscription budget is used.
            self.budget.acquire(RequestKind.GENERAL, priority=priority)

        underlying_contract = self.ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
        if not underlying_contract:
            raise LookupError(f"IBKR did not qualify the underlying {symbol}")
        underlying_contract = underlying_contract[0]
        underlying_con_id = int(getattr(underlying_contract, "conId", 0))

        if self.budget is not None:
            self.budget.acquire(RequestKind.GENERAL, priority=priority)
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
                # Generic ticks 100 (option volume) and 101 (open interest):
                # neither arrives on the default tick set, so an empty list here
                # is why open interest was None on every live run before
                # 2026-08-01. The underlying keeps the default set -- OI is an
                # option concept.
                tickers[con_id] = self.ib.reqMktData(
                    contracts[con_id], "100,101", False, False
                )

            # Wait for the greeks themselves rather than for a fixed number of
            # seconds. A flat sleep is a bet that every model computation lands
            # inside it, and the size of that bet scales with the number of legs
            # -- so the same command produces a different subset of the chain on
            # each run, and strike selection becomes a race. Polling the
            # recorder (rather than ``waitOnUpdate``, which drops ticks) lets a
            # good run finish early and a slow one keep waiting.
            #
            # ``require_two_sided`` additionally holds out for a bid AND an ask
            # on every leg. Callers quoting a *selected structure* (marking a
            # held position, pricing an exit) pass True, because a one-sided
            # snapshot is precisely the coin-flip that made in-pass marking
            # fail 7 of 10 passes on 2026-07-31. Callers sweeping a chain
            # window must NOT: deep wings without bids would pin the wait at
            # its ceiling on every pass. At the deadline a one-sided snapshot
            # is still returned for downstream truthfulness gates.
            deadline = self.clock() + self.settle_seconds
            while self.clock() < deadline:
                self.ib.sleep(self.poll_seconds)
                if not all(
                    recorder.latest_greeks.get(c) is not None for c in option_ids
                ):
                    continue
                if require_two_sided and not all(
                    _price(getattr(tickers.get(c), "bid", None)) is not None
                    and _price(getattr(tickers.get(c), "ask", None)) is not None
                    for c in option_ids
                ):
                    continue
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
                    open_interest=_open_interest_for(ticker),
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


class IBKRExecutionReportAdapter:
    """:class:`~engine.options.ports.ExecutionReportPort` over ``ib_async``.

    ``ib.fills()`` is preferred over ``ib.executions()`` because only the first
    pairs each execution with its ``commissionReport``. The second returns bare
    ``Execution`` objects, and an execution with no commission attached is
    exactly the state this engine already recorded once and could not recover
    from -- reading the API that structurally cannot answer the question is how
    that happens twice.

    ``reqExecutions`` is called first when available, because ``ib.fills()``
    returns only what has been delivered to *this* session's event loop. After a
    restart that set is empty, and an empty set is indistinguishable from an
    uncosted fill. Asking the server repopulates it.

    Reads only. Both calls are queries.
    """

    def __init__(
        self, ib: Any, *, budget: Any = None, budget_priority: Any = None
    ) -> None:
        self.ib = ib
        self.budget = budget
        self.budget_priority = budget_priority

    def executions(self) -> Sequence[Any]:
        request = getattr(self.ib, "reqExecutions", None)
        if callable(request):
            if self.budget is not None:
                from .pacing import Priority, RequestKind  # noqa: PLC0415

                self.budget.acquire(
                    RequestKind.GENERAL,
                    priority=self.budget_priority or Priority.WORKING_ORDERS,
                )
            try:
                request()
            except Exception:  # noqa: BLE001 - a refill failure is not a result
                # The local fill set is still readable and may already hold what
                # is needed. Swallowing here loses nothing: an execution that is
                # genuinely absent shows up downstream as a coverage gap, which
                # is the honest answer rather than an outage.
                pass
        reader = getattr(self.ib, "fills", None)
        fills = reader() if callable(reader) else ()
        return executions_from_fills(fills)


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

    def __init__(
        self, broker: Any, *, budget: Any = None, budget_priority: Any = None
    ) -> None:
        self.broker = broker
        self.budget = budget
        self.budget_priority = budget_priority

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
        if self.budget is not None:
            from .pacing import Priority, RequestKind  # noqa: PLC0415

            self.budget.acquire(
                RequestKind.GENERAL,
                priority=self.budget_priority or Priority.AUTHORIZATION,
            )
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
