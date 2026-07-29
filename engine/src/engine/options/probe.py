"""Non-transmitting market-data capability probe.

Answers one question with evidence rather than documentation: **does IBKR send
option model greeks on a delayed subscription?** IBKR documents delayed model
computations (tick 83, "Computed Greeks and model's implied volatility based on
delayed stock and option prices") while also stating it no longer offers delayed
quotation information on U.S. equities to IBLLC clients. Those two cannot both
be fully true for a US equity option, and which one wins decides whether the
adapter work can be developed against real numbers before a subscription lands.

This module transmits nothing. It has no import of, and no reference to, any
order-placement call -- ``test_options_probe.py`` asserts that mechanically, so
the guarantee survives someone editing this file later.

**The callback-observation layer.** ``Ticker.marketDataType`` is declared ``= 1``
and written only when TWS sends its market-data-type message, so reading the
field cannot distinguish "server said live" from "server said nothing". The
probe therefore wraps ``ib.wrapper.marketDataType`` and
``ib.wrapper.tickOptionComputation`` at runtime, records every callback with the
contract it arrived for, and delegates to the original. Nothing on disk is
patched and the wrappers are removed in a ``finally``.

**Per-contract, not per-session.** IBKR: "a market data subscription for both
the underlying and derivative are necessary for options greeks data." A session
that got a callback for SPY has established nothing about the option contracts,
so every observation here is keyed by contract id.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..errors import EngineError
from .marketdata import (
    MarketDataType,
    OptionGreeks,
    normalize_greek,
)

__all__ = [
    "ProbeOutcome",
    "ContractObservation",
    "ProbeReport",
    "CallbackRecorder",
    "classify",
    "run_market_data_probe",
]


class ProbeOutcome(str, Enum):
    """The five states this probe is allowed to conclude."""

    DELAYED_GREEKS_AVAILABLE = "DELAYED_GREEKS_AVAILABLE"
    DELAYED_QUOTES_ONLY = "DELAYED_QUOTES_ONLY"
    NO_DELAYED_OPTION_DATA = "NO_DELAYED_OPTION_DATA"
    UNKNOWN_CALLBACK_STATE = "UNKNOWN_CALLBACK_STATE"
    PROBE_ERROR = "PROBE_ERROR"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


@dataclass
class ContractObservation:
    """Everything observed for one contract during one subscription."""

    label: str
    kind: str  # "underlying" | "option"
    con_id: int = 0
    data_type_callbacks: list[int] = field(default_factory=list)
    greek_callback_count: int = 0
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    close: float | None = None
    greeks: OptionGreeks | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def callback_received(self) -> bool:
        """True only if the *server* sent a market-data-type message for this
        contract. Never inferred from the ticker field, which defaults to 1."""
        return bool(self.data_type_callbacks)

    @property
    def reported_type(self) -> int | None:
        return self.data_type_callbacks[-1] if self.data_type_callbacks else None

    @property
    def has_two_sided_quote(self) -> bool:
        return self.bid is not None and self.ask is not None

    @property
    def has_any_price(self) -> bool:
        return any(v is not None for v in (self.bid, self.ask, self.last, self.close))

    @property
    def has_valid_delta(self) -> bool:
        """Reported separately from greeks presence on purpose: ib_async assigns
        the computation even when every field sanitizes away."""
        return self.greeks is not None and self.greeks.has_valid_delta

    def to_record(self) -> dict[str, Any]:
        greeks = self.greeks
        return {
            "label": self.label,
            "kind": self.kind,
            "con_id": self.con_id,
            "callback_received": self.callback_received,
            "reported_type": self.reported_type,
            "data_type_callbacks": list(self.data_type_callbacks),
            "greek_callbacks": self.greek_callback_count,
            "bid": self.bid,
            "ask": self.ask,
            "last": self.last,
            "close": self.close,
            "greeks_object_present": greeks is not None,
            "delta": str(greeks.delta) if greeks and greeks.delta is not None else None,
            "delta_valid": self.has_valid_delta,
            "implied_volatility": (
                str(greeks.implied_volatility)
                if greeks and greeks.implied_volatility is not None
                else None
            ),
            "underlying_price_in_computation": (
                str(greeks.underlying_price)
                if greeks and greeks.underlying_price is not None
                else None
            ),
            "theta": str(greeks.theta) if greeks and greeks.theta is not None else None,
            "vega": str(greeks.vega) if greeks and greeks.vega is not None else None,
            "errors": list(self.errors),
        }


@dataclass
class ProbeReport:
    outcome: ProbeOutcome
    requested_type: int
    account: str
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    underlying: ContractObservation | None = None
    options: list[ContractObservation] = field(default_factory=list)
    expiration: str | None = None
    dte: int | None = None
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return {
            "event": "capability_probe",
            "probe": "option_market_data",
            "outcome": self.outcome.value,
            "requested_type": self.requested_type,
            "account": self.account,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "expiration": self.expiration,
            "dte": self.dte,
            "underlying": self.underlying.to_record() if self.underlying else None,
            "options": [o.to_record() for o in self.options],
            "errors": list(self.errors),
            "notes": list(self.notes),
        }

    def describe(self) -> str:
        lines: list[str] = []
        lines.append(f"outcome            {self.outcome.value}")
        lines.append(
            f"requested type     {self.requested_type} "
            f"({MarketDataType(self.requested_type).name.lower() if self.requested_type in (1, 2, 3, 4) else '?'})"
        )
        lines.append(f"account            {self.account}")
        if self.expiration:
            lines.append(f"expiration         {self.expiration}  ({self.dte} DTE)")
        lines.append("")

        def row(obs: ContractObservation) -> str:
            reported = obs.reported_type if obs.callback_received else "NONE"
            quote = (
                f"bid={obs.bid} ask={obs.ask}"
                if obs.has_any_price
                else "no prices"
            )
            delta = (
                str(obs.greeks.delta)
                if obs.greeks is not None and obs.greeks.delta is not None
                else "-"
            )
            return (
                f"  {obs.label:<28} conId={obs.con_id:<10} "
                f"reported={str(reported):<5} greek_cbs={obs.greek_callback_count:<3} "
                f"delta={delta:<8} {quote}"
            )

        if self.underlying is not None:
            lines.append("underlying")
            lines.append(row(self.underlying))
        if self.options:
            lines.append("options")
            lines.extend(row(o) for o in self.options)
        if self.errors:
            lines.append("")
            lines.append("broker messages")
            lines.extend(f"  {e}" for e in self.errors)
        if self.notes:
            lines.append("")
            lines.extend(f"note: {n}" for n in self.notes)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Callback observation
# ---------------------------------------------------------------------------


class CallbackRecorder:
    """Wraps two wrapper methods to observe callbacks, then puts them back.

    This is the layer that makes "did the server actually say anything?"
    answerable. Reading ``ticker.marketDataType`` cannot answer it, because the
    field is initialised to 1 whether or not a message ever arrived.
    """

    def __init__(self, ib: Any) -> None:
        self.ib = ib
        self._wrapper = getattr(ib, "wrapper", None)
        self._original_market_data_type: Any = None
        self._original_tick_option: Any = None
        self.data_types: dict[int, list[int]] = {}
        self.greek_counts: dict[int, int] = {}
        self.latest_greeks: dict[int, Any] = {}
        self.unmapped_events: int = 0

    def _con_id_for(self, req_id: int) -> int | None:
        """Map a request id back to the contract it belongs to."""
        wrapper = self._wrapper
        if wrapper is None:
            return None
        ticker = getattr(wrapper, "reqId2Ticker", {}).get(req_id)
        contract = getattr(ticker, "contract", None)
        con_id = getattr(contract, "conId", None)
        return con_id if isinstance(con_id, int) and con_id else None

    def install(self) -> None:
        wrapper = self._wrapper
        if wrapper is None:  # pragma: no cover - only if ib_async changes shape
            return

        self._original_market_data_type = wrapper.marketDataType
        self._original_tick_option = wrapper.tickOptionComputation

        original_mdt = self._original_market_data_type
        original_toc = self._original_tick_option

        def record_market_data_type(req_id: int, market_data_id: int) -> Any:
            con_id = self._con_id_for(req_id)
            if con_id is None:
                self.unmapped_events += 1
            else:
                self.data_types.setdefault(con_id, []).append(int(market_data_id))
            return original_mdt(req_id, market_data_id)

        def record_tick_option(req_id: int, tick_type: int, *args: Any) -> Any:
            con_id = self._con_id_for(req_id)
            if con_id is not None:
                self.greek_counts[con_id] = self.greek_counts.get(con_id, 0) + 1
            # Delegate first so ib_async builds the OptionComputation and
            # assigns it; then read what it produced rather than re-parsing the
            # positional wire arguments, whose order is the library's business.
            result = original_toc(req_id, tick_type, *args)
            ticker = getattr(wrapper, "reqId2Ticker", {}).get(req_id)
            model = getattr(ticker, "modelGreeks", None)
            if con_id is not None and model is not None:
                self.latest_greeks[con_id] = model
            return result

        wrapper.marketDataType = record_market_data_type
        wrapper.tickOptionComputation = record_tick_option

    def remove(self) -> None:
        wrapper = self._wrapper
        if wrapper is None:  # pragma: no cover
            return
        if self._original_market_data_type is not None:
            wrapper.marketDataType = self._original_market_data_type
        if self._original_tick_option is not None:
            wrapper.tickOptionComputation = self._original_tick_option


# ---------------------------------------------------------------------------
# Outcome classification -- pure, so it is testable without a broker
# ---------------------------------------------------------------------------


def classify(
    underlying: ContractObservation | None,
    options: Sequence[ContractObservation],
    *,
    fatal_error: bool = False,
) -> ProbeOutcome:
    """Decide the outcome from what was observed. No I/O, no ib_async.

    Order matters. An error beats everything; silence beats absence of data,
    because "we heard nothing" and "there is nothing" are different findings and
    conflating them would report a capability failure for what may be a
    connection or entitlement-acknowledgement problem.
    """
    if fatal_error:
        return ProbeOutcome.PROBE_ERROR
    if not options:
        return ProbeOutcome.PROBE_ERROR

    heard_from_server = any(o.callback_received for o in options) or (
        underlying is not None and underlying.callback_received
    )
    if not heard_from_server:
        return ProbeOutcome.UNKNOWN_CALLBACK_STATE

    if any(o.has_valid_delta for o in options):
        return ProbeOutcome.DELAYED_GREEKS_AVAILABLE

    if any(o.has_any_price for o in options):
        return ProbeOutcome.DELAYED_QUOTES_ONLY

    return ProbeOutcome.NO_DELAYED_OPTION_DATA


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


def _dte(expiry: str, today: dt.date) -> int:
    return (dt.datetime.strptime(expiry, "%Y%m%d").date() - today).days


def _f(value: Any) -> float | None:
    """A float, or None for NaN and IBKR's absent markers."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    if abs(number) >= 1.7976931348623157e308:
        return None
    return number


