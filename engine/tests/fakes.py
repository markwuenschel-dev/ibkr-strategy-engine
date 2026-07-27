"""A fake ``IB`` with the same surface :class:`engine.broker.Broker` uses.

Only the methods the broker actually calls are implemented. That is deliberate:
if the broker starts calling something new, these tests fail with
``AttributeError`` rather than silently exercising a different path than
production does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeContract:
    symbol: str = "SPY"
    secType: str = "STK"


@dataclass
class FakePosition:
    contract: FakeContract
    position: float
    avgCost: float


@dataclass
class FakeSummaryRow:
    tag: str
    value: str
    currency: str = "USD"


@dataclass
class FakeTicker:
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    close: float | None = None

    def marketPrice(self) -> float:  # noqa: N802 - mirrors the ib_async name
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.last if self.last is not None else float("nan")


@dataclass
class FakeOrderState:
    initMarginChange: float = 100.0  # noqa: N815 - mirrors the IBKR field names
    maintMarginChange: float = 90.0  # noqa: N815
    commission: float = 1.0  # noqa: N815
    equityWithLoanChange: float = 0.0  # noqa: N815


@dataclass
class FakeOrderStatus:
    status: str = "Filled"
    filled: float = 1.0
    avgFillPrice: float = 100.0  # noqa: N815


@dataclass
class FakeOrder:
    orderId: int = 42  # noqa: N815
    account: str = ""


@dataclass
class FakeTrade:
    order: FakeOrder = field(default_factory=FakeOrder)
    orderStatus: FakeOrderStatus = field(default_factory=FakeOrderStatus)  # noqa: N815
    done: bool = True

    def isDone(self) -> bool:  # noqa: N802 - mirrors the ib_async name
        return self.done


class FakeIB:
    """Records what it was asked to do so tests can assert on it."""

    def __init__(
        self,
        *,
        accounts: list[str] | None = None,
        ticker: FakeTicker | None = None,
        positions: list[FakePosition] | None = None,
        order_state: FakeOrderState | None = None,
        trade: FakeTrade | None = None,
        connect_error: Exception | None = None,
    ) -> None:
        self._accounts = accounts if accounts is not None else ["DU1234567"]
        self._ticker = ticker or FakeTicker(bid=99.0, ask=101.0, last=100.0, close=99.5)
        self._positions = positions or []
        self._order_state = order_state or FakeOrderState()
        self._trade = trade or FakeTrade()
        self._connect_error = connect_error

        self.connected = False
        self.placed: list[tuple[Any, Any]] = []
        self.what_ifs: list[tuple[Any, Any]] = []
        self.market_data_types: list[int] = []
        self.cancelled: list[Any] = []

    # -- lifecycle -------------------------------------------------------

    def connect(self, host: str, port: int, clientId: int, timeout: float = 10.0) -> None:  # noqa: N803
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True
        self.host, self.port, self.client_id = host, port, clientId

    def disconnect(self) -> None:
        self.connected = False

    def managedAccounts(self) -> list[str]:  # noqa: N802
        return list(self._accounts)

    def sleep(self, seconds: float) -> None:
        return None

    # -- reads -----------------------------------------------------------

    def accountSummary(self, account: str = "") -> list[FakeSummaryRow]:  # noqa: N802
        return [
            FakeSummaryRow("AccountType", "INDIVIDUAL"),
            FakeSummaryRow("NetLiquidation", "1000000.00"),
            FakeSummaryRow("BuyingPower", "4000000.00"),
        ]

    def positions(self, account: str = "") -> list[FakePosition]:
        return list(self._positions)

    def qualifyContracts(self, contract: Any) -> list[Any]:  # noqa: N802
        return [contract]

    def reqMarketDataType(self, data_type: int) -> None:  # noqa: N802
        self.market_data_types.append(data_type)

    def reqMktData(self, contract: Any, *args: object, **kwargs: object) -> FakeTicker:  # noqa: N802
        return self._ticker

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        self.cancelled.append(contract)

    # -- orders ----------------------------------------------------------

    def whatIfOrder(self, contract: Any, order: Any) -> FakeOrderState:  # noqa: N802
        self.what_ifs.append((contract, order))
        return self._order_state

    def placeOrder(self, contract: Any, order: Any) -> FakeTrade:  # noqa: N802
        self.placed.append((contract, order))
        return self._trade
