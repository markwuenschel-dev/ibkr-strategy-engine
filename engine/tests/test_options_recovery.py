"""Order-lifecycle recovery, driven by a broker fake that actually changes state.

Every other options test hands the runner a broker whose answer is decided before
the order is sent: one frozen ``orderStatus``, ``isDone()`` returning ``True`` on
the first poll. That fake cannot express the sequences this file is about, and a
sequence is where recovery lives -- an order does not fail, it *becomes* rejected,
after it was acknowledged, having filled one of three lots.

``ScriptedIB`` therefore returns a :class:`ScriptedTrade` that walks a list of
callbacks as ``place_combo`` polls it. One state is observed per poll, in order,
so a test writes the broker's story and the engine reads it exactly as it would
read TWS: submitted, acknowledged, partial, filled -- or acknowledged and then
Inactive with an error attached, or a fill callback that beats every status
callback to the socket.

The properties under test all share one shape: **an order that did something must
never be recorded as an order that did nothing.** A partial fill is a position. A
cancel after a partial is a position. A timeout is not a failure. A disconnect
says nothing whatsoever about the order and must never be resolved into a verdict.

``ScriptedIB.placeOrder`` records every call, and that is load-bearing rather than
decorative: a fake missing the method would make every ``placed == []`` assertion
pass by ``AttributeError``, proving the exact opposite of what it claims.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

import reviewer
from engine.config import EngineConfig
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
from engine.options.orderstate import OrderLifecycleState, snapshot_from_trade
from engine.options.policy import RiskPolicy
from engine.options.portfolio import PortfolioSnapshot, PositionExposure
from engine.options.ports import StrategyQuoteSnapshot
from engine.options.positions import (
    PositionEvent,
    PositionState,
    PositionStore,
)
from engine.options.runner import RunReport, run_once
from engine.options.selection import Bias
from engine.options.sink import LifecycleRecorder
from engine.safety import SafetyGate

D = Decimal

NOW = dt.datetime(2026, 7, 29, 13, 0, tzinfo=dt.timezone.utc)
TODAY = NOW.date()

SPOT = D("500")
UNDERLYING_CON_ID = 9000

#: 400..600 in fives. ``narrow_strikes`` keeps the middle 25 with no reference
#: price, which is 440..560, and both legs below sit inside that window.
STRIKES = [D(strike) for strike in range(400, 601, 5)]

#: |delta| is exactly 0.30 at 450, which is ``directional_target_delta``; 445 is
#: the listed strike nearest ``target_width`` below it.
SHORT_STRIKE = D("450")
LONG_STRIKE = D("445")

#: Contract ids ARE the strikes, so the market-data fake can decode a strike back
#: out of a con_id and serve the chain scan and the management mark from one port.
SHORT_CON_ID = 450
LONG_CON_ID = 445

HALF_SPREAD = D("0.05")

#: What the what-if reserves against a 5-wide spread.
BPR = D("500")

#: The credit the chain scan prices: 15.00 short mid minus 13.50 long mid.
CREDIT = D("1.50")

#: (5.00 width - 1.50 credit) * 100. The sizing arithmetic downstream of this is
#: exact, so ``sized_policy`` can name a contract count rather than guess at one.
LOSS_PER_CONTRACT = D("350")


# ---------------------------------------------------------------------------
# The market the fakes present
# ---------------------------------------------------------------------------


def leg_mid(strike: Decimal) -> Decimal:
    """A put mid that rises with the strike, so the two legs differ."""
    return max(D("0.10"), (strike - D("400")) * D("0.30"))


def leg_delta(strike: Decimal) -> Decimal:
    """Put delta, negative, exactly -0.30 at 450."""
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
) -> OptionQuote:
    """One leg quote whose greeks carry the quote's own generation.

    A mismatched generation would be refused by
    ``require_uniform_live_provenance`` and would sink every candidate for a
    reason no test in this file is about.
    """
    generation = uuid4()
    return OptionQuote(
        con_id=con_id,
        provenance=provenance(generation, reported=reported, at=at),
        bid=mid - HALF_SPREAD,
        ask=mid + HALF_SPREAD,
        greeks=OptionGreeks(
            received_at=at, subscription_generation=generation, delta=delta
        ),
    )


def quote_snapshot(
    legs: tuple[OptionQuote, ...],
    *,
    symbol: str = "SPY",
    at: dt.datetime = NOW,
) -> StrategyQuoteSnapshot:
    underlying_generation = uuid4()
    return StrategyQuoteSnapshot(
        underlying=UnderlyingQuote(
            symbol=symbol,
            provenance=provenance(
                underlying_generation, reported=MarketDataType.LIVE, at=at
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
# The stateful broker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Callback:
    """One broker callback, as ``place_combo`` would observe it on one poll.

    ``None`` means "resolve against the order that was actually sent", which is
    what lets a single script serve a 3-lot open and a 1-lot close without the
    test restating the arithmetic:

    ``filled``     ``None`` -> the whole order
    ``remaining``  ``None`` -> whatever is left of the order after ``filled``
    ``average``    ``None`` -> the submitted limit price, sign and all

    ``average=0.0`` is therefore not the same as ``average=None``: zero is an
    unpopulated field, which is exactly what a status callback carries before
    anything has traded.
    """

    status: str = ""
    filled: float | None = 0.0
    remaining: float | None = None
    average: float | None = 0.0
    message: str | None = None
    done: bool = False


#: Handed to the API, nothing back yet. The state a Trade is in the instant
#: ``placeOrder`` returns.
SUBMITTED = Callback(status="PreSubmitted")
#: The broker has it and is working it.
ACKNOWLEDGED = Callback(status="Submitted")


def partial(filled: float, *, status: str = "Submitted") -> Callback:
    """``filled`` lots done, the rest still working. Fills at the sent limit."""
    return Callback(status=status, filled=filled, average=None)


def cancelled_after(filled: float) -> Callback:
    """The remainder cancelled, with ``filled`` lots already in the market."""
    return Callback(status="Cancelled", filled=filled, average=None, done=True)


def rejected(message: str) -> Callback:
    """IBKR's usual shape for a refusal: ``Inactive`` plus an error in the log."""
    return Callback(status="Inactive", message=message, done=True)


#: Everything fills. The terminal state carries the whole order at the limit.
FILLED = Callback(status="Filled", filled=None, average=None, done=True)

#: The ordinary happy sequence, used for any order a test is not interrogating.
SCRIPT_CLEAN_FILL: tuple[Callback, ...] = (SUBMITTED, ACKNOWLEDGED, FILLED)

#: Submitted, acknowledged, one lot of three, then the rest.
SCRIPT_PARTIAL_THEN_FILLED: tuple[Callback, ...] = (
    SUBMITTED,
    ACKNOWLEDGED,
    partial(1.0),
    FILLED,
)

#: One lot of three, and the remainder still working when we stop waiting. The
#: fill arithmetic outranks the timeout, so this resolves to PARTIALLY_FILLED --
#: working, not terminal, and a real position either way.
SCRIPT_PARTIAL_STILL_WORKING: tuple[Callback, ...] = (
    SUBMITTED,
    ACKNOWLEDGED,
    partial(1.0),
)

#: Submitted, acknowledged, one lot of three, and the remainder cancelled.
SCRIPT_PARTIAL_THEN_CANCELLED: tuple[Callback, ...] = (
    SUBMITTED,
    ACKNOWLEDGED,
    partial(1.0),
    cancelled_after(1.0),
)

#: Acknowledged and then refused.
SCRIPT_REJECTED: tuple[Callback, ...] = (
    SUBMITTED,
    ACKNOWLEDGED,
    rejected("201: Order rejected - reason: margin"),
)

#: Nothing ever comes back. Not even a status string, which is what a lost
#: acknowledgement actually looks like: an empty ``orderStatus`` forever.
SCRIPT_SILENT: tuple[Callback, ...] = (Callback(),)

#: The same partial delivered twice in a row, which IBKR does freely.
SCRIPT_DUPLICATE_PARTIAL: tuple[Callback, ...] = (
    SUBMITTED,
    ACKNOWLEDGED,
    partial(1.0),
    partial(1.0),
    cancelled_after(1.0),
)

#: A complete fill with no status string at all -- the execution callback beat
#: every order-status callback to the socket. The fill counts are the evidence.
SCRIPT_FILL_BEFORE_STATUS: tuple[Callback, ...] = (
    Callback(status="", filled=None, average=None, done=True),
)

#: Two lots, then a stale callback claiming one, then the remainder cancelled at
#: two. The stale number is genuinely delivered mid-stream, and two is what the
#: order actually did.
SCRIPT_STALE_SMALLER_FILL: tuple[Callback, ...] = (
    SUBMITTED,
    ACKNOWLEDGED,
    partial(2.0),
    partial(1.0),
    cancelled_after(2.0),
)


#: Accepted and worked, forever. The last callback repeats, so this order never
#: fills and never dies -- which is exactly what the first real order did.
SCRIPT_WORKING_FOREVER: tuple[Callback, ...] = (SUBMITTED, ACKNOWLEDGED)


def cancelled_empty(*, done: bool) -> Callback:
    """Cancelled with nothing filled -- a terminal refusal, still being polled."""
    return Callback(status="Cancelled", filled=0.0, done=done)


#: A terminal cancel, then a *stale earlier status* re-delivered after it, then
#: the cancel again. This is the sequence that exercises the progression guard
#: rather than the fill-count guard: the re-delivered ``Submitted`` carries no
#: new fill, so nothing but its **rank** marks it as old news. Without the
#: guard it would walk the recorder's idea of the state back to ACKNOWLEDGED,
#: and the next cancel would then look like a fresh terminal verdict and append
#: a second OPEN_FAILED for one order that failed once.
SCRIPT_STALE_STATUS_AFTER_TERMINAL: tuple[Callback, ...] = (
    SUBMITTED,
    ACKNOWLEDGED,
    cancelled_empty(done=False),
    ACKNOWLEDGED,
    cancelled_empty(done=True),
)


class _Status:
    """The ``orderStatus`` shape ``snapshot_from_trade`` reads."""

    def __init__(self, callback: Callback) -> None:
        self.status = callback.status
        self.filled = callback.filled
        self.remaining = callback.remaining
        self.avgFillPrice = callback.average  # noqa: N815
        self.commission = None


class _LogEntry:
    def __init__(self, message: str) -> None:
        self.message = message


