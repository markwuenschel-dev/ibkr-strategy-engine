"""IV Rank from IBKR's own implied-volatility history.

This runs today. It needs no market-data subscription: the input is
``reqHistoricalData(whatToShow="OPTION_IMPLIED_VOLATILITY")``, which returned a
full year of usable daily bars on the delayed-only paper account.

Two rules the type system enforces rather than trusts:

**One source, start to finish.** ``current_iv`` is the most recent bar of the
same series the range comes from. Mixing an IBKR current reading against a range
computed elsewhere produces a number that looks like IV Rank and means nothing.
``source`` and ``methodology_version`` are recorded on the metric so a cache hit
or a journal entry can be checked rather than assumed.

**Unavailable is not zero.** A flat range, too few observations, or a bad
current reading yields ``iv_rank=None`` with a ``degraded_reason``, never a
default. A zero would read as "very low IV" and, against a ``>= 50`` entry
filter, would silently mean "never trade" -- a bug that looks like caution.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "IVObservation",
    "IVRankMetric",
    "METHODOLOGY_VERSION",
    "SOURCE_IBKR_OPTION_IV",
    "TRADING_DAYS",
    "calculate_iv_rank",
    "observations_from_bars",
    "build_iv_rank",
]

# Bumped whenever the calculation changes, so a cached metric computed under old
# rules can be told apart from a fresh one rather than silently reused.
METHODOLOGY_VERSION = "iv-rank/1"

# Never to be compared numerically with a tastytrade IV Rank. Different input
# series, different methodology; the label is what stops them being mixed.
SOURCE_IBKR_OPTION_IV = "IBKR:OPTION_IMPLIED_VOLATILITY:daily"

TRADING_DAYS = 252
MINIMUM_OBSERVATIONS = 60


@dataclass(frozen=True)
class IVObservation:
    """One daily implied-volatility reading, kept raw and persisted separately
    from the derived metric so a recomputation can be checked against inputs."""

    on: dt.date
    implied_volatility: Decimal


@dataclass(frozen=True)
class IVRankMetric:
    symbol: str
    current_iv: Decimal | None
    trailing_low: Decimal | None
    trailing_high: Decimal | None
    iv_rank: Decimal | None
    observation_count: int
    first_observation: dt.date | None
    last_observation: dt.date | None
    source: str
    methodology_version: str
    calculated_at: dt.datetime
    degraded_reason: str | None = None

    @property
    def is_usable(self) -> bool:
        """A strategy may not be admitted on an unavailable IV Rank."""
        return self.iv_rank is not None and self.degraded_reason is None

    def meets(self, minimum: Decimal) -> bool:
        """Fails closed: an unusable metric never meets a threshold."""
        return self.is_usable and self.iv_rank is not None and self.iv_rank >= minimum

    def describe(self) -> str:
        if self.iv_rank is None:
            return (
                f"{self.symbol}  IV Rank unavailable"
                f"  ({self.degraded_reason or 'no reason recorded'})"
            )
        return (
            f"{self.symbol}  IV Rank {self.iv_rank:.2f}"
            f"  (current {self.current_iv}, range {self.trailing_low}-{self.trailing_high},"
            f" {self.observation_count} obs {self.first_observation}..{self.last_observation})"
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "iv_rank": str(self.iv_rank) if self.iv_rank is not None else None,
            "current_iv": str(self.current_iv) if self.current_iv is not None else None,
            "trailing_low": str(self.trailing_low) if self.trailing_low is not None else None,
            "trailing_high": str(self.trailing_high) if self.trailing_high is not None else None,
            "observation_count": self.observation_count,
            "first_observation": self.first_observation.isoformat()
            if self.first_observation
            else None,
            "last_observation": self.last_observation.isoformat()
            if self.last_observation
            else None,
            "source": self.source,
            "methodology_version": self.methodology_version,
            "degraded_reason": self.degraded_reason,
        }


def calculate_iv_rank(
    current_iv: Decimal,
    trailing_low: Decimal,
    trailing_high: Decimal,
) -> Decimal | None:
    """Where the current reading sits in its trailing range, 0-100.

    Returns ``None`` for a flat or inverted range rather than dividing. A year
    of identical IV has no rank -- the honest answer is "cannot say", and any
    number returned here would be acted on.
    """
    denominator = trailing_high - trailing_low
    if current_iv <= 0 or denominator <= 0:
        return None
    rank = Decimal("100") * (current_iv - trailing_low) / denominator
    return min(Decimal("100"), max(Decimal("0"), rank))


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


def observations_from_bars(bars: Sequence[Any]) -> list[IVObservation]:
    """Turn ib_async ``BarData`` into validated observations.

    Bars whose date or close cannot be read are dropped, not defaulted -- and
    the count of what survived is what the metric reports, so a mostly-empty
    series shows up as degraded rather than as a confident number.
    """
    observations: list[IVObservation] = []
    for bar in bars:
        on = _to_date(getattr(bar, "date", None))
        close = _to_decimal(getattr(bar, "close", None))
        if on is None or close is None:
            continue
        observations.append(IVObservation(on=on, implied_volatility=close))
    observations.sort(key=lambda o: o.on)
    return observations


def build_iv_rank(
    symbol: str,
    observations: Sequence[IVObservation],
    *,
    calculated_at: dt.datetime,
    window: int = TRADING_DAYS,
    minimum_observations: int = MINIMUM_OBSERVATIONS,
) -> IVRankMetric:
    """Compute the metric, degrading explicitly rather than guessing.

    The current reading is the last bar of the retained window, so it is by
    construction from the same series as the range.
    """
    kept = list(observations)[-window:]

    def degraded(reason: str) -> IVRankMetric:
        return IVRankMetric(
            symbol=symbol,
            current_iv=None,
            trailing_low=None,
            trailing_high=None,
            iv_rank=None,
            observation_count=len(kept),
            first_observation=kept[0].on if kept else None,
            last_observation=kept[-1].on if kept else None,
            source=SOURCE_IBKR_OPTION_IV,
            methodology_version=METHODOLOGY_VERSION,
            calculated_at=calculated_at,
            degraded_reason=reason,
        )

    if not kept:
        return degraded("no usable implied-volatility observations")
    if len(kept) < minimum_observations:
        return degraded(
            f"only {len(kept)} observations, need at least {minimum_observations}"
        )

    values = [o.implied_volatility for o in kept]
    low, high, current = min(values), max(values), values[-1]
    rank = calculate_iv_rank(current, low, high)

    if rank is None:
        return degraded(
            f"flat or inverted trailing range ({low}..{high}); IV Rank is undefined"
        )

    return IVRankMetric(
        symbol=symbol,
        current_iv=current,
        trailing_low=low,
        trailing_high=high,
        iv_rank=rank,
        observation_count=len(kept),
        first_observation=kept[0].on,
        last_observation=kept[-1].on,
        source=SOURCE_IBKR_OPTION_IV,
        methodology_version=METHODOLOGY_VERSION,
        calculated_at=calculated_at,
        degraded_reason=None,
    )
