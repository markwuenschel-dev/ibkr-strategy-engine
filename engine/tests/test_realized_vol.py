"""Unit coverage for the realized-vol / iv_rv_ratio calculation.

New financial logic (populates ``VolatilityAssessment.iv_rv_ratio``, which
gates the LOW and MEDIUM regime tiers) with no existing indirect coverage --
the universe/runner integration tests exercise wiring and pacing with fake
adapters, not the arithmetic itself. This file checks the arithmetic.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

import pytest

from engine.options.realized_vol import (
    CONTEXT_WINDOW,
    RATIO_WINDOW,
    PriceObservation,
    build_realized_vol,
    calculate_iv_rv_ratio,
    calculate_realized_vol,
    observations_from_price_bars,
)

D = Decimal


def _closes(values: list[float], start: dt.date = dt.date(2026, 1, 1)) -> list[PriceObservation]:
    """One observation per weekday, starting ``start``."""
    out: list[PriceObservation] = []
    day = start
    for value in values:
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        out.append(PriceObservation(on=day, close=D(str(value))))
        day += dt.timedelta(days=1)
    return out


class TestCalculateRealizedVol:
    def test_zero_variance_series_is_undefined_not_zero(self) -> None:
        """A flat close series has no realized vol -- None, never 0. A zero
        would read as 'no risk' and, as iv_rv_ratio's denominator, would
        either explode the ratio or divide by zero; neither is honest."""
        closes = _closes([100.0] * (RATIO_WINDOW + 1))
        assert calculate_realized_vol(closes, window=RATIO_WINDOW) is None

    def test_too_few_observations_is_none(self) -> None:
        closes = _closes([100.0, 101.0, 99.0])
        assert calculate_realized_vol(closes, window=RATIO_WINDOW) is None

    def test_known_daily_move_produces_the_expected_annualized_figure(self) -> None:
        """Alternating +/-1% daily log returns has a closed-form stdev: every
        return has the same magnitude, so the sample stdev of a zero-mean
        alternating series is exactly that magnitude (Bessel-corrected)."""
        move = 0.01
        values = [100.0]
        for i in range(RATIO_WINDOW):
            values.append(values[-1] * math.exp(move if i % 2 == 0 else -move))
        closes = _closes(values)

        result = calculate_realized_vol(closes, window=RATIO_WINDOW)

        assert result is not None
        # returns alternate +move, -move -> mean 0, population variance move^2,
        # sample variance move^2 * n/(n-1)
        n = RATIO_WINDOW
        expected_daily = move * math.sqrt(n / (n - 1))
        expected_annualized = expected_daily * math.sqrt(252)
        assert float(result) == pytest.approx(expected_annualized, rel=1e-6)

    def test_a_non_positive_close_makes_the_series_undefined(self) -> None:
        closes = _closes([100.0] * RATIO_WINDOW) + [
            PriceObservation(on=dt.date(2026, 3, 1), close=D("0"))
        ]
        assert calculate_realized_vol(closes, window=RATIO_WINDOW) is None


class TestCalculateIvRvRatio:
    def test_ratio_is_iv_over_realized_vol(self) -> None:
        assert calculate_iv_rv_ratio(D("0.30"), D("0.20")) == D("1.5")

    def test_missing_current_iv_is_none(self) -> None:
        assert calculate_iv_rv_ratio(None, D("0.20")) is None

    def test_missing_realized_vol_is_none(self) -> None:
        assert calculate_iv_rv_ratio(D("0.30"), None) is None

    def test_nonpositive_realized_vol_never_divides(self) -> None:
        assert calculate_iv_rv_ratio(D("0.30"), D("0")) is None
        assert calculate_iv_rv_ratio(D("0.30"), D("-0.1")) is None


class TestObservationsFromPriceBars:
    class _Bar:
        def __init__(self, date: str, close: float) -> None:
            self.date = date
            self.close = close

    def test_drops_bars_with_unreadable_close(self) -> None:
        bars = [self._Bar("20260101", 100.0), self._Bar("20260102", float("nan"))]
        observations = observations_from_price_bars(bars)
        assert len(observations) == 1
        assert observations[0].close == D("100.0")

    def test_rejects_dbl_max_like_ib_async_placeholder(self) -> None:
        bars = [self._Bar("20260101", 1.7976931348623157e308)]
        assert observations_from_price_bars(bars) == []


class TestBuildRealizedVol:
    def test_degrades_with_a_named_reason_when_too_few_observations(self) -> None:
        metric = build_realized_vol(
            "AAA",
            _closes([100.0, 101.0]),
            current_iv=D("0.30"),
            calculated_at=dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc),
        )
        assert metric.iv_rv_ratio is None
        assert metric.degraded_reason is not None
        assert not metric.is_usable

    def test_a_rich_iv_against_calm_realized_vol_produces_a_usable_ratio(self) -> None:
        # A closely-alternating +/-0.1% series -> a small, well-defined RV,
        # so a much larger current_iv produces ratio > 1 (rich IV).
        move = 0.001
        values = [100.0]
        for i in range(CONTEXT_WINDOW):
            values.append(values[-1] * math.exp(move if i % 2 == 0 else -move))
        closes = _closes(values)

        metric = build_realized_vol(
            "AAA",
            closes,
            current_iv=D("0.50"),
            calculated_at=dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc),
        )

        assert metric.is_usable
        assert metric.realized_vol_20 is not None
        assert metric.realized_vol_60 is not None
        assert metric.iv_rv_ratio is not None
        assert metric.iv_rv_ratio > D("1.0")
        assert metric.ratio_window == RATIO_WINDOW

    def test_missing_current_iv_degrades_the_ratio_but_keeps_realized_vol(self) -> None:
        move = 0.001
        values = [100.0]
        for i in range(CONTEXT_WINDOW):
            values.append(values[-1] * math.exp(move if i % 2 == 0 else -move))
        closes = _closes(values)

        metric = build_realized_vol(
            "AAA",
            closes,
            current_iv=None,
            calculated_at=dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc),
        )

        assert metric.realized_vol_20 is not None
        assert metric.iv_rv_ratio is None
        assert not metric.is_usable