class _Order:
    def __init__(
        self, *, order_id: int, perm_id: int, order_ref: str = "", limit_price: float = 0.0
    ) -> None:
        self.orderId = order_id  # noqa: N815
        self.permId = perm_id  # noqa: N815
        self.orderRef = order_ref  # noqa: N815
        self.lmtPrice = limit_price  # noqa: N815


class ScriptedTrade:
    """A ``Trade`` that changes as it is polled, instead of one frozen answer.

    One callback is observed per ``isDone()`` call, in order, and the last one
    repeats forever. That mirrors ``place_combo``'s loop exactly -- it polls
    ``isDone()`` once per iteration and reads ``orderStatus`` once at the end --
    so a script is read as the sequence of states the engine actually saw.
    """

    def __init__(
        self,
        script: tuple[Callback, ...],
        *,
        order_id: int,
        perm_id: int,
        total_quantity: float,
        limit_price: float,
        order_ref: str = "",
        observer: Callable[[int], None] | None = None,
    ) -> None:
        if not script:
            raise ValueError("a scripted trade needs at least one callback")
        self.order = _Order(
            order_id=order_id,
            perm_id=perm_id,
            order_ref=order_ref,
            limit_price=limit_price,
        )
        self.limit_price = limit_price
        self.total_quantity = total_quantity
        self.polls = 0
        self._index = 0
        self._observer = observer
        self._script = tuple(
            _resolve(callback, total=total_quantity, limit=limit_price)
            for callback in script
        )

    def accept_cancel(self, script: tuple[Callback, ...], *, total: float) -> None:
        """What the broker does when ``cancelOrder`` reaches a working order.

        The trade keeps its identity -- same ``Order`` object, same ids -- and
        starts walking a new script from the beginning. That is what a real
        cancellation does: the same order transitions, it is not replaced by a
        different one, and a fake that handed back a fresh trade would let the
        ladder appear to work while losing the identifiers reconciliation
        matches on.
        """
        self._script = tuple(
            _resolve(callback, total=total, limit=self.limit_price)
            for callback in script
        )
        self._index = 0

    @property
    def current(self) -> Callback:
        return self._script[self._index]

    @property
    def observed(self) -> tuple[Callback, ...]:
        """Every callback this trade has already surfaced."""
        return self._script[: self._index + 1]

    @property
    def orderStatus(self) -> _Status:  # noqa: N802
        return _Status(self.current)

    @property
    def log(self) -> list[_LogEntry]:
        message = self.current.message
        return [_LogEntry(message)] if message else []

    def isDone(self) -> bool:  # noqa: N802
        """Answer for the current callback, then advance to the next one.

        Answer-then-advance rather than advance-then-answer, so the first poll
        observes the first scripted state instead of skipping it.
        """
        # Called *before* this poll's emission, so an observer sees the store
        # exactly as a process dying at this instant would have left it on
        # disk. That is the only way to prove persistence is callback-driven
        # rather than final-snapshot-driven: reading the store after the loop
        # cannot distinguish the two, because both end up correct.
        if self._observer is not None:
            self._observer(self.polls)
        self.polls += 1
        done = self.current.done
        if self._index + 1 < len(self._script):
            self._index += 1
        return done


def _resolve(callback: Callback, *, total: float, limit: float) -> Callback:
    """Fill in the ``None`` fields from the order that was actually sent."""
    filled = total if callback.filled is None else callback.filled
    remaining = (total - filled) if callback.remaining is None else callback.remaining
    average = limit if callback.average is None else callback.average
    return dataclasses.replace(
        callback, filled=filled, remaining=remaining, average=average
    )


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


class ScriptedIB:
    """Everything ``run_once`` calls on ``broker.ib``, with a stateful order book.

    ``scripts`` is one script per ``placeOrder`` call; the last one repeats, so a
    test that only cares about the first order does not have to enumerate the
    rest. ``connected_polls`` is how many times ``isConnected()`` answers True
    before the socket is declared gone -- that is what drives the disconnect path,
    and it is deliberately a *count of polls* rather than a flag, so the drop
    lands in the middle of the wait rather than before it.
    """

    def __init__(
        self,
        *,
        scripts: tuple[tuple[Callback, ...], ...] = (SCRIPT_CLEAN_FILL,),
        today: dt.date = TODAY,
        connected_polls: int | None = None,
        place_error: str | None = None,
        observer: Callable[[int], None] | None = None,
        cancel_script: tuple[Callback, ...] = (),
        cancel_error: str | None = None,
        working_orders: tuple[Any, ...] | None = None,
    ) -> None:
        if not scripts:
            raise ValueError("ScriptedIB needs at least one script")
        self.today = today
        self.scripts = scripts
        self.connected_polls = connected_polls
        self.place_error = place_error
        #: What a cancelled order does next. Defaults to a clean terminal
        #: cancellation with nothing filled -- the ordinary case.
        self.cancel_script = cancel_script or (cancelled_empty(done=True),)
        self.cancel_error = cancel_error
        #: What ``openTrades()`` reports. ``None`` means the method answers with
        #: whatever this fake has placed and not yet seen finish, which is what
        #: a real broker does.
        self.working_orders = working_orders
        #: Called once per poll, before that poll's observation is persisted.
        self.observer = observer
        self.placed: list[tuple[Any, Any]] = []
        self.cancelled: list[Any] = []
        self.trades: list[ScriptedTrade] = []
        self.slept = 0.0
        self.connection_checks = 0

    # -- chain and margin, static ----------------------------------------

    def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
        out: list[Any] = []
        for contract in contracts:
            if getattr(contract, "secType", "") == "STK":
                out.append(_Contract(con_id=UNDERLYING_CON_ID, strike=0.0, right=""))
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
        """A year of IV ending at the top of its own range, so IV Rank clears 50."""
        start = self.today - dt.timedelta(days=365)
        return [_Bar(start + dt.timedelta(days=i), 0.10 + 0.001 * i) for i in range(260)]

    def reqSecDefOptParams(self, *_a: Any, **_k: Any) -> list[Any]:  # noqa: N802
        return [_Chain(self.today)]

    def reqContractDetails(self, _contract: Any) -> list[Any]:  # noqa: N802
        return [_Detail(float(strike)) for strike in STRIKES]

    def whatIfOrder(self, _contract: Any, _order: Any) -> Any:  # noqa: N802
        return _OrderState()

    # -- the transmitting surface ----------------------------------------

    def placeOrder(self, contract: Any, order: Any) -> Any:  # noqa: N802
        """Record the call and hand back a Trade that will walk its script."""
        self.placed.append((contract, order))
        if self.place_error is not None:
            raise RuntimeError(self.place_error)
        index = min(len(self.trades), len(self.scripts) - 1)
        trade = ScriptedTrade(
            self.scripts[index],
            order_id=70 + len(self.trades),
            perm_id=8800 + len(self.trades),
            total_quantity=float(getattr(order, "totalQuantity", 1)),
            limit_price=float(getattr(order, "lmtPrice", 0.0)),
            order_ref=str(getattr(order, "orderRef", "") or ""),
            observer=self.observer,
        )
        self.trades.append(trade)
        return trade

    def cancelOrder(self, order: Any) -> Any:  # noqa: N802
        """Retract a working order, by walking its trade onto the cancel script.

        Implemented rather than omitted, for the reason ``RecordingIB`` gives
        about ``placeOrder``: a test that passes because the fake raised
        ``AttributeError`` proves the fake is incomplete, not that the code is
        right.
        """
        self.cancelled.append(order)
        if self.cancel_error is not None:
            raise RuntimeError(self.cancel_error)
        for trade in self.trades:
            if trade.order is order:
                trade.accept_cancel(self.cancel_script, total=trade.total_quantity)
                return trade
        return None

    def openTrades(self) -> tuple[Any, ...]:  # noqa: N802
        """What the broker is still working. See ``working_orders``."""
        if self.working_orders is not None:
            return self.working_orders
        return tuple(trade for trade in self.trades if not trade.current.done)

    def sleep(self, seconds: float) -> None:
        self.slept += seconds

    def isConnected(self) -> bool:  # noqa: N802
        self.connection_checks += 1
        if self.connected_polls is None:
            return True
        return self.connection_checks <= self.connected_polls


class FakeBroker:
    def __init__(
        self,
        *,
        ib: ScriptedIB | None = None,
        positions: tuple[tuple[str, int, float], ...] = (),
    ) -> None:
        self.ib = ib if ib is not None else ScriptedIB()
        self._positions = positions

    def positions(self) -> tuple[tuple[str, int, float], ...]:
        return self._positions


class BrokerOrder:
    """The shape ``reconcile_against_broker`` accepts for a live broker order.

    Deliberately a bare object with ``orderId``/``permId``/``orderRef`` rather
    than an ``ib_async`` type: that is the whole contract ``_reconcile_orders``
    documents, and matching it here proves the reconciler needs no adapter in
    between.

    ``order_ref`` is what ``build_combo`` stamps with the strategy id, and it is
    the only field that says whose order this is.
    """

    def __init__(
        self,
        *,
        order_id: int | None,
        perm_id: int | None,
        order_ref: str | None = None,
    ) -> None:
        self.orderId = order_id  # noqa: N815
        self.permId = perm_id  # noqa: N815
        self.orderRef = order_ref  # noqa: N815


class BrokerTrade:
    """An ``ib_async`` ``Trade``: the identifiers live on ``.order``, not on it.

    ``ib.openTrades()`` returns these, and a reconciler that read ``orderId``
    off the wrapper would see ``None`` on every entry -- indistinguishable from
    an account with nothing working, which is the exact false negative this
    lane exists to remove.
    """

    def __init__(self, order: BrokerOrder) -> None:
        self.order = order


class FakeMarketDataPort:
    def __init__(
        self,
        *,
        price_factor: Decimal = D("1"),
        at: dt.datetime = NOW,
    ) -> None:
        self.price_factor = price_factor
        self.at = at
        self.calls: list[tuple[str, tuple[int, ...]]] = []

    def strategy_quotes(
        self, *, underlying_symbol: str, con_ids: Any
    ) -> StrategyQuoteSnapshot:
        con_ids = tuple(int(c) for c in con_ids)
        self.calls.append((underlying_symbol, con_ids))
        legs = tuple(
            option_quote(
                con_id=con_id,
                mid=leg_mid(D(con_id)) * self.price_factor,
                delta=leg_delta(D(con_id)),
                at=self.at,
            )
            for con_id in con_ids
        )
        return quote_snapshot(legs, symbol=underlying_symbol, at=self.at)


