"""Realized volatility from the underlying's own daily closes.

``iv_rv_ratio`` gates the LOW and MEDIUM regime tiers (``regime.py``): a
symbol whose implied volatility is not priced above its own recent realized
volatility has no edge to sell, tastytrade-style. This module supplies the
denominator that was, until now, never computed anywhere in the engine --
``VolatilityAssessment.iv_rv_ratio`` was always ``None``, which reads as
"unknown edge" and refuses both tiers outright (``REFUSAL_EDGE``).

Mirrors :mod:`engine.options.ivrank` deliberately: same fail-closed shape
(``degraded_reason`` rather than a fabricated number), same raw-observations-
persisted-separately-from-the-derived-metric split, same ``_to_decimal``
sanitizer for DBL_MAX / NaN / inf guards against a flaky adapter.

**Two windows, one gate.** ``realized_vol_20`` (~one month of trading days)
is the tighter, more responsive estimate and is what ``iv_rv_ratio`` divides
by -- a stale quarter-old realized-vol figure would understate a recent
regime change exactly when the ratio matters most. ``realized_vol_60`` rides
along as context only (``VolatilityAssessment`` docstring: evidence the
reviewer sees, not a requirement) -- it is not consulted by ``classify()``.
This window choice is a strategy parameter, not a derived fact; it is
recorded on the metric (``ratio_window``) so it can be audited or changed
without guessing what a cached record assumed.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "PriceObservation",
    "RealizedVolMetric",
    "METHODOLOGY_VERSION",
    "SOURCE_IBKR_TRADES",
    "TRADING_DAYS",
    "RATIO_WINDOW",
    "CONTEXT_WINDOW",
    "calculate_realized_vol",
    "calculate_iv_rv_ratio",
    "observations_from_price_bars",
    "build_realized_vol",
]

# Bumped whenever the calculation changes, so a cached metric computed under
# old rules can be told apart from a fresh one rather than silently reused.
METHODOLOGY_VERSION = "realized-vol/1"

SOURCE_IBKR_TRADES = "IBKR:TRADES:daily"

TRADING_DAYS = 252

#: Trading days of returns behind the ratio's denominator (~1 month).
RATIO_WINDOW = 20
#: Trading days behind the context-only longer window (~1 quarter).
CONTEXT_WINDOW = 60

# calculate_realized_vol needs WINDOW+1 closes to produce WINDOW returns.
_MINIMUM_OBSERVATIONS = CONTEXT_WINDOW + 1


@dataclass(frozen=True)
class PriceObservation:
    """One daily close, kept raw so realized vol is recomputed from inputs
    rather than trusted from a cache of conclusions."""

    on: dt.date
    close: Decimal


@dataclass(frozen=True)
class RealizedVolMetric:
    symbol: str
    realized_vol_20: Decimal | None
    realized_vol_60: Decimal | None
    current_iv: Decimal | None
    iv_rv_ratio: Decimal | None
    ratio_window: int
    observation_count: int
    first_observation: dt.date | None
    last_observation: dt.date | None
    source: str
    methodology_version: str
    calculated_at: dt.datetime
    degraded_reason: str | None = None

    @property
    def is_usable(self) -> bool:
        """A tier may not tighten on an unavailable ratio."""
        return self.iv_rv_ratio is not None and self.degraded_reason is None

    def to_record(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "realized_vol_20": (
                str(self.realized_vol_20) if self.realized_vol_20 is not None else None
            ),
            "realized_vol_60": (
                str(self.realized_vol_60) if self.realized_vol_60 is not None else None
            ),
            "current_iv": str(self.current_iv) if self.current_iv is not None else None,
            "iv_rv_ratio": str(self.iv_rv_ratio) if self.iv_rv_ratio is not None else None,
            "ratio_window": self.ratio_window,
            "observation_count": self.observation_count,
            "first_observation": (
                self.first_observation.isoformat() if self.first_observation else None
            ),
            "last_observation": (
                self.last_observation.isoformat() if self.last_observation else None
            ),
            "source": self.source,
            "methodology_version": self.methodology_version,
            "degraded_reason": self.degraded_reason,
        }


def _annualized_stdev(closes: Sequence[Decimal], window: int) -> Decimal | None:
    """Annualized stdev of daily log returns over the trailing ``window``.

    ``None`` on too few closes or a degenerate (zero-variance / non-positive)
    series -- the same "unavailable is not zero" contract as IV Rank. A zero
    realized vol would read as "no risk" and, as a ratio denominator, would
    either divide-by-zero or manufacture an infinite edge; neither is honest.
    """
    kept = list(closes)[-(window + 1) :]
    if len(kept) < window + 1:
        return None
    returns: list[float] = []
    for previous, current in zip(kept, kept[1:]):
        if previous <= 0 or current <= 0:
            return None
        try:
            returns.append(math.log(float(current) / float(previous)))
        except (ValueError, OverflowError):
            return None
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if variance <= 0:
        return None
    daily_stdev = math.sqrt(variance)
    annualized = daily_stdev * math.sqrt(TRADING_DAYS)
    if not math.isfinite(annualized) or annualized <= 0:
        return None
    try:
        return Decimal(str(annualized))
    except (InvalidOperation, ValueError):  # pragma: no cover
        return None


def calculate_realized_vol(
    observations: Sequence[PriceObservation], *, window: int
) -> Decimal | None:
    """Annualized realized volatility over the trailing ``window`` trading
    days, oldest-first ``observations`` assumed."""
    closes = [o.close for o in observations]
    return _annualized_stdev(closes, window)


def calculate_iv_rv_ratio(
    current_iv: Decimal | None, realized_vol: Decimal | None
) -> Decimal | None:
    """``current_iv / realized_vol`` -- how rich implied is versus what the
    underlying actually did. ``None`` propagates from either missing input or
    a non-positive realized vol, never a divide-by-zero."""
    if current_iv is None or realized_vol is None or realized_vol <= 0 or current_iv <= 0:
        return None
    return current_iv / realized_vol


def _to_decimal(value: Any) -> Decimal | None:
    """Reject NaN, infinity, DBL_MAX and anything nonpositive."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if number >= 1.7976931348623157e308:
        return None
    try:
        return Decimal(str(number))
    except (InvalidOperation, ValueError):  # pragma: no cover
        return None


