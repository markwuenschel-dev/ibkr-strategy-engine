"""Bulk IBKR market-data entitlement measurement for the universe catalog.

Automates what has, until today, been a manual, one-symbol-at-a-time process:
an operator running ``options-scan`` by hand against one symbol and hand-
writing the result into the catalog JSON. That only 10 of the catalog's 80
symbols had ever been measured (``docs/autotrader-catalog-operator-v1.json``)
is the direct consequence -- there was no bulk tool.

Reuses the exact live-vs-delayed signal the manual process used: the
``marketDataType`` callback, recorded via
:class:`engine.options.probe.CallbackRecorder` so "the server said nothing"
and "the server said delayed" are never conflated. Measured against the
*underlying* alone -- the catalog's own recorded evidence for the five
already-denied symbols (QQQ, TLT, AAPL, MSFT, NVDA) states the gap is the
underlying equity feed, not the option legs, so that is the cheapest signal
that answers the actual question.

**Read-only against the broker.** Subscribes, observes, cancels. This module
has no import of and no reference to any order-placement call -- the same
guarantee ``test_options_no_transmit.py`` walks the AST of every module in
this package to enforce.

**A measurement is evidence, not an assertion.** A callback that names a type
other than LIVE, or an explicit broker error, produces a hard denial with the
type/error recorded as ``reason``. Silence -- no callback observed within the
timeout -- produces an *inconclusive* result, not a denial: "we heard
nothing" and "the server said no" are different findings
(:func:`engine.options.probe.classify` draws the same distinction), and an
inconclusive symbol is left at its previous classification rather than
invented into either a promotion or a demotion.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .marketdata import MarketDataType
from .probe import CallbackRecorder

__all__ = [
    "EntitlementMeasurement",
    "measure_symbol_entitlement",
    "measure_catalog_entitlement",
]

METHOD = (
    "options-measure-entitlement bulk live-quote measurement: one "
    "reqMktData(type=LIVE) subscription per symbol against the underlying, "
    "marketDataType callback observed via CallbackRecorder, cancelled "
    "immediately after."
)


@dataclass(frozen=True)
class EntitlementMeasurement:
    symbol: str
    #: True/False are hard conclusions; None means no callback was observed
    #: within the timeout -- inconclusive, not a denial.
    entry_allowed: bool | None
    readiness: str
    #: The exchange IBKR's own contract qualification reported, or None if
    #: qualification itself failed.
    listing_venue: str | None
    reported_types: tuple[int, ...]
    reason: str
    measured_at: dt.date
    error: str | None = None


def measure_symbol_entitlement(
    ib: Any,
    symbol: str,
    *,
    requested_type: int = int(MarketDataType.LIVE),
    timeout_seconds: float = 6.0,
    poll_seconds: float = 0.25,
    sleep: Any = None,
    now: dt.date | None = None,
) -> EntitlementMeasurement:
    """Measure one symbol's live-data entitlement. Never raises for a normal
    broker refusal -- a qualification failure or a hard entitlement denial
    both come back as a populated :class:`EntitlementMeasurement`, because a
    bulk run over 80 symbols must not abort on the first refused one."""
    from ib_async import Stock  # noqa: PLC0415 - optional dependency

    measured_at = now or dt.datetime.now(dt.timezone.utc).date()
    key = symbol.strip().upper()
    sleeper = sleep or ib.sleep

    try:
        qualified = ib.qualifyContracts(Stock(key, "SMART", "USD"))
    except Exception as exc:  # noqa: BLE001 - adapter boundary
        return EntitlementMeasurement(
            symbol=key,
            entry_allowed=None,
            readiness="UNVERIFIED",
            listing_venue=None,
            reported_types=(),
            reason=f"contract qualification failed: {type(exc).__name__}: {exc}",
            measured_at=measured_at,
            error=str(exc),
        )
    if not qualified:
        return EntitlementMeasurement(
            symbol=key,
            entry_allowed=None,
            readiness="UNVERIFIED",
            listing_venue=None,
            reported_types=(),
            reason="IBKR did not qualify the underlying contract",
            measured_at=measured_at,
        )
    contract = qualified[0]
    venue = str(getattr(contract, "primaryExchange", "") or "").strip().upper() or None
    con_id = int(getattr(contract, "conId", 0))

    errors: list[str] = []

    def on_error(req_id: int, code: int, message: str, *_: Any) -> None:
        errors.append(f"{code}: {message}")

    recorder = CallbackRecorder(ib)
    recorder.install()
    ib.errorEvent += on_error
    try:
        ib.reqMarketDataType(requested_type)
        ib.reqMktData(contract, "", False, False)
        elapsed = 0.0
        while con_id not in recorder.data_types and elapsed < timeout_seconds:
            sleeper(poll_seconds)
            elapsed += poll_seconds
    finally:
        try:
            ib.cancelMktData(contract)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        ib.errorEvent -= on_error
        recorder.remove()

    reported = tuple(recorder.data_types.get(con_id, ()))
    if not reported:
        detail = f"; broker errors: {'; '.join(errors)}" if errors else ""
        return EntitlementMeasurement(
            symbol=key,
            entry_allowed=None,
            readiness="UNVERIFIED",
            listing_venue=venue,
            reported_types=(),
            reason=f"no marketDataType callback observed within {timeout_seconds}s{detail}",
            measured_at=measured_at,
            error="; ".join(errors) or None,
        )

    if MarketDataType.LIVE in reported:
        return EntitlementMeasurement(
            symbol=key,
            entry_allowed=True,
            readiness="VERIFIED",
            listing_venue=venue,
            reported_types=reported,
            reason=(
                f"{venue or 'the venue'} underlying served live type-1 data "
                "with no broker error, market_data_entitlement PASS"
            ),
            measured_at=measured_at,
        )

    names = ", ".join(
        MarketDataType(t).name if t in (1, 2, 3, 4) else str(t) for t in reported
    )
    detail = f"; broker errors: {'; '.join(errors)}" if errors else ""
    return EntitlementMeasurement(
        symbol=key,
        entry_allowed=False,
        readiness="UNVERIFIED",
        listing_venue=venue,
        reported_types=reported,
        reason=(
            f"requested LIVE, server reported {names}{detail} -- "
            "the underlying equity feed is not entitled for this account"
        ),
        measured_at=measured_at,
    )


def measure_catalog_entitlement(
    ib: Any,
    symbols: Sequence[str],
    *,
    requested_type: int = int(MarketDataType.LIVE),
    timeout_seconds: float = 6.0,
    poll_seconds: float = 0.25,
    sleep: Any = None,
    now: dt.date | None = None,
) -> list[EntitlementMeasurement]:
    """One measurement per symbol, in order, on the one connection given."""
    return [
        measure_symbol_entitlement(
            ib,
            symbol,
            requested_type=requested_type,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            sleep=sleep,
            now=now,
        )
        for symbol in symbols
    ]