class FakePortfolioPort:
    def __init__(
        self,
        *,
        net_liquidation: str = "1000000",
        positions: tuple[PositionExposure, ...] = (),
    ) -> None:
        self.net_liquidation = D(net_liquidation)
        self.positions = positions

    def snapshot(self, *, as_of: dt.datetime) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            as_of=as_of,
            net_liquidation=self.net_liquidation,
            positions=self.positions,
            reported_buying_power_reserved=None,
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


def store_for(tmp_path: Path, *, name: str = "positions.jsonl") -> PositionStore:
    return PositionStore(tmp_path / "state" / name)


def sized_policy(*, contracts: int) -> RiskPolicy:
    """A policy that sizes the 450/445 spread to exactly ``contracts`` lots.

    The default budget covers one contract, and one contract cannot express a
    partial fill at all -- there is no "some of it" in a 1-lot. Every cap that
    scales with the structure's total risk is lifted to exactly the total this
    quantity produces, so the sizing is what changed and nothing else is.
    """
    total = LOSS_PER_CONTRACT * contracts
    return RiskPolicy(
        risk_budget_per_position=total,
        max_defined_loss_per_position=total,
        max_stress_loss_per_position=total,
    )


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
    quantity: int = 1,
    order_id: int | None = None,
    perm_id: int | None = None,
) -> OptionStrategyIntent:
    """Submit and fill one position -- the same writes the runner makes."""
    intent = spread_intent(
        expiration=TODAY + dt.timedelta(days=dte), credit=credit, quantity=quantity
    )
    store.record_open_submitted(intent, at=NOW, buying_power_reserved=BPR)
    store.record_open_filled(
        intent.strategy_id,
        at=NOW,
        filled_credit=D(credit),
        order_id=order_id,
        perm_id=perm_id,
    )
    return intent


def seed_in_flight_position(
    store: PositionStore,
    *,
    dte: int = 40,
    order_id: int | None = 41,
    perm_id: int | None = 880,
) -> OptionStrategyIntent:
    """A position stranded in OPENING with its broker identifiers recorded.

    Exactly the state a crash between ``record_open_submitted`` and the fill
    leaves behind, which is the state a restart has to be able to read.
    """
    intent = spread_intent(expiration=TODAY + dt.timedelta(days=dte))
    store.record_open_submitted(intent, at=NOW, buying_power_reserved=BPR)
    store.record_acknowledged(
        intent.strategy_id, at=NOW, order_id=order_id, perm_id=perm_id
    )
    return intent


def seed_uncertain_position(
    store: PositionStore, *, dte: int = 40, reason: str = "no resolution before timeout"
) -> OptionStrategyIntent:
    intent = spread_intent(expiration=TODAY + dt.timedelta(days=dte))
    store.record_open_submitted(intent, at=NOW, buying_power_reserved=BPR)
    store.record_uncertain(
        intent.strategy_id, at=NOW, reason=reason, order_id=55, perm_id=9055
    )
    return intent


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
) -> RunReport:
    """One pass, with a real reviewed verifier gate unless one is supplied.

    An entry with no verifier configured is refused before anything is sent, so
    a recovery test that never reached the broker would be proving nothing about
    recovery. The default is a real :class:`~reviewer.ReviewedGate` over a temp
    collab -- a real request, a real reviewer, a real answer -- hung off the same
    ``tmp_path`` the safety gate's state directory uses, so two passes in one
    test share one ledger and the single-use rule really binds them. A test that
    needs a second, independent approval passes its own ``verifier``.
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
    )


def event_names(store: PositionStore) -> list[str]:
    return [str(event.get("event")) for event in store.events()]


def only_position(store: PositionStore) -> Any:
    positions = store.open_positions()
    assert len(positions) == 1, [p.describe() for p in positions]
    return positions[0]


# ===========================================================================
# The scripted broker really does change state
# ===========================================================================


class TestTheFakeIsStateful:
    """If these fail, every sequence below is being read as one frozen status
    and the tests are asserting against a broker that cannot be wrong."""

    def test_a_trade_walks_its_script_one_state_per_poll(self) -> None:
        """A fake whose ``isDone()`` answers the same thing every time cannot
        express "acknowledged, then partially filled, then cancelled" -- which is
        the only shape the recovery paths in this file exist for."""
        trade = ScriptedTrade(
            SCRIPT_PARTIAL_THEN_FILLED,
            order_id=1,
            perm_id=2,
            total_quantity=3.0,
            limit_price=-1.5,
        )
        seen = []
        for _ in range(4):
            seen.append((trade.orderStatus.status, trade.orderStatus.filled))
            trade.isDone()

        assert seen == [
            ("PreSubmitted", 0.0),
            ("Submitted", 0.0),
            ("Submitted", 1.0),
            ("Filled", 3.0),
        ]

    def test_the_last_state_repeats_rather_than_running_off_the_end(self) -> None:
        """``place_combo`` polls once more after the loop breaks. A script that
        raised IndexError there would turn a resolved order into an exception
        escaping after ``placeOrder`` -- the one outcome worse than any state."""
        trade = ScriptedTrade(
            SCRIPT_CLEAN_FILL,
            order_id=1,
            perm_id=2,
            total_quantity=1.0,
            limit_price=-1.5,
        )
        for _ in range(20):
            trade.isDone()

        assert trade.isDone() is True
        assert trade.orderStatus.status == "Filled"

    def test_place_order_is_recorded_so_silence_cannot_pass_by_accident(self) -> None:
        """The whole file's ``placed == []`` assertions are worthless without it."""
        ib = ScriptedIB()
        assert ib.placed == []
        assert hasattr(ib, "placeOrder")


# ===========================================================================
# A. A partial fill is a position, not a failure
# ===========================================================================


class TestPartialFillIsAPosition:
    def test_a_partial_fill_leaves_a_live_smaller_position(self, tmp_path: Path) -> None:
        """One lot of three filled is a real spread in the market.

        Recording it as OPEN_FAILED -- which the old "is it completely filled"
        question did -- leaves a live position nothing manages, and it still
        expires, still gets assigned, and still loses the full width.
        """
        ib = ScriptedIB(scripts=(SCRIPT_PARTIAL_STILL_WORKING,))
        store = store_for(tmp_path)
        report = run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        assert len(ib.placed) == 1, report.describe()
        result = report.transmissions[0]
        assert result.state is OrderLifecycleState.PARTIALLY_FILLED
        assert result.snapshot is not None and result.snapshot.is_working is True

        position = only_position(store)
        assert position.is_live is True
        assert position.quantity == 3
        assert position.filled_quantity == D("1")
        assert position.is_partially_filled is True
        assert PositionEvent.OPEN_FAILED.value not in event_names(store)

    def test_the_exit_is_sized_to_what_filled_not_to_what_was_ordered(
        self, tmp_path: Path
    ) -> None:
        """``manageable_quantity`` is the FILLED count.

        Sizing an exit off the intent after a partial fill sells two contracts
        that were never bought -- a defensive close that opens a naked short.
        """
        ib = ScriptedIB(scripts=(SCRIPT_PARTIAL_STILL_WORKING,))
        store = store_for(tmp_path)
        run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        position = only_position(store)
        assert position.manageable_quantity == 1
        assert position.manageable_quantity != position.quantity

    def test_the_transmitted_exit_is_sized_to_the_fill_not_to_the_intent(
        self, tmp_path: Path
    ) -> None:
        """The exit closes what filled, never what was ordered.

        One lot of a three-lot order filled, so the closing order must be for 1.
        Sizing it off the intent would sell two contracts that were never bought
        -- turning a *defensive* exit at 10 DTE into an opening naked short, at
        whatever price the market happens to be offering.

        ``_manage_one`` passes ``quantity=position.manageable_quantity``; an
        earlier version omitted it and inherited the full order size from
        ``OptionStrategyIntent.closing_intent``'s default.
        """
        store = store_for(tmp_path)
        intent = spread_intent(
            expiration=TODAY + dt.timedelta(days=10), quantity=3
        )
        store.record_open_submitted(intent, at=NOW, buying_power_reserved=BPR)
        store.record_partial_fill(
            intent.strategy_id, at=NOW, filled_quantity=D("1"), average_price=D("-1.50")
        )
        position = store.get(intent.strategy_id)
        assert position is not None and position.manageable_quantity == 1

        # No broker positions, so reconciliation disagrees and the entry half of
        # the pass is blocked -- leaving exactly the exit under test.
        ib = ScriptedIB(scripts=(SCRIPT_CLEAN_FILL,))
        report = run_pass(
            FakeBroker(ib=ib, positions=()), gate_for(tmp_path), store, armed=True
        )

        assert [d.action for d in report.decisions] == [ManagementAction.CLOSE_DTE]
        assert len(ib.placed) == 1, report.describe()
        _bag, order = ib.placed[0]
        assert order.totalQuantity == 1
        assert order.totalQuantity == position.manageable_quantity
        assert order.totalQuantity < position.quantity

    def test_the_run_reports_the_partial_rather_than_claiming_a_clean_entry(
        self, tmp_path: Path
    ) -> None:
        """Entered, and loudly smaller than intended. A silent partial reads in
        the report as a 3-lot the governor has already sized the book against."""
        ib = ScriptedIB(scripts=(SCRIPT_PARTIAL_THEN_FILLED,))
        store = store_for(tmp_path)
        report = run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        # This script does resolve to a complete fill, so the run is a clean
        # entry -- the partial in the middle of the sequence is a state the
        # engine passed through, not the outcome.
        assert report.entered is True
        position = only_position(store)
        assert position.filled_quantity == D("3")
        assert position.is_partially_filled is False


# ===========================================================================
# B. Rejection after acknowledgement
# ===========================================================================