def _to_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(value[:10].replace("-", "")[:8], "%Y%m%d").date()
            except ValueError:
                continue
    return None


def observations_from_price_bars(bars: Sequence[Any]) -> list[PriceObservation]:
    """Turn ib_async ``BarData`` (``whatToShow="TRADES"``) into validated
    observations. Bars whose date or close cannot be read are dropped, not
    defaulted."""
    observations: list[PriceObservation] = []
    for bar in bars:
        on = _to_date(getattr(bar, "date", None))
        close = _to_decimal(getattr(bar, "close", None))
        if on is None or close is None:
            continue
        observations.append(PriceObservation(on=on, close=close))
    observations.sort(key=lambda o: o.on)
    return observations


def build_realized_vol(
    symbol: str,
    observations: Sequence[PriceObservation],
    *,
    current_iv: Decimal | None,
    calculated_at: dt.datetime,
    ratio_window: int = RATIO_WINDOW,
    context_window: int = CONTEXT_WINDOW,
) -> RealizedVolMetric:
    """Compute the metric, degrading explicitly rather than guessing."""
    kept = list(observations)[-(context_window + 1) :]

    def degraded(reason: str) -> RealizedVolMetric:
        return RealizedVolMetric(
            symbol=symbol,
            realized_vol_20=None,
            realized_vol_60=None,
            current_iv=current_iv,
            iv_rv_ratio=None,
            ratio_window=ratio_window,
            observation_count=len(kept),
            first_observation=kept[0].on if kept else None,
            last_observation=kept[-1].on if kept else None,
            source=SOURCE_IBKR_TRADES,
            methodology_version=METHODOLOGY_VERSION,
            calculated_at=calculated_at,
            degraded_reason=reason,
        )

    if not kept:
        return degraded("no usable daily-close observations")
    if len(kept) < ratio_window + 1:
        return degraded(
            f"only {len(kept)} observations, need at least {ratio_window + 1} "
            f"for a {ratio_window}-day realized-vol window"
        )

    rv20 = calculate_realized_vol(kept, window=ratio_window)
    rv60 = calculate_realized_vol(kept, window=context_window)
    if rv20 is None:
        return degraded(
            f"flat, negative, or degenerate {ratio_window}-day close series; "
            "realized volatility is undefined"
        )
    ratio = calculate_iv_rv_ratio(current_iv, rv20)

    return RealizedVolMetric(
        symbol=symbol,
        realized_vol_20=rv20,
        realized_vol_60=rv60,
        current_iv=current_iv,
        iv_rv_ratio=ratio,
        ratio_window=ratio_window,
        observation_count=len(kept),
        first_observation=kept[0].on,
        last_observation=kept[-1].on,
        source=SOURCE_IBKR_TRADES,
        methodology_version=METHODOLOGY_VERSION,
        calculated_at=calculated_at,
        degraded_reason=None if ratio is not None else "current_iv unavailable to form a ratio",
    )
