"""The ib_async wrapper -- the only module here that talks to TWS.

Everything broker-facing is funnelled through this one class so that the rest of
the engine can be tested without a socket, and so there is exactly one place to
audit when asking "what can this program actually send to a broker?".

The ``IB`` object is injected rather than constructed internally
(:class:`Broker` takes ``ib=``), which is what lets the test suite drive a fake
with the same method surface and keeps the promise that no test touches the
network.

Two API details worth knowing before reading on:

* **Market data type.** Paper accounts usually have no real-time subscription,
  so the engine asks for delayed data (``reqMarketDataType(3)``, or ``4`` for
  delayed-frozen outside hours) and carries that choice through to
  :class:`Quote.source` -- a delayed price must never be silently presented as
  live.
* **``whatIfOrder``.** IBKR returns margin and commission for an order *without
  placing it*. That is the pre-trade gate in :meth:`preview`, and it is the last
  thing that happens before :meth:`place` transmits anything.

Field names on ``OrderState`` are read defensively via :func:`_first_float`.
This session did not verify IBKR's exact attribute spellings against a live
connection, so rather than assert one and have it silently read ``None`` at the
worst moment, the code tries the known candidates and refuses when it finds
nothing -- which :meth:`~engine.safety.SafetyGate.gate_margin` treats as a
refusal, not a pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .config import EngineConfig
from .errors import ConnectionError_, EngineError
from .journal import OrderJournal
from .safety import BUY, OrderIntent

# reqMarketDataType codes, per the IBKR API.
MARKET_DATA_LIVE = 1
MARKET_DATA_FROZEN = 2
MARKET_DATA_DELAYED = 3
MARKET_DATA_DELAYED_FROZEN = 4

MARKET_DATA_LABELS = {
    MARKET_DATA_LIVE: "live",
    MARKET_DATA_FROZEN: "frozen",
    MARKET_DATA_DELAYED: "delayed",
    MARKET_DATA_DELAYED_FROZEN: "delayed-frozen",
}

# Enough of ``OrderState`` to tell one from whatever else a failed request
# resolved to. Structural rather than an isinstance check, so the test fake does
# not have to be an ib_async type to exercise the same path.
_ORDER_STATE_FIELDS = (
    "initMarginChange",
    "maintMarginChange",
    "commission",
    "equityWithLoanChange",
)

# IBKR sends DBL_MAX for "this field does not apply", e.g. minCommission and
# maxCommission on a plain stock order. It is a finite float, so the NaN/inf
# screen below does not catch it, and left alone it would be read as a real
# number -- a commission of 1.8e308, or worse a margin impact that happens to
# clear a cap comparison somewhere.
IB_UNSET = 1.7976931348623157e308


@dataclass
class Quote:
    """A price plus, always, where it came from."""

    symbol: str
    price: float | None
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    close: float | None = None
    requested_market_data_type: int = MARKET_DATA_DELAYED
    reported_market_data_type: int | None = None

    @property
    def source(self) -> str:
        """What the *provider* said this is, never what we asked for.

        These were one field until it was noticed that it stored the requested
        constant, which meant a quote could be labelled "live" purely because
        live was requested -- the label being wrong in exactly the case where
        it matters. When the server has not reported a type the label says so
        rather than guessing.

        This is a display-honesty fix, not an entitlement gate. A real
        ``ib_async`` ``Ticker.marketDataType`` defaults to ``1``, so a silent
        server is indistinguishable from a confirmed-live one at this layer.
        Distinguishing them needs per-subscription bookkeeping, which is why
        the options path uses :mod:`engine.options.marketdata` and not this
        class.
        """
        if self.reported_market_data_type is None:
            requested = MARKET_DATA_LABELS.get(
                self.requested_market_data_type, f"type-{self.requested_market_data_type}"
            )
            return f"{requested} (requested, unconfirmed)"
        return MARKET_DATA_LABELS.get(
            self.reported_market_data_type, f"type-{self.reported_market_data_type}"
        )

    def describe(self) -> str:
        price = f"{self.price:,.4f}" if self.price is not None else "unavailable"
        parts = [f"{self.symbol}  {price}  [{self.source}]"]
        detail = ", ".join(
            f"{name} {value:,.4f}"
            for name, value in (
                ("bid", self.bid),
                ("ask", self.ask),
                ("last", self.last),
                ("close", self.close),
            )
            if value is not None
        )
        if detail:
            parts.append(f"  ({detail})")
        return "".join(parts)


@dataclass
class Preview:
    """The result of ``whatIfOrder`` -- what the order *would* cost."""

    intent: OrderIntent
    init_margin_change: float | None = None
    maint_margin_change: float | None = None
    commission: float | None = None
    equity_with_loan_change: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def margin_impact(self) -> float | None:
        """The number the margin cap is applied to.

        Initial margin is the binding constraint on whether a new order can be
        placed, so it is preferred; maintenance margin is the fallback when the
        broker only reports that.
        """
        for value in (self.init_margin_change, self.maint_margin_change):
            if value is not None:
                return value
        return None

    def describe(self) -> str:
        def show(value: float | None) -> str:
            return f"{value:,.2f}" if value is not None else "not reported"

        return "\n".join(
            [
                f"  order        {self.intent.describe()}",
                f"  init margin  {show(self.init_margin_change)}",
                f"  maint margin {show(self.maint_margin_change)}",
                f"  commission   {show(self.commission)}",
                f"  equity delta {show(self.equity_with_loan_change)}",
            ]
        )


class Broker:
    """Connection, market data and order placement against one paper account."""

    def __init__(
        self,
        config: EngineConfig,
        journal: OrderJournal,
        *,
        ib: Any | None = None,
    ) -> None:
        self.config = config
        self.journal = journal
        self._ib = ib
        self._connected = False

    # -- lifecycle -------------------------------------------------------

    @property
    def ib(self) -> Any:
        if self._ib is None:
            self._ib = self._make_ib()
        return self._ib

    def _make_ib(self) -> Any:
        """Import ib_async lazily so the module imports without it installed."""
        try:
            from ib_async import IB  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ConnectionError_(
                f"ib_async is not installed: {exc}",
                hint="run `uv sync` inside engine/",
            ) from exc
        return IB()

    def connect(self) -> str:
        """Connect and prove we are on the expected paper account.

        Returns the confirmed account id. Raises before doing anything else if
        the broker is serving an account we were not told to expect -- the
        second of the two interlocks described in :mod:`engine.config`.
        """
        try:
            self.ib.connect(
                self.config.host,
                self.config.port,
                clientId=self.config.client_id,
                timeout=self.config.connect_timeout,
            )
        except Exception as exc:
            raise ConnectionError_(
                f"could not connect to {self.config.venue} at "
                f"{self.config.host}:{self.config.port}: {exc}",
                hint=(
                    "is TWS running and logged in to the PAPER account, with "
                    "Configure > API > Settings > 'Enable ActiveX and Socket Clients' "
                    f"ticked and the socket port set to {self.config.port}?"
                ),
            ) from exc

        self._connected = True
        accounts = [str(a) for a in (self.ib.managedAccounts() or [])]
        if self.config.account_id not in accounts:
            self.disconnect()
            raise ConnectionError_(
                f"connected, but this session serves {accounts or 'no accounts'} -- "
                f"not the configured account {self.config.account_id}",
                hint=(
                    "refusing to trade an account you did not name. Either fix "
                    "IBKR_ACCOUNT_ID, or check which account TWS is logged in to."
                ),
            )
        self.journal.record(
            "connected",
            account=self.config.account_id,
            venue=self.config.venue,
            host=self.config.host,
            port=self.config.port,
        )
        return self.config.account_id

    def disconnect(self) -> None:
        if self._ib is not None and self._connected:
            try:
                self._ib.disconnect()
            except Exception:  # pragma: no cover - teardown must not mask errors
                pass
            self._connected = False

    def __enter__(self) -> "Broker":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.disconnect()

    # -- read-only surface (M1) ------------------------------------------

    def account_summary(self) -> list[tuple[str, str, str]]:
        """``[(tag, value, currency)]`` for the configured account."""
        rows = self.ib.accountSummary(self.config.account_id)
        out: list[tuple[str, str, str]] = []
        for row in rows or []:
            out.append(
                (
                    str(getattr(row, "tag", "")),
                    str(getattr(row, "value", "")),
                    str(getattr(row, "currency", "")),
                )
            )
        return out

    def positions(self) -> list[tuple[str, float, float]]:
        """``[(symbol, quantity, average_cost)]``."""
        out: list[tuple[str, float, float]] = []
        for position in self.ib.positions(self.config.account_id) or []:
            contract = getattr(position, "contract", None)
            symbol = str(getattr(contract, "symbol", "?"))
            out.append(
                (
                    symbol,
                    _as_float(getattr(position, "position", 0)) or 0.0,
                    _as_float(getattr(position, "avgCost", 0)) or 0.0,
                )
            )
        return out

    def position_qty(self, symbol: str) -> int:
        """Signed quantity currently held, for the position cap."""
        wanted = symbol.strip().upper()
        for held_symbol, quantity, _cost in self.positions():
            if held_symbol.upper() == wanted:
                return int(quantity)
        return 0

    # -- market data (M2) ------------------------------------------------

    def quote(self, symbol: str, *, timeout: float = 6.0) -> Quote:
        """A delayed quote, labelled with its data type.

        Delayed is requested rather than live because a paper account normally
        has no real-time subscription; asking for live and getting nothing back
        is a far more confusing failure than asking for delayed and saying so.
        """
        contract = self._stock(symbol)
        data_type = MARKET_DATA_DELAYED
        try:
            self.ib.reqMarketDataType(data_type)
        except Exception:  # pragma: no cover - older servers
            pass

        ticker = self.ib.reqMktData(contract, "", False, False)
        self._settle(timeout)

        price = _first_float(ticker, ("marketPrice",), call=True)
        last = _first_float(ticker, ("last", "close"))
        bid = _first_float(ticker, ("bid",))
        ask = _first_float(ticker, ("ask",))
        close = _first_float(ticker, ("close",))
        if price is None:
            # Mid, then last, then close -- in decreasing order of freshness.
            if bid is not None and ask is not None:
                price = (bid + ask) / 2
            else:
                price = last if last is not None else close

        try:
            self.ib.cancelMktData(contract)
        except Exception:  # pragma: no cover
            pass

        # Read back what the provider said rather than echoing the request.
        # getattr, because a ticker that has received nothing may not carry the
        # attribute at all -- and absent is the honest answer in that case.
        reported = getattr(ticker, "marketDataType", None)
        if not isinstance(reported, int) or isinstance(reported, bool):
            reported = None

        return Quote(
            symbol=symbol.strip().upper(),
            price=price,
            bid=bid,
            ask=ask,
            last=last,
            close=close,
            requested_market_data_type=data_type,
            reported_market_data_type=reported,
        )

    # -- orders (M3, M4) -------------------------------------------------

    def preview(self, intent: OrderIntent) -> Preview:
        """``whatIfOrder``: margin and commission, transmitting nothing."""
        contract = self._stock(intent.symbol)
        order = self._build_order(intent)
        state = self.ib.whatIfOrder(contract, order)
        if state is None or not any(
            hasattr(state, name) for name in _ORDER_STATE_FIELDS
        ):
            # Not merely absent -- the wrong *shape*. ib_async ends a failed
            # request by resolving it with its empty result container, so a
            # rejected whatIf arrives as `[]`, not as None and not as an
            # OrderState. Reading fields off that silently yields "margin not
            # reported", which gate_margin refuses -- correct, but it blames the
            # cap for what was actually a broker error. Say which it was.
            raise EngineError(
                f"the broker returned no order state for {intent.describe()} "
                f"(got {type(state).__name__} {state!r})",
                hint=(
                    "the whatIf request was rejected by TWS -- check the error "
                    "line above it for the IBKR code. Cannot price the order, "
                    "so it will not be placed."
                ),
            )
        return Preview(
            intent=intent,
            init_margin_change=_first_float(
                state, ("initMarginChange", "initMarginAfter", "maintMarginChange")
            ),
            maint_margin_change=_first_float(state, ("maintMarginChange", "maintMarginAfter")),
            commission=_first_float(state, ("commission", "minCommission", "maxCommission")),
            equity_with_loan_change=_first_float(
                state, ("equityWithLoanChange", "equityWithLoanAfter")
            ),
            raw={
                name: getattr(state, name)
                for name in dir(state)
                if not name.startswith("_") and not callable(getattr(state, name, None))
            },
        )

    def place(self, intent: OrderIntent, *, timeout: float = 30.0) -> dict[str, Any]:
        """Transmit an order and wait for it to finish. Returns the fill record.

        The caller is responsible for having passed every gate in
        :class:`~engine.safety.SafetyGate` first. This method does not re-check
        them -- it is the transmit step, and mixing policy into it would give two
        places to look when asking what is allowed.
        """
        contract = self._stock(intent.symbol)
        order = self._build_order(intent)
        trade = self.ib.placeOrder(contract, order)

        deadline = timeout
        step = 0.5
        while deadline > 0 and not _is_done(trade):
            self._settle(step)
            deadline -= step

        status = getattr(getattr(trade, "orderStatus", None), "status", "Unknown")
        filled = _as_float(getattr(getattr(trade, "orderStatus", None), "filled", 0)) or 0.0
        avg_price = _as_float(
            getattr(getattr(trade, "orderStatus", None), "avgFillPrice", None)
        )
        order_id = getattr(getattr(trade, "order", None), "orderId", None)

        return {
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "status": status,
            "filled": filled,
            "avg_fill_price": avg_price,
            "order_id": order_id,
            "account": self.config.account_id,
            "timed_out": not _is_done(trade),
        }

    # -- internals -------------------------------------------------------

    def _stock(self, symbol: str) -> Any:
        from ib_async import Stock  # type: ignore[import-not-found]

        contract = Stock(symbol.strip().upper(), "SMART", "USD")
        qualified = self.ib.qualifyContracts(contract)
        return (qualified[0] if qualified else contract)

    def _build_order(self, intent: OrderIntent) -> Any:
        from ib_async import LimitOrder, MarketOrder  # type: ignore[import-not-found]

        action = BUY if intent.side == BUY else "SELL"
        if intent.limit_price is not None:
            order = LimitOrder(action, intent.quantity, intent.limit_price)
        else:
            order = MarketOrder(action, intent.quantity)
        # Pin the account on the order itself. If this session ever manages more
        # than one, the order still cannot land anywhere but the configured one.
        order.account = self.config.account_id
        # Set the time-in-force explicitly, even though DAY is what an unset TIF
        # resolves to anyway. Left blank, TWS applies its order preset and
        # announces it with error 10349 "Order TIF was set to DAY based on order
        # preset." ib_async does not list 10349 as a warning
        # (ib_async/wrapper.py:1609), so it ends the in-flight request and
        # whatIfOrder resolves to the empty container `[]` instead of an
        # OrderState -- margin comes back unknown and gate_margin refuses a
        # perfectly ordinary order. Naming the value leaves the preset nothing to
        # override, and no 10349 is emitted. Verified against TWS 2026-07-28.
        order.tif = "DAY"
        return order

    def _settle(self, seconds: float) -> None:
        """Let the event loop process incoming messages."""
        sleep = getattr(self.ib, "sleep", None)
        if callable(sleep):
            sleep(seconds)


def _is_done(trade: Any) -> bool:
    is_done = getattr(trade, "isDone", None)
    if callable(is_done):
        try:
            return bool(is_done())
        except Exception:  # pragma: no cover
            return False
    return False


def _as_float(value: Any) -> float | None:
    """Coerce to float, treating NaN and non-numbers as 'not reported'.

    IBKR uses NaN liberally for absent prices, and a NaN that leaks into a
    notional calculation compares false against every cap -- silently passing a
    check it should have failed.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    if abs(number) >= IB_UNSET:
        return None
    return number


def _first_float(source: Any, names: tuple[str, ...], *, call: bool = False) -> float | None:
    """First attribute in ``names`` that yields a usable number, else None."""
    for name in names:
        attribute = getattr(source, name, None)
        if attribute is None:
            continue
        value = attribute
        if call and callable(attribute):
            try:
                value = attribute()
            except Exception:  # pragma: no cover
                continue
        number = _as_float(value)
        if number is not None:
            return number
    return None