class TestRejectionAfterAcknowledgement:
    def test_a_rejected_order_leaves_no_position_behind(self, tmp_path: Path) -> None:
        """The broker took it, then refused it. Nothing is in the market.

        The OPENING record written before the send must not survive as an OPEN
        position: one the broker never accepted would be managed, exited, and
        counted against every concentration cap forever.
        """
        ib = ScriptedIB(scripts=(SCRIPT_REJECTED,))
        store = store_for(tmp_path)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        assert len(ib.placed) == 1, report.describe()
        assert report.entered is False
        assert store.open_positions() == []
        assert store.positions() == {}

    def test_the_rejection_is_recorded_as_a_failure_not_an_uncertainty(
        self, tmp_path: Path
    ) -> None:
        """A refusal is knowledge, and the distinction is what unblocks trading.

        OPEN_UNCERTAIN would stop every future entry until a human reconciled an
        order the broker has already told us it will never work.
        """
        ib = ScriptedIB(scripts=(SCRIPT_REJECTED,))
        store = store_for(tmp_path)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        assert event_names(store) == [
            PositionEvent.OPEN_SUBMITTED.value,
            PositionEvent.OPEN_ACKNOWLEDGED.value,
            PositionEvent.OPEN_FAILED.value,
        ]
        assert PositionEvent.OPEN_UNCERTAIN.value not in event_names(store)
        assert report.transmissions[0].state is OrderLifecycleState.REJECTED

    def test_inactive_plus_an_error_message_is_a_rejection(
        self, tmp_path: Path
    ) -> None:
        """``Inactive`` alone is ambiguous between refused and suspended, so the
        log message is what resolves it. Reading the status string on its own
        would classify a refusal as a working order and wait for a callback that
        is never coming."""
        ib = ScriptedIB(scripts=(SCRIPT_REJECTED,))
        report = run_pass(
            FakeBroker(ib=ib), gate_for(tmp_path), store_for(tmp_path), armed=True
        )

        snapshot = report.transmissions[0].snapshot
        assert snapshot is not None
        assert snapshot.raw_status == "Inactive"
        assert snapshot.state is OrderLifecycleState.REJECTED
        assert snapshot.is_terminal is True
        assert snapshot.has_position is False


# ===========================================================================
# C. Cancel after a partial
# ===========================================================================


class TestCancelAfterPartial:
    def test_the_order_is_terminal_and_still_has_a_position(
        self, tmp_path: Path
    ) -> None:
        """Terminal, successful and known are three separate questions.

        A cancel-after-partial is terminal and *not* successful and still left
        one lot in the market. Collapsing the three into "did it fill" is how a
        live spread gets recorded as nothing happening.
        """
        ib = ScriptedIB(scripts=(SCRIPT_PARTIAL_THEN_CANCELLED,))
        report = run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store_for(tmp_path),
            armed=True,
            policy=sized_policy(contracts=3),
        )

        result = report.transmissions[0]
        assert result.state is OrderLifecycleState.CANCELLED
        assert result.snapshot is not None
        assert result.snapshot.is_terminal is True
        assert result.snapshot.is_working is False
        assert result.has_position is True
        assert result.filled == D("1")

    def test_the_store_keeps_the_contracts_the_cancel_did_not_take_back(
        self, tmp_path: Path
    ) -> None:
        """A cancellation retires the *remainder*. It does not unwind the lots
        that already traded, and treating it as "nothing happened" abandons
        them."""
        ib = ScriptedIB(scripts=(SCRIPT_PARTIAL_THEN_CANCELLED,))
        store = store_for(tmp_path)
        run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        assert event_names(store) == [
            PositionEvent.OPEN_SUBMITTED.value,
            PositionEvent.OPEN_ACKNOWLEDGED.value,
            PositionEvent.OPEN_PARTIAL.value,
        ]
        position = only_position(store)
        assert position.state is PositionState.OPEN
        assert position.filled_quantity == D("1")
        assert position.manageable_quantity == 1
        assert position.filled_credit == D("1.5")


# ===========================================================================
# D. A timeout with no acknowledgement at all
# ===========================================================================


class TestTimeoutWithNoAcknowledgement:
    def test_silence_becomes_an_uncertainty_and_blocks_the_report(
        self, tmp_path: Path
    ) -> None:
        """We stopped waiting. That says nothing about the order.

        The order may be resting in the book right now, so the only honest
        record is UNCERTAIN -- which counts as live, because something may well
        be in the market.
        """
        ib = ScriptedIB(scripts=(SCRIPT_SILENT,))
        store = store_for(tmp_path)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        assert len(ib.placed) == 1, report.describe()
        assert report.transmissions[0].state is OrderLifecycleState.TIMED_OUT
        assert PositionEvent.OPEN_UNCERTAIN.value in event_names(store)
        assert PositionEvent.OPEN_FAILED.value not in event_names(store)

        position = only_position(store)
        assert position.is_uncertain is True
        assert position.is_live is True
        assert position.state is PositionState.UNCERTAIN
        assert any("unknown" in blocker for blocker in report.blockers), report.blockers

    def test_the_wait_really_ran_before_it_gave_up(self, tmp_path: Path) -> None:
        """Proves the state above came from a timeout rather than from a fake
        that answered "done" on the first poll and never polled again."""
        ib = ScriptedIB(scripts=(SCRIPT_SILENT,))
        run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store_for(tmp_path), armed=True)

        assert ib.trades[0].polls > 1
        assert ib.slept > 0

    def test_a_second_pass_refuses_a_new_entry_and_still_manages(
        self, tmp_path: Path
    ) -> None:
        """An order whose outcome is unknown may be resting in the book, so
        opening another on top of it turns one intended position into two.

        Entries blocked, management not: reconciling and exiting are exactly
        what resolves the unknown state, so locking them would make the block
        permanent.
        """
        gate = gate_for(tmp_path)
        store = store_for(tmp_path)
        first = ScriptedIB(scripts=(SCRIPT_SILENT,))
        run_pass(FakeBroker(ib=first), gate, store, armed=True)

        second = ScriptedIB(scripts=(SCRIPT_CLEAN_FILL,))
        report = run_pass(FakeBroker(ib=second), gate, store, armed=True)

        assert second.placed == [], report.describe()
        assert report.entered is False
        assert "RUNNER_UNRESOLVED_ORDER" in report.refusal_codes
        assert any("unresolved" in blocker for blocker in report.blockers)
        # Management still evaluated the uncertain position rather than skipping
        # the whole pass.
        assert len(report.decisions) == 1
        assert report.decisions[0].action is ManagementAction.HOLD


# ===========================================================================
# E. A disconnect during polling
# ===========================================================================


class TestDisconnectDuringPolling:
    def test_a_dropped_socket_resolves_to_unknown_and_never_to_a_failure(
        self, tmp_path: Path
    ) -> None:
        """A status string read while the socket is down describes the last thing
        we heard, not the order.

        The order may be working, filled, or resting in the book. Recording a
        failure would let the next pass transmit a duplicate.
        """
        ib = ScriptedIB(
            scripts=(SCRIPT_PARTIAL_THEN_FILLED,), connected_polls=2
        )
        store = store_for(tmp_path)
        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        result = report.transmissions[0]
        assert result.state is OrderLifecycleState.UNKNOWN
        assert result.is_uncertain is True
        assert result.message == "connection lost while awaiting the order outcome"
        assert PositionEvent.OPEN_FAILED.value not in event_names(store)

    def test_the_disconnected_position_stays_live_and_uncertain(
        self, tmp_path: Path
    ) -> None:
        """UNCERTAIN counts as live. Treating "might be in the market" as "is
        not" is how a real position stops being managed."""
        ib = ScriptedIB(
            scripts=(SCRIPT_PARTIAL_THEN_FILLED,), connected_polls=2
        )
        store = store_for(tmp_path)
        run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        position = only_position(store)
        assert position.state is PositionState.UNCERTAIN
        assert position.is_uncertain is True
        assert position.is_live is True
        assert "connection lost" in (position.uncertainty or "")

    def test_the_drop_landed_mid_wait_rather_than_before_the_send(
        self, tmp_path: Path
    ) -> None:
        """A socket that was already down before ``placeOrder`` is a different
        and much easier problem. This one is the order that really was sent."""
        ib = ScriptedIB(
            scripts=(SCRIPT_PARTIAL_THEN_FILLED,), connected_polls=2
        )
        run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store_for(tmp_path), armed=True)

        assert len(ib.placed) == 1
        assert ib.connection_checks > 1
        assert ib.trades[0].polls >= 1


# ===========================================================================
# F. A restart mid-flight reconciles rather than duplicates
# ===========================================================================


class TestRestartMidFlight:
    def test_a_second_store_on_the_same_path_replays_to_the_same_state(
        self, tmp_path: Path
    ) -> None:
        """The whole point of an append-only log: the book is on disk, not in
        the process that wrote it. A store that rebuilt a different state after a
        restart would silently disagree with the one that placed the order."""
        first = store_for(tmp_path)
        intent = seed_in_flight_position(first)
        before = first.get(intent.strategy_id)

        restarted = PositionStore(first.path)
        after = restarted.get(intent.strategy_id)

        assert before is not None and after is not None
        assert after.state is PositionState.OPENING
        assert after.state is before.state
        assert after.to_record() == before.to_record()

    def test_the_broker_identifiers_survive_the_restart(self, tmp_path: Path) -> None:
        """``permId`` is IBKR's durable identifier and is the one thing that lets
        a fresh session match an order it can plainly see. A reconciler holding
        only the session-scoped ``orderId`` cannot."""
        first = store_for(tmp_path)
        intent = seed_in_flight_position(first, order_id=41, perm_id=880)

        restarted = PositionStore(first.path)
        position = restarted.get(intent.strategy_id)

        assert position is not None
        assert position.open_order_id == 41
        assert position.open_perm_id == 880

    def test_a_run_after_the_restart_does_not_transmit_the_same_order_again(
        self, tmp_path: Path
    ) -> None:
        """The duplicate this whole state machine exists to prevent.

        The stranded OPENING record is a disagreement with the broker, and a
        disagreeing book refuses new risk -- so the restarted engine reconciles
        rather than sending a second copy of an order that may be live.
        """
        first = store_for(tmp_path)
        intent = seed_in_flight_position(first)

        restarted = PositionStore(first.path)
        ib = ScriptedIB(scripts=(SCRIPT_CLEAN_FILL,))
        report = run_pass(
            FakeBroker(ib=ib, positions=(("SPY", -1, 100.0),)),
            gate_for(tmp_path),
            restarted,
            armed=True,
        )

        assert ib.placed == [], report.describe()
        assert report.entered is False
        assert report.reconciliation is not None
        assert intent.strategy_id in report.reconciliation.stranded_opening
        assert report.reconciliation.agrees is False
        # And specifically not a second order carrying this strategy's identity.
        assert [
            order.orderRef for _bag, order in ib.placed
        ] == []