def run_market_data_probe(
    broker: Any,
    *,
    symbol: str = "SPY",
    market_data_type: int = int(MarketDataType.DELAYED),
    target_dte: int = 45,
    strike_count: int = 4,
    settle_seconds: float = 12.0,
    account: str = "",
) -> ProbeReport:
    """Subscribe to an underlying and a few near-the-money options, observe.

    Transmits nothing. The only broker calls made are qualification, chain
    enumeration, market-data subscription and cancellation.

    ``market_data_type`` is requested exactly once, at the start, and no other
    type is requested in this process -- which is the condition that makes a
    delayed result trustworthy. Run the live variant as a separate invocation.
    """
    started = _utcnow()
    report = ProbeReport(
        outcome=ProbeOutcome.PROBE_ERROR,
        requested_type=market_data_type,
        account=account,
        started_at=started,
    )

    ib = broker.ib
    recorder = CallbackRecorder(ib)
    subscribed: list[Any] = []
    fatal = False

    def on_error(req_id: int, code: int, message: str, *_: Any) -> None:
        report.errors.append(f"{code}: {message}")

    try:
        from ib_async import Option, Stock  # noqa: PLC0415 - optional dependency

        ib.errorEvent += on_error
        recorder.install()

        # One data-type request, before any subscription. Nothing else in this
        # process asks for a different type.
        ib.reqMarketDataType(market_data_type)

        underlying = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))[0]
        under_obs = ContractObservation(
            label=f"{symbol} (stock)", kind="underlying", con_id=underlying.conId
        )
        report.underlying = under_obs

        chains = ib.reqSecDefOptParams(symbol, "", "STK", underlying.conId)
        if not chains:
            report.notes.append("reqSecDefOptParams returned no chains")
            report.outcome = ProbeOutcome.PROBE_ERROR
            return report

        # Prefer the chain carrying the most strikes. Filtering on
        # exchange == "SMART" can match a degenerate entry with a handful.
        chain = max(chains, key=lambda c: len(getattr(c, "strikes", ()) or ()))
        today = dt.date.today()
        expirations = sorted(
            e for e in getattr(chain, "expirations", ()) or () if _dte(e, today) > 0
        )
        if not expirations:
            report.notes.append("no future expirations in the chain")
            return report
        expiry = min(expirations, key=lambda e: abs(_dte(e, today) - target_dte))
        report.expiration = expiry
        report.dte = _dte(expiry, today)

        # Enumerate what is actually listed for THIS expiration. The chain's
        # strike list is the union across all expirations, so a strike from it
        # frequently does not exist for the expiry chosen.
        details = ib.reqContractDetails(
            Option(symbol, expiry, 0, "P", "SMART", currency="USD")
        )
        listed = sorted(
            {
                float(d.contract.strike)
                for d in details
                if getattr(getattr(d, "contract", None), "strike", None)
            }
        )
        if not listed:
            report.notes.append(f"no listed put strikes for {expiry}")
            return report

        # Subscribe to the underlying first so there is a spot to centre on.
        under_ticker = ib.reqMktData(underlying, "", False, False)
        subscribed.append(underlying)
        broker._settle(settle_seconds / 2)

        spot = (
            _f(under_ticker.marketPrice())
            or _f(getattr(under_ticker, "close", None))
            or _f(getattr(under_ticker, "last", None))
        )
        if spot is None:
            spot = listed[len(listed) // 2]
            report.notes.append(
                "no underlying price; centred the strike window on the chain median"
            )

        centre = min(range(len(listed)), key=lambda i: abs(listed[i] - spot))
        half = max(1, strike_count // 2)
        window = listed[max(0, centre - half) : centre + half]

        option_contracts = ib.qualifyContracts(
            *[
                Option(symbol, expiry, strike, "P", "SMART", currency="USD")
                for strike in window
            ]
        )
        for contract in option_contracts:
            obs = ContractObservation(
                label=f"{symbol} {expiry} {contract.strike}P",
                kind="option",
                con_id=contract.conId,
            )
            report.options.append(obs)
            ib.reqMktData(contract, "", False, False)
            subscribed.append(contract)

        broker._settle(settle_seconds)

        # Harvest. Values are read from the tickers, but liveness is read from
        # the recorder -- the ticker field cannot witness a callback.
        tickers = {
            getattr(getattr(t, "contract", None), "conId", None): t
            for t in getattr(ib, "tickers", lambda: [])()
        }

        def harvest(obs: ContractObservation) -> None:
            obs.data_type_callbacks = list(recorder.data_types.get(obs.con_id, ()))
            obs.greek_callback_count = recorder.greek_counts.get(obs.con_id, 0)
            ticker = tickers.get(obs.con_id)
            if ticker is None:
                return
            obs.bid = _f(getattr(ticker, "bid", None))
            obs.ask = _f(getattr(ticker, "ask", None))
            obs.last = _f(getattr(ticker, "last", None))
            obs.close = _f(getattr(ticker, "close", None))
            computation = recorder.latest_greeks.get(obs.con_id)
            if computation is None:
                computation = getattr(ticker, "modelGreeks", None)
            if computation is not None:
                obs.greeks = OptionGreeks.from_ib(
                    computation,
                    received_at=_utcnow(),
                    subscription_generation=__import__("uuid").uuid4(),
                )

        harvest(under_obs)
        under_obs.bid = under_obs.bid if under_obs.bid is not None else _f(
            getattr(under_ticker, "bid", None)
        )
        for obs in report.options:
            harvest(obs)

        report.outcome = classify(under_obs, report.options)

    except ImportError as exc:
        fatal = True
        report.errors.append(f"ib_async is not installed: {exc}")
        report.outcome = ProbeOutcome.PROBE_ERROR
    except EngineError:
        raise
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not crash
        fatal = True
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.outcome = ProbeOutcome.PROBE_ERROR
    finally:
        for contract in subscribed:
            try:
                ib.cancelMktData(contract)
            except Exception:  # noqa: BLE001 - teardown must not mask the result
                pass
        recorder.remove()
        try:
            ib.errorEvent -= on_error
        except Exception:  # noqa: BLE001 - older event shapes
            pass
        report.finished_at = _utcnow()
        if recorder.unmapped_events:
            report.notes.append(
                f"{recorder.unmapped_events} callbacks could not be mapped to a contract"
            )
        if fatal:
            report.outcome = ProbeOutcome.PROBE_ERROR

    return report