# ===========================================================================
# G. Duplicate and out-of-order callbacks
# ===========================================================================


class TestDuplicateAndOutOfOrderCallbacks:
    def test_the_same_partial_twice_does_not_double_the_fill(
        self, tmp_path: Path
    ) -> None:
        """IBKR reports **cumulative** fills and re-sends callbacks freely.

        Adding the second callback to the first would record six contracts on a
        three-lot order, and the domain would then refuse the position outright
        as a fill larger than the order.
        """
        store = store_for(tmp_path)
        intent = spread_intent(
            expiration=TODAY + dt.timedelta(days=40), quantity=3
        )
        store.record_open_submitted(intent, at=NOW, buying_power_reserved=BPR)
        for _ in range(2):
            store.record_partial_fill(
                intent.strategy_id,
                at=NOW,
                filled_quantity=D("1"),
                average_price=D("-1.50"),
            )

        position = store.get(intent.strategy_id)
        assert position is not None
        assert position.filled_quantity == D("1")
        assert position.manageable_quantity == 1

    def test_a_stale_smaller_fill_never_walks_the_position_backwards(
        self, tmp_path: Path
    ) -> None:
        """Callbacks arrive out of order. Taking the smaller of two cumulative
        fills under-sizes the exit and leaves contracts in the market that
        nothing intends to close."""
        store = store_for(tmp_path)
        intent = spread_intent(
            expiration=TODAY + dt.timedelta(days=40), quantity=3
        )
        store.record_open_submitted(intent, at=NOW, buying_power_reserved=BPR)
        store.record_partial_fill(
            intent.strategy_id, at=NOW, filled_quantity=D("2"), average_price=D("-1.50")
        )
        store.record_partial_fill(
            intent.strategy_id, at=NOW, filled_quantity=D("1"), average_price=D("-1.50")
        )

        position = store.get(intent.strategy_id)
        assert position is not None
        assert position.filled_quantity == D("2")
        assert position.manageable_quantity == 2

    def test_a_duplicated_callback_stream_still_records_one_position(
        self, tmp_path: Path
    ) -> None:
        """The broker repeats a partial, and one lot is what lands on disk.

        ``place_combo`` snapshots the trade once, at the end of the wait, so a
        repeated callback cannot become a second OPEN_PARTIAL event here -- and
        that single-snapshot behaviour is itself worth pinning, because a future
        callback-driven transmit path would have to earn the deduplication the
        store's monotonic guard currently provides.
        """
        ib = ScriptedIB(scripts=(SCRIPT_DUPLICATE_PARTIAL,))
        store = store_for(tmp_path)
        run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        position = only_position(store)
        assert position.filled_quantity == D("1")
        assert event_names(store).count(PositionEvent.OPEN_PARTIAL.value) == 1

    def test_a_stale_smaller_callback_through_the_runner_keeps_the_larger(
        self, tmp_path: Path
    ) -> None:
        """Two lots filled, then a late callback claiming one, then a cancel.

        Two is what the order did and two is what the exit must be sized to. The
        guard that protects a recorded fill *between* passes lives in the store
        and is asserted above; this one proves the mid-stream stale number is
        not what the runner ends up persisting.
        """
        ib = ScriptedIB(scripts=(SCRIPT_STALE_SMALLER_FILL,))
        store = store_for(tmp_path)
        run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        position = only_position(store)
        assert position.filled_quantity == D("2")
        assert position.manageable_quantity == 2

    def test_a_fill_arriving_before_any_status_callback_is_still_a_fill(
        self, tmp_path: Path
    ) -> None:
        """A complete fill carrying an empty status string is a real sequence.

        The fill counts sit above the status string on purpose: reading the
        string first would classify a finished order as working and leave the
        runner waiting for a callback that has already been and gone.
        """
        ib = ScriptedIB(scripts=(SCRIPT_FILL_BEFORE_STATUS,))
        store = store_for(tmp_path)
        report = run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        result = report.transmissions[0]
        assert result.snapshot is not None
        assert result.snapshot.raw_status == ""
        assert result.state is OrderLifecycleState.FILLED
        assert report.entered is True

        position = only_position(store)
        assert position.state is PositionState.OPEN
        assert position.filled_quantity == D("3")


# ===========================================================================
# H. Order-level reconciliation
# ===========================================================================


class TestOrderLevelReconciliation:
    def test_an_unrelated_resting_order_on_the_account_is_ignored(
        self, tmp_path: Path
    ) -> None:
        """The trap this scoping exists to avoid.

        A paper account is shared with whatever the operator has typed into TWS
        by hand. An order with no ``orderRef`` of ours and no ``permId`` we ever
        recorded is not this engine's, and flagging it would produce a
        DISAGREEMENT that blocks every entry for as long as it rests -- a
        condition no amount of correct engine behaviour could clear.
        """
        store = store_for(tmp_path)
        seed_open_position(store, dte=40, order_id=42, perm_id=8042)

        report = store.reconcile_against_broker(
            (("SPY", -1, 100.0),),
            checked_at=NOW,
            broker_orders=(
                BrokerOrder(order_id=901, perm_id=5501),
                BrokerOrder(order_id=902, perm_id=5502, order_ref="MANUAL-TICKET"),
            ),
        )

        assert report.orders_unknown_to_store == ()
        assert report.agrees is True, report.describe()

    def test_a_foreign_order_reusing_one_of_our_order_ids_is_still_ignored(
        self, tmp_path: Path
    ) -> None:
        """``orderId`` is not ownership evidence, and this is why.

        It is client-assigned and reused across sessions, so a stranger's order
        can carry one of ours by coincidence -- the C22 defect pointed the other
        way. Claiming it would put a foreign order into this engine's
        bookkeeping.
        """
        store = store_for(tmp_path)
        seed_open_position(store, dte=40, order_id=42, perm_id=8042)

        report = store.reconcile_against_broker(
            (("SPY", -1, 100.0),),
            checked_at=NOW,
            broker_orders=(BrokerOrder(order_id=42, perm_id=999_111),),
        )

        assert report.orders_unknown_to_store == ()
        assert report.agrees is True, report.describe()

    def test_our_own_order_with_no_live_position_to_account_for_it_is_surfaced(
        self, tmp_path: Path
    ) -> None:
        """The signal the scoping must not throw away.

        An order carrying a strategy id this store minted, still working at the
        broker, for a position the book says is finished. That is this engine
        having lost track of its own order, and it is exactly what
        ``orders_unknown_to_store`` is for.
        """
        store = store_for(tmp_path)
        intent = seed_open_position(store, dte=40, order_id=42, perm_id=8042)
        store.record_close_submitted(intent.strategy_id, at=NOW, target_debit=D("0.50"))
        store.record_close_filled(intent.strategy_id, at=NOW, closing_debit=D("0.50"))

        report = store.reconcile_against_broker(
            (),
            checked_at=NOW,
            broker_orders=(
                BrokerOrder(order_id=77, perm_id=9077, order_ref=str(intent.strategy_id)),
            ),
        )

        # Reported once, by the durable identifier -- the one that can still be
        # looked up in TWS after a reconnect.
        assert report.orders_unknown_to_store == (9077,)
        assert report.agrees is False

    def test_a_mid_transition_order_the_broker_is_not_working_is_surfaced(
        self, tmp_path: Path
    ) -> None:
        """The dangerous shape: it either filled while we were not looking or was
        never accepted, and those demand opposite fixes. Reported, never
        guessed at.

        ``broker_orders=()`` is load-bearing: it means the broker WAS asked and
        is working nothing.
        """
        store = store_for(tmp_path)
        intent = seed_in_flight_position(store, order_id=41, perm_id=880)

        report = store.reconcile_against_broker(
            (("SPY", -1, 100.0),), checked_at=NOW, broker_orders=()
        )

        assert report.orders_absent_at_broker == (intent.strategy_id,)
        assert report.orders_working_at_broker == ()
        assert report.agrees is False
        assert "the broker was asked" in report.describe()

    def test_a_working_order_is_reported_as_working_and_not_as_absent(
        self, tmp_path: Path
    ) -> None:
        """The false claim this lane exists to remove.

        The live run reported ``ORDERS ABSENT -- transmitted, and the broker is
        not working them``. The broker *was* working it. The conservative
        outcome was right and the stated reason was wrong, and an operator
        acting on that sentence would go looking for a fill that never happened.
        """
        store = store_for(tmp_path)
        intent = seed_in_flight_position(store, order_id=896, perm_id=1_151_642_162)

        report = store.reconcile_against_broker(
            (("SPY", -1, 100.0),),
            checked_at=NOW,
            broker_orders=(
                BrokerTrade(
                    BrokerOrder(
                        order_id=896,
                        perm_id=1_151_642_162,
                        order_ref=str(intent.strategy_id),
                    )
                ),
            ),
        )

        assert report.orders_working_at_broker == (intent.strategy_id,)
        assert report.orders_absent_at_broker == ()
        assert report.orders_unverified_at_broker == ()
        assert "the broker IS working them" in report.describe()
        assert "is not working them" not in report.describe()

    def test_a_working_order_survives_a_restart_that_reassigned_the_order_id(
        self, tmp_path: Path
    ) -> None:
        """Matched by ``permId`` when ``orderId`` no longer means anything.

        A reconnect renumbers ``orderId`` from scratch. The store still holds
        the old one, and the only identifier that connects the two sessions is
        ``permId`` -- which is why the precedence is permId, then orderRef, then
        orderId, and why a match on the first is enough on its own.
        """
        store = store_for(tmp_path)
        intent = seed_in_flight_position(store, order_id=3, perm_id=1_151_642_162)

        report = store.reconcile_against_broker(
            (("SPY", -1, 100.0),),
            checked_at=NOW,
            broker_orders=(BrokerOrder(order_id=814, perm_id=1_151_642_162),),
        )

        assert report.orders_working_at_broker == (intent.strategy_id,)
        assert report.orders_absent_at_broker == ()
        assert report.orders_unknown_to_store == ()

    def test_a_working_order_matches_on_order_ref_before_any_perm_id_exists(
        self, tmp_path: Path
    ) -> None:
        """The middle rung of the precedence, exercised on its own.

        Between submission and the broker assigning a ``permId`` there is a
        window where ``orderRef`` is the only durable identity the order has --
        and it is durable because we chose it. Without this rung, an order in
        that window reconciles as absent.
        """
        store = store_for(tmp_path)
        intent = seed_in_flight_position(store, order_id=41, perm_id=None)

        report = store.reconcile_against_broker(
            (("SPY", -1, 100.0),),
            checked_at=NOW,
            broker_orders=(
                BrokerOrder(order_id=812, perm_id=770, order_ref=str(intent.strategy_id)),
            ),
        )

        assert report.orders_working_at_broker == (intent.strategy_id,)
        assert report.orders_absent_at_broker == ()
        assert report.orders_unknown_to_store == ()

    def test_not_asking_is_not_the_same_as_asking_and_finding_nothing(
        self, tmp_path: Path
    ) -> None:
        """``None`` and ``()`` are different answers, and the report says so.

        This is the defect at the root of the false claim. The old default was
        ``()``, so a caller that never enumerated open orders got a report
        asserting the broker was not working them -- an assertion about a
        question nobody asked.
        """
        store = store_for(tmp_path)
        intent = seed_in_flight_position(store, order_id=41, perm_id=880)

        report = store.reconcile_against_broker((("SPY", -1, 100.0),), checked_at=NOW)

        assert report.orders_unverified_at_broker == (intent.strategy_id,)
        assert report.orders_absent_at_broker == ()
        assert report.agrees is False
        assert "never enumerated" in report.describe()

    def test_a_reassigned_order_id_still_matches_on_perm_id(
        self, tmp_path: Path
    ) -> None:
        """``orderId`` is client-assigned and unique only within a session, so a
        reconciler that trusted it after a reconnect would match this session's
        order 3 against the previous session's unrelated order 3. ``permId`` is
        the durable one, and matching on it is what keeps the position out of
        ``orders_absent_at_broker``.

        A match on either identifier is a match. An earlier version differenced
        the two id spaces independently, so this same order was *also* reported
        as unknown to the store by its reassigned ``orderId`` -- a phantom
        disagreement that blocked new entries permanently after any reconnect.
        """
        store = store_for(tmp_path)
        seed_in_flight_position(store, order_id=3, perm_id=777)

        report = store.reconcile_against_broker(
            (("SPY", -1, 100.0),),
            checked_at=NOW,
            broker_orders=(BrokerOrder(order_id=91, perm_id=777),),
        )

        assert report.orders_absent_at_broker == ()
        assert report.orders_unknown_to_store == ()

    def test_a_clean_book_with_matching_orders_agrees(self, tmp_path: Path) -> None:
        """The control. Without it every disagreement above could be produced by
        the mere presence of a position rather than by the mismatch under test."""
        store = store_for(tmp_path)
        seed_open_position(store, dte=40, order_id=42, perm_id=8042)

        report = store.reconcile_against_broker(
            (("SPY", -1, 100.0),),
            checked_at=NOW,
            broker_orders=(BrokerOrder(order_id=42, perm_id=8042),),
        )

        assert report.orders_unknown_to_store == ()
        assert report.orders_absent_at_broker == ()
        assert report.agrees is True, report.describe()

    def test_no_broker_orders_at_all_is_not_a_disagreement(
        self, tmp_path: Path
    ) -> None:
        """The default. A caller that cannot enumerate open orders must not be
        told the book disagrees for that reason alone."""
        store = store_for(tmp_path)
        seed_open_position(store, dte=40, order_id=42, perm_id=8042)

        report = store.reconcile_against_broker(
            (("SPY", -1, 100.0),), checked_at=NOW
        )

        assert report.agrees is True, report.describe()


# ===========================================================================
# I. Entries blocked, exits not
# ===========================================================================


class TestExitsWorkWhileEntriesAreBlocked:
    def test_a_due_exit_transmits_while_an_unresolved_order_blocks_entries(
        self, tmp_path: Path
    ) -> None:
        """The asymmetry with teeth.

        An unresolved order is a reason not to take on more risk. It is not a
        reason to sit in a healthy position through expiration week -- the
        blocked entry and the permitted exit come from the same fact, and
        reading it as a reason to freeze everything turns a bookkeeping problem
        into a market one.
        """
        ib = ScriptedIB(scripts=(SCRIPT_CLEAN_FILL,))
        store = store_for(tmp_path)
        seed_uncertain_position(store, dte=40)
        healthy = seed_open_position(store, dte=10)

        report = run_pass(
            FakeBroker(ib=ib, positions=(("SPY", -1, 100.0),)),
            gate_for(tmp_path),
            store,
            armed=True,
        )

        assert len(ib.placed) == 1, report.describe()
        assert [t.action for t in report.transmissions] == [StrategyAction.CLOSE]
        _bag, order = ib.placed[0]
        assert order.orderRef != str(healthy.strategy_id)  # the close is its own id

        assert report.entered is False
        assert "RUNNER_UNRESOLVED_ORDER" in report.refusal_codes
        assert report.candidate is None

    def test_the_exit_closes_the_healthy_position_and_not_the_uncertain_one(
        self, tmp_path: Path
    ) -> None:
        """A position whose outcome is unknown must not be closed: there may be
        nothing there, and a close against nothing is an opening trade in the
        other direction."""
        ib = ScriptedIB(scripts=(SCRIPT_CLEAN_FILL,))
        store = store_for(tmp_path)
        unresolved = seed_uncertain_position(store, dte=40)
        healthy = seed_open_position(store, dte=10)

        report = run_pass(
            FakeBroker(ib=ib, positions=(("SPY", -1, 100.0),)),
            gate_for(tmp_path),
            store,
            armed=True,
        )

        acted = [d for d in report.decisions if d.acts]
        assert [d.position_id for d in acted] == [healthy.strategy_id]
        assert [d.action for d in acted] == [ManagementAction.CLOSE_DTE]

        held = [d for d in report.decisions if not d.acts]
        assert [d.position_id for d in held] == [unresolved.strategy_id]

        closed = store.get(healthy.strategy_id)
        assert closed is not None and closed.state is PositionState.CLOSED
        still_unknown = store.get(unresolved.strategy_id)
        assert still_unknown is not None
        assert still_unknown.state is PositionState.UNCERTAIN

    def test_an_unresolved_order_alone_never_reaches_the_entry_path(
        self, tmp_path: Path
    ) -> None:
        """Proves the block above is the unresolved order rather than anything
        the candidate builder happened to refuse: no candidate is even built."""
        ib = ScriptedIB(scripts=(SCRIPT_CLEAN_FILL,))
        store = store_for(tmp_path)
        seed_uncertain_position(store, dte=40)

        report = run_pass(
            FakeBroker(ib=ib, positions=(("SPY", -1, 100.0),)),
            gate_for(tmp_path),
            store,
            armed=True,
        )

        assert ib.placed == []
        assert report.candidate is None
        assert report.risk is None
        assert "RUNNER_UNRESOLVED_ORDER" in report.refusal_codes


# ===========================================================================
# The log records the journey, not just the destination
# ===========================================================================


class TestIntermediateTransitionsArePersisted:
    """G8: persistence is callback-driven, not final-snapshot-driven.

    Before the lifecycle sink, ``place_combo`` wrote a single snapshot after the
    poll loop finished, so a partial fill that later completed left no trace of
    ever having been partial -- and a crash between the two lost the fill
    entirely. These tests fail if per-poll emission is removed, which is what
    makes them evidence rather than decoration.
    """

    def test_a_partial_that_later_fills_leaves_both_transitions(
        self, tmp_path: Path
    ) -> None:
        """The whole point. A final-snapshot-only implementation records only
        OPEN_FILLED, and the fact that one lot was in the market before the rest
        arrived is gone for good."""
        ib = ScriptedIB(scripts=(SCRIPT_PARTIAL_THEN_FILLED,))
        store = store_for(tmp_path)
        run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        names = event_names(store)
        assert PositionEvent.OPEN_PARTIAL.value in names, names
        assert PositionEvent.OPEN_FILLED.value in names, names
        assert names.index(PositionEvent.OPEN_PARTIAL.value) < names.index(
            PositionEvent.OPEN_FILLED.value
        )

    def test_the_submission_is_recorded_before_any_broker_response(
        self, tmp_path: Path
    ) -> None:
        """If the process dies on the line after placeOrder returns, the store
        must already know an order exists for this strategy."""
        ib = ScriptedIB(scripts=(SCRIPT_CLEAN_FILL,))
        store = store_for(tmp_path)
        run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        names = event_names(store)
        assert names[0] == PositionEvent.OPEN_SUBMITTED.value
        assert PositionEvent.OPEN_ACKNOWLEDGED.value in names

    def test_a_cancel_after_partial_keeps_the_partial_transition(
        self, tmp_path: Path
    ) -> None:
        """Terminal does not mean nothing happened. The contracts that filled
        before the cancel must appear in the history, not only in the final
        exposure figure."""
        ib = ScriptedIB(scripts=(SCRIPT_PARTIAL_THEN_CANCELLED,))
        store = store_for(tmp_path)
        run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        names = event_names(store)
        assert PositionEvent.OPEN_PARTIAL.value in names, names
        held = store.open_positions()
        assert len(held) == 1
        assert held[0].filled_quantity > D("0")

    def test_duplicate_callbacks_do_not_duplicate_transitions(
        self, tmp_path: Path
    ) -> None:
        """IBKR re-sends status callbacks freely. Ingestion is idempotent, so
        polling faster than the broker moves costs nothing in the log."""
        ib = ScriptedIB(scripts=(SCRIPT_DUPLICATE_PARTIAL,))
        store = store_for(tmp_path)
        run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        names = event_names(store)
        assert names.count(PositionEvent.OPEN_PARTIAL.value) <= 2, names
        held = store.open_positions()
        assert len(held) == 1


# ===========================================================================
# What is on disk *while* the order is still working
# ===========================================================================


def _book_watcher(store: PositionStore) -> tuple[list[dict[str, Any]], Any]:
    """An observer that photographs the store before each poll is persisted.

    The list it fills is the durable state a process dying at that instant
    would have left behind, one entry per poll. Reading the store *after* the
    loop proves nothing about when anything was written -- a final-snapshot
    implementation ends up equally correct there, which is precisely why the
    original defect survived so long.
    """
    timeline: list[dict[str, Any]] = []

    def watch(poll: int) -> None:
        for strategy_id, position in store.positions().items():
            timeline.append(
                {
                    "poll": poll,
                    "strategy_id": strategy_id,
                    "state": position.state,
                    "order_id": position.open_order_id,
                    "perm_id": position.open_perm_id,
                    "filled": position.filled_quantity,
                }
            )

    return timeline, watch


class TestTheStoreDoesNotLagTheBroker:
    """Identity and exposure reach disk when observed, not when polling ends.

    The class above proves the *log* keeps every transition. These prove the
    stronger and more useful thing: that each fact was durable at the poll that
    observed it. Every assertion here reads the store from inside ``isDone()``,
    so it fails if persistence is moved back to after the loop even though the
    finished log would still look identical.
    """

    def test_the_broker_identifiers_are_durable_before_the_first_poll_answers(
        self, tmp_path: Path
    ) -> None:
        """``orderId`` and ``permId`` are the only things that let a restart find
        this order at the broker. They exist the instant ``placeOrder`` returns,
        so waiting for a terminal status to write them is a window in which a
        crash leaves an order nobody can look up."""
        store = store_for(tmp_path)
        timeline, watch = _book_watcher(store)
        ib = ScriptedIB(scripts=(SCRIPT_PARTIAL_THEN_FILLED,), observer=watch)

        run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        first = [row for row in timeline if row["poll"] == 0]
        assert first, timeline
        # The values the fake minted, not merely "not None" -- an assertion that
        # only checks presence passes against an implementation that writes a
        # placeholder.
        assert [row["order_id"] for row in first] == [70]
        assert [row["perm_id"] for row in first] == [8800]

    def test_a_partial_fill_is_durable_at_the_poll_that_observed_it(
        self, tmp_path: Path
    ) -> None:
        """One lot of three is in the market. If the socket drops on the next
        poll, the store must already say so -- this is the transition the
        pre-sink implementation lost, and losing it means a live spread recorded
        as an order that never filled."""
        store = store_for(tmp_path)
        timeline, watch = _book_watcher(store)
        ib = ScriptedIB(scripts=(SCRIPT_PARTIAL_THEN_FILLED,), observer=watch)

        run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        # The order ends completely filled at three. A store that only learned
        # the outcome after the loop would show 0 for every poll and then 3.
        assert any(row["filled"] == D("1") for row in timeline), timeline
        assert store.get(timeline[0]["strategy_id"]).filled_quantity == D("3")

    def test_the_partial_is_recorded_before_the_fill_that_supersedes_it(
        self, tmp_path: Path
    ) -> None:
        """Ordering, not just presence: the 1 must appear on an *earlier* poll
        than the 3, or the store is still learning both at the end."""
        store = store_for(tmp_path)
        timeline, watch = _book_watcher(store)
        ib = ScriptedIB(scripts=(SCRIPT_PARTIAL_THEN_FILLED,), observer=watch)

        run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        polls_at_one = [row["poll"] for row in timeline if row["filled"] == D("1")]
        polls_at_three = [row["poll"] for row in timeline if row["filled"] == D("3")]
        assert polls_at_one, timeline
        assert polls_at_three, timeline
        assert min(polls_at_one) < min(polls_at_three)

    def test_the_recorded_fill_never_decreases_across_the_whole_poll_sequence(
        self, tmp_path: Path
    ) -> None:
        """The monotonic property as a property, over every observation rather
        than over the two the other tests happen to name. IBKR re-sends
        callbacks and delivers fills out of order with respect to status, so a
        smaller number arriving later is ordinary and must never win."""
        store = store_for(tmp_path)
        timeline, watch = _book_watcher(store)
        ib = ScriptedIB(scripts=(SCRIPT_STALE_SMALLER_FILL,), observer=watch)

        run_pass(
            FakeBroker(ib=ib),
            gate_for(tmp_path),
            store,
            armed=True,
            policy=sized_policy(contracts=3),
        )

        seen = [row["filled"] for row in timeline]
        assert seen == sorted(seen), seen
        assert max(seen) == D("2"), seen

    def test_a_stale_earlier_status_does_not_re_fire_a_terminal_verdict(
        self, tmp_path: Path
    ) -> None:
        """The progression guard, which the fill-count guard cannot cover.

        A re-delivered ``Submitted`` after a cancel carries no new fill, so
        nothing about its *numbers* marks it as stale -- only its rank does.
        Let it through and the recorder's idea of the state walks backwards,
        and the next cancel reads as a fresh verdict: one order that failed
        once, recorded as having failed twice.
        """
        ib = ScriptedIB(scripts=(SCRIPT_STALE_STATUS_AFTER_TERMINAL,))
        store = store_for(tmp_path)

        run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        names = event_names(store)
        assert names.count(PositionEvent.OPEN_FAILED.value) == 1, names

    def test_a_crash_mid_poll_leaves_a_store_a_restart_reads_without_transmitting(
        self, tmp_path: Path
    ) -> None:
        """The end-to-end recovery property, with nothing hand-seeded.

        A real ``place_combo`` is interrupted mid-poll by a dropped socket, and
        then everything in memory is thrown away: the store object, the
        recorder and its cache, and the broker connection. A second pass is
        built from the **journal file alone**, against a fully healthy broker
        that would happily accept an order. It must send nothing.
        """
        path = tmp_path / "state" / "positions.jsonl"
        first_store = PositionStore(path)
        dropped = ScriptedIB(
            scripts=(SCRIPT_PARTIAL_STILL_WORKING,), connected_polls=2
        )

        run_pass(
            FakeBroker(ib=dropped),
            gate_for(tmp_path),
            first_store,
            armed=True,
            policy=sized_policy(contracts=3),
        )
        assert len(dropped.placed) == 1

        # The crash. Only the file survives.
        del first_store
        restarted = PositionStore(path)
        stranded = restarted.open_positions()
        assert len(stranded) == 1, [p.describe() for p in stranded]
        assert stranded[0].is_uncertain, stranded[0].describe()
        # The identifiers came from the interrupted poll loop, not from a test
        # fixture -- which is what makes the order findable at the broker.
        assert stranded[0].open_order_id == 70
        assert stranded[0].open_perm_id == 8800

        healthy = ScriptedIB(scripts=(SCRIPT_CLEAN_FILL,))
        report = run_pass(
            FakeBroker(ib=healthy, positions=(("SPY", -1, 100.0),)),
            gate_for(tmp_path),
            restarted,
            armed=True,
        )

        assert healthy.placed == [], report.describe()
        assert "RUNNER_UNRESOLVED_ORDER" in report.refusal_codes


# ===========================================================================
# The closing order has its own fill count
# ===========================================================================


def _replayed_close_trade() -> ScriptedTrade:
    """The partial-close observation re-delivered verbatim after a restart.

    Carries the identifiers ``ScriptedIB`` minted for the exit, because that is
    what a reconnecting client would see: the same order, the same permId, the
    same one lot already filled. Nothing here is new information, and the
    recorder has to recognise that from the store alone.
    """
    return ScriptedTrade(
        (partial(1.0),),
        order_id=70,
        perm_id=8800,
        total_quantity=3.0,
        limit_price=-0.5,
    )


class TestTheClosingFillIsPersistedToo:
    """An exit can partially fill, and until now the amount was thrown away.

    ``record_partial_fill(closing=True)`` wrote the quantity to the journal and
    the replay ignored it, so a cancelled-after-partial exit reloaded as
    "closing, amount unknown": the contracts that got out and the ones still
    held were indistinguishable on disk. That is the mirror of the opening-side
    defect the ledger records as C21, on the side where the position is being
    retired rather than taken on.
    """

    def _partially_closed(self, tmp_path: Path) -> tuple[PositionStore, UUID]:
        """Run a real exit that fills one of three lots and is then cancelled."""
        ib = ScriptedIB(scripts=(SCRIPT_PARTIAL_THEN_CANCELLED,))
        store = store_for(tmp_path)
        # Blocks the entry path, so the single scripted order is the exit and
        # the assertions below cannot be reading a fresh opening order.
        seed_uncertain_position(store, dte=40)
        held = seed_open_position(store, dte=10, quantity=3)

        run_pass(
            FakeBroker(ib=ib, positions=(("SPY", -3, 100.0),)),
            gate_for(tmp_path),
            store,
            armed=True,
        )
        assert len(ib.placed) == 1
        return store, held.strategy_id

    def test_a_partial_close_records_how_much_of_the_exit_filled(
        self, tmp_path: Path
    ) -> None:
        store, strategy_id = self._partially_closed(tmp_path)

        assert PositionEvent.CLOSE_PARTIAL.value in event_names(store)
        position = store.get(strategy_id)
        assert position is not None
        assert position.state is PositionState.CLOSING
        assert position.close_filled_quantity == D("1")

    def test_the_contracts_still_held_are_derivable_after_a_partial_close(
        self, tmp_path: Path
    ) -> None:
        """Three were opened and one got out, so two are still in the market.

        Before the closing fill was persisted this number did not exist at all:
        ``filled_quantity`` still said three and nothing recorded the one.
        """
        store, strategy_id = self._partially_closed(tmp_path)

        position = store.get(strategy_id)
        assert position is not None
        assert position.filled_quantity == D("3")
        assert position.remaining_quantity == D("2")
        assert position.is_partially_closed is True

    def test_the_closing_fill_survives_a_restart(self, tmp_path: Path) -> None:
        """It has to round-trip through ``to_record``/``from_record`` as well as
        through the replay, or a restart silently resets it to zero."""
        store, strategy_id = self._partially_closed(tmp_path)

        restarted = PositionStore(store.path)
        position = restarted.get(strategy_id)
        assert position is not None
        assert position.close_filled_quantity == D("1")
        assert position.remaining_quantity == D("2")

    def test_a_duplicate_closing_partial_does_not_double_the_closed_amount(
        self, tmp_path: Path
    ) -> None:
        """Store-level, mirroring the opening-side test above: the guarantee has
        to hold on the *disk replay*, because the recorder's cache dies with the
        process and a restart replays the log with nothing in memory."""
        store = store_for(tmp_path)
        intent = seed_open_position(store, dte=10, quantity=3)
        store.record_close_submitted(intent.strategy_id, at=NOW)
        for _ in range(2):
            store.record_partial_fill(
                intent.strategy_id, at=NOW, filled_quantity=D("1"), closing=True
            )

        position = store.get(intent.strategy_id)
        assert position is not None
        assert position.close_filled_quantity == D("1")

    def test_a_stale_smaller_closing_partial_never_walks_it_backwards(
        self, tmp_path: Path
    ) -> None:
        store = store_for(tmp_path)
        intent = seed_open_position(store, dte=10, quantity=3)
        store.record_close_submitted(intent.strategy_id, at=NOW)
        store.record_partial_fill(
            intent.strategy_id, at=NOW, filled_quantity=D("2"), closing=True
        )
        store.record_partial_fill(
            intent.strategy_id, at=NOW, filled_quantity=D("1"), closing=True
        )

        position = store.get(intent.strategy_id)
        assert position is not None
        assert position.close_filled_quantity == D("2")
        assert position.remaining_quantity == D("1")

    def test_a_completed_close_retires_everything_that_was_held(
        self, tmp_path: Path
    ) -> None:
        """A position that partially filled its exit and then completed it holds
        nothing. Leaving the closing count at the partial would report contracts
        nobody owns."""
        store = store_for(tmp_path)
        intent = seed_open_position(store, dte=10, quantity=3)
        store.record_close_submitted(intent.strategy_id, at=NOW)
        store.record_partial_fill(
            intent.strategy_id, at=NOW, filled_quantity=D("1"), closing=True
        )
        store.record_close_filled(intent.strategy_id, at=NOW, closing_debit=D("0.50"))

        position = store.get(intent.strategy_id)
        assert position is not None
        assert position.state is PositionState.CLOSED
        assert position.close_filled_quantity == D("3")
        assert position.remaining_quantity == D("0")
        assert position.is_partially_closed is False

    def test_a_recorder_built_after_a_restart_does_not_re_append_a_closing_fill(
        self, tmp_path: Path
    ) -> None:
        """The seeding half of the same property.

        ``LifecycleRecorder`` seeded only the *opening* order, so a restart
        mid-close came back believing nothing had filled on the exit and the
        first callback after recovery re-appended a fill the store already
        held. Re-delivering the exact observation must now write nothing.
        """
        store, strategy_id = self._partially_closed(tmp_path)
        before = len(event_names(store))

        restarted = PositionStore(store.path)
        recorder = LifecycleRecorder(restarted)
        wrote = recorder.observe(
            strategy_id,
            snapshot_from_trade(
                _replayed_close_trade(),
                observed_at=NOW,
                quantity=3,
            ),
            closing=True,
        )

        assert wrote is False
        assert len(event_names(restarted)) == before


# ===========================================================================
# K. Order control, end to end through run_once
# ===========================================================================


class TestAWorkingEntryIsNotLeftResting:
    """The whole lane, exercised the way the failure actually happened.

    ``run_once`` sends an entry, the broker accepts it and works it, and nothing
    fills. Before this lane that ended the pass with an unresolved order resting
    in the book and every later entry refused, permanently, with no process able
    to clear it.
    """

    def test_the_entry_is_worked_and_then_cancelled(self, tmp_path: Path) -> None:
        ib = ScriptedIB(scripts=(SCRIPT_WORKING_FOREVER,))
        store = store_for(tmp_path)

        report = run_pass(FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True)

        assert report.reprice is not None, report.describe()
        assert report.reprice.stop.value == "REPRICE_EXHAUSTED"
        assert report.reprice.attempts == 4
        assert report.reprice.cancelled is True
        # Five orders, five cancels, and nothing left working at the broker.
        assert len(ib.placed) == 5
        assert len(ib.cancelled) == 5
        assert ib.openTrades() == ()
        # Each rung asked for less credit than the one before it. The limits are
        # negative -- ``build_combo``'s credit convention -- so ascending order
        # is a shrinking credit.
        limits = [order.lmtPrice for _contract, order in ib.placed]
        assert limits == sorted(limits), limits
        # Nothing filled, so nothing is open -- and the log still replays.
        assert store.open_positions() == []
        assert store.integrity_errors() == ()

    def test_the_pass_says_so_rather_than_reporting_an_unknown_outcome(
        self, tmp_path: Path
    ) -> None:
        """What the operator is told.

        The old report said the order's outcome was unknown, which was true and
        useless. The new one names what was done about it.
        """
        ib = ScriptedIB(scripts=(SCRIPT_WORKING_FOREVER,))
        report = run_pass(
            FakeBroker(ib=ib), gate_for(tmp_path), store_for(tmp_path), armed=True
        )

        assert any("entry order worked" in blocker for blocker in report.blockers), (
            report.blockers
        )
        assert report.entered is False
        assert "REPRICE_EXHAUSTED" in report.describe()

    def test_the_ladder_can_be_switched_off_and_the_old_behaviour_returns(
        self, tmp_path: Path
    ) -> None:
        """The mutation half. One argument different, and the order rests.

        This is what proves the ladder above is what cancelled the order -- not
        the fake, and not the runner doing it anyway.
        """
        ib = ScriptedIB(scripts=(SCRIPT_WORKING_FOREVER,))
        gate = gate_for(tmp_path)
        store = store_for(tmp_path)

        report = run_once(
            FakeBroker(ib=ib),
            gate=gate,
            journal=gate.journal,
            store=store,
            policy=RiskPolicy(),
            armed=True,
            symbol="SPY",
            bias=Bias.BULLISH,
            market_data=FakeMarketDataPort(),
            portfolio=FakePortfolioPort(),
            now=NOW,
            today=TODAY,
            account="DU1234567",
            verifier=reviewer.approving_gate(tmp_path / "verifier"),
            approval_context=reviewer.approval_context(),
            reprice=None,
        )

        assert report.reprice is None
        assert len(ib.placed) == 1
        assert ib.cancelled == []
        assert ib.openTrades() != (), "the order is still resting at the broker"


class TestTheNextPassSeesTheWorkingOrder:
    def test_reconciliation_reads_open_trades_and_reports_the_order_as_working(
        self, tmp_path: Path
    ) -> None:
        """A restart, with the order still live at the broker.

        The store holds an ``OPENING`` position whose ``orderId`` was assigned
        in the previous session. The broker reports the same order under a
        renumbered ``orderId`` and the durable ``permId``, and the pass must say
        the broker IS working it.
        """
        store = store_for(tmp_path)
        intent = seed_in_flight_position(store, order_id=3, perm_id=1_151_642_162)
        ib = ScriptedIB(
            working_orders=(
                BrokerTrade(
                    BrokerOrder(
                        order_id=814,
                        perm_id=1_151_642_162,
                        order_ref=str(intent.strategy_id),
                    )
                ),
            )
        )

        report = run_pass(
            FakeBroker(ib=ib, positions=(("SPY", -1, 100.0),)),
            gate_for(tmp_path),
            store,
            armed=True,
        )

        assert report.reconciliation is not None
        assert report.reconciliation.orders_working_at_broker == (intent.strategy_id,)
        assert report.reconciliation.orders_absent_at_broker == ()
        assert report.reconciliation.orders_unverified_at_broker == ()
        assert "is not working them" not in report.describe()
        # Still no new risk: a stranded OPENING is a disagreement whatever the
        # reason. The conservative outcome was always right; only the stated
        # reason was wrong.
        assert report.entered is False
        assert report.reconciliation_outcome.may_open_new_risk is False

    def test_an_unrelated_resting_order_does_not_block_the_pass(
        self, tmp_path: Path
    ) -> None:
        """The trap, end to end.

        A manual ticket sitting on the same paper account must not turn every
        subsequent pass into a DISAGREEMENT. Nothing about it is ours, so the
        reconciler ignores it and the pass proceeds to its ordinary entry.
        """
        ib = ScriptedIB(
            scripts=(SCRIPT_CLEAN_FILL,),
            working_orders=(BrokerOrder(order_id=77, perm_id=4242, order_ref="MANUAL"),),
        )

        report = run_pass(
            FakeBroker(ib=ib), gate_for(tmp_path), store_for(tmp_path), armed=True
        )

        assert report.reconciliation is not None
        assert report.reconciliation.orders_unknown_to_store == ()
        assert report.reconciliation_outcome.may_open_new_risk is True
        assert report.entered is True, report.describe()


class TestAReopenedPartialCloseIsNotResoldInFull:
    """C21's failure reached by a different road, on the *transmitting* path.

    The existing C21 test covers a partial *open*: ordered 3, filled 1, close 1.
    This is the other shape. The open fills in full, the CLOSE partially fills,
    and then the close fails -- which returns the position to OPEN, where
    ``decide_management_action`` re-decides it from scratch.

    At that point ``manageable_quantity`` still reports 3, because it subtracts
    what the *open* filled and nothing that the *close* already retired. Sizing
    the exit off it sells two contracts that are no longer held.

    The lifecycle's hold-while-CLOSING guard is why this looked safe and is not:
    CLOSE_FAILED ends the CLOSING state.
    """

    def test_the_second_exit_closes_only_the_unclosed_remainder(
        self, tmp_path: Path
    ) -> None:
        store = store_for(tmp_path)
        intent = spread_intent(
            expiration=TODAY + dt.timedelta(days=10), quantity=3
        )
        store.record_open_submitted(intent, at=NOW, buying_power_reserved=BPR)
        store.record_open_filled(
            intent.strategy_id, at=NOW, filled_credit=D("1.50"), filled_quantity=D("3")
        )
        store.record_close_submitted(intent.strategy_id, at=NOW, target_debit=D("0.75"))
        store.record_partial_fill(
            intent.strategy_id, at=NOW, filled_quantity=D("2"), closing=True
        )
        store.record_close_failed(
            intent.strategy_id, at=NOW, reason="the closing order was cancelled"
        )

        position = store.get(intent.strategy_id)
        assert position is not None
        # The premise, asserted so this cannot pass for the wrong reason.
        assert position.state is PositionState.OPEN, "it must be re-decided"
        assert position.manageable_quantity == 3, "the misleading number"
        assert position.remaining_quantity == D("1"), "the true holding"

        ib = ScriptedIB(scripts=(SCRIPT_CLEAN_FILL,))
        report = run_pass(
            FakeBroker(ib=ib, positions=()), gate_for(tmp_path), store, armed=True
        )

        assert len(ib.placed) == 1, report.describe()
        _bag, order = ib.placed[0]
        assert order.totalQuantity == 1, (
            "the exit must close the 1 contract still held, not the 3 the open "
            "filled -- selling 2 unheld contracts is a naked short"
        )
