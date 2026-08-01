"""The universe scanner's foundations: the IV cache and the pacing budget.

Both exist for one number: ninety symbols against a sixty-requests-per-ten-
minutes broker limit. The cache makes the second scan of a day cost nothing;
the budget makes the first scan slow instead of banned.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from engine.options.ivrank import IVObservation, build_iv_rank
from engine.options.ivstore import IVStore
from engine.options.pacing import (
    DiscoveryPaced,
    PacedRequestBudget,
    Priority,
    RequestKind,
)

D = Decimal
NOW = dt.datetime(2026, 8, 3, 13, 0, tzinfo=dt.timezone.utc)  # a Monday
TODAY = NOW.date()


def observations(count: int = 70, *, last: dt.date | None = None) -> list[IVObservation]:
    end = last or dt.date(2026, 7, 31)  # the previous Friday
    out = []
    for i in range(count):
        on = end - dt.timedelta(days=count - 1 - i)
        out.append(IVObservation(on=on, implied_volatility=D("0.15") + D(i) / 1000))
    return out


class TestIVStoreRoundTrip:
    def test_written_series_reads_back_identically(self, tmp_path) -> None:
        store = IVStore(tmp_path / "iv")
        series = observations()
        store.write("SPY", series, fetched_at=NOW)
        cached = store.read("SPY")
        assert list(cached.observations) == series
        assert cached.fetched_at == NOW

    def test_the_cached_series_feeds_the_real_metric(self, tmp_path) -> None:
        """The cache stores inputs, not conclusions -- the metric (rank AND
        percentile) recomputes from what was persisted."""
        store = IVStore(tmp_path / "iv")
        store.write("SPY", observations(), fetched_at=NOW)
        metric = build_iv_rank(
            "SPY", store.read("SPY").observations, calculated_at=NOW
        )
        assert metric.is_usable
        assert metric.iv_percentile is not None

    def test_a_corrupt_line_degrades_not_bricks(self, tmp_path) -> None:
        store = IVStore(tmp_path / "iv")
        path = store.write("SPY", observations(), fetched_at=NOW)
        content = path.read_text(encoding="utf-8").splitlines()
        content.insert(3, "{not json")
        content.insert(5, '{"on": "not-a-date", "iv": "nope"}')
        path.write_text("\n".join(content) + "\n", encoding="utf-8")
        cached = store.read("SPY")
        assert len(cached.observations) == 70  # the good lines all survive
        assert cached.fetched_at == NOW

    def test_absent_symbol_reads_empty(self, tmp_path) -> None:
        cached = IVStore(tmp_path / "iv").read("XYZ")
        assert cached.observations == ()
        assert cached.fetched_at is None


class TestFreshness:
    def test_fetched_today_reaching_last_session_is_fresh(self, tmp_path) -> None:
        """Monday, cache fetched this morning, series ends Friday: no request."""
        store = IVStore(tmp_path / "iv")
        store.write("SPY", observations(last=dt.date(2026, 7, 31)), fetched_at=NOW)
        assert store.fresh("SPY", today=TODAY, now=NOW)

    def test_fetched_yesterday_is_stale(self, tmp_path) -> None:
        store = IVStore(tmp_path / "iv")
        store.write(
            "SPY",
            observations(last=dt.date(2026, 7, 31)),
            fetched_at=NOW - dt.timedelta(days=1),
        )
        assert not store.fresh("SPY", today=TODAY, now=NOW)

    def test_a_series_short_of_the_previous_session_is_stale(self, tmp_path) -> None:
        """Fetched today but the data stops Wednesday: something ate two
        sessions, and trading on it would rank against a stale range."""
        store = IVStore(tmp_path / "iv")
        store.write("SPY", observations(last=dt.date(2026, 7, 29)), fetched_at=NOW)
        assert not store.fresh("SPY", today=TODAY, now=NOW)

    def test_empty_cache_is_stale(self, tmp_path) -> None:
        assert not IVStore(tmp_path / "iv").fresh("SPY", today=TODAY, now=NOW)


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestPacedRequestBudget:
    def test_management_bursts_to_full_capacity_without_sleeping(self) -> None:
        """Priorities 1-2 own the whole bucket, reserve included."""
        fake = FakeTime()
        budget = PacedRequestBudget(
            historical_per_window=5, clock=fake.clock, sleeper=fake.sleep
        )
        for _ in range(5):
            budget.acquire(
                RequestKind.HISTORICAL, priority=Priority.EXITS_MANAGEMENT
            )
        assert fake.slept == []

    def test_the_request_past_capacity_waits_for_a_refill(self) -> None:
        fake = FakeTime()
        budget = PacedRequestBudget(
            historical_per_window=5,
            historical_window_seconds=600.0,
            clock=fake.clock,
            sleeper=fake.sleep,
        )
        for _ in range(5):
            budget.acquire(
                RequestKind.HISTORICAL, priority=Priority.EXITS_MANAGEMENT
            )
        budget.acquire(RequestKind.HISTORICAL, priority=Priority.EXITS_MANAGEMENT)
        # One token refills in window/capacity = 120s; the wait must be real
        # and bounded, not a spin.
        assert 100.0 <= sum(fake.slept) <= 140.0
        assert len(fake.slept) < 10

    def test_penalize_halves_the_refill_rate_and_drops_tokens(self) -> None:
        """Error 162 means the broker's ledger disagrees with ours; ours is
        wrong by definition. The next request after a penalty waits about
        twice as long as it would have."""
        fake = FakeTime()
        budget = PacedRequestBudget(
            historical_per_window=5,
            historical_window_seconds=600.0,
            clock=fake.clock,
            sleeper=fake.sleep,
        )
        budget.penalize(RequestKind.HISTORICAL)
        budget.acquire(RequestKind.HISTORICAL, priority=Priority.EXITS_MANAGEMENT)
        assert 220.0 <= sum(fake.slept) <= 260.0

    def test_kinds_do_not_share_tokens(self) -> None:
        fake = FakeTime()
        budget = PacedRequestBudget(
            historical_per_window=1,
            general_per_window=3,
            clock=fake.clock,
            sleeper=fake.sleep,
        )
        budget.acquire(RequestKind.HISTORICAL, priority=Priority.EXITS_MANAGEMENT)
        for _ in range(3):
            budget.acquire(RequestKind.GENERAL, priority=Priority.EXITS_MANAGEMENT)
        assert fake.slept == []

    def test_a_one_token_bucket_cannot_deadlock_low_priority(self) -> None:
        """The reserve floor is capped below capacity: with a single-token
        bucket a DISCOVERY acquire must complete (slowly), never spin --
        pinned against the exact infinite loop found on 2026-08-01."""
        fake = FakeTime()
        budget = PacedRequestBudget(
            historical_per_window=1,
            historical_window_seconds=600.0,
            clock=fake.clock,
            sleeper=fake.sleep,
        )
        budget.acquire(RequestKind.HISTORICAL, priority=Priority.DISCOVERY)
        budget.acquire(RequestKind.HISTORICAL, priority=Priority.DISCOVERY)
        assert len(fake.slept) < 20  # waited, terminated


class TestManagementReserve:
    def test_scanner_load_cannot_consume_the_management_reserve(self) -> None:
        """Discovery tries to drain the whole bucket -- past the reserve line
        -- and management must STILL acquire without a single wait. Without
        the floor, the same discovery load empties the bucket and management
        queues behind a scan, which is the starvation this reserve forbids.

        Discovery drawing more than the free portion is the load-bearing part
        of the fixture: a test that stops politely at the reserve line passes
        with the floor deleted (found by exactly that mutation, 2026-08-01).
        """
        fake = FakeTime()
        budget = PacedRequestBudget(
            historical_per_window=8,
            historical_window_seconds=600.0,
            management_reserve_fraction=0.25,  # reserve = 2 tokens
            clock=fake.clock,
            sleeper=fake.sleep,
        )
        for _ in range(6):  # the free portion: 8 - reserve 2
            budget.acquire(RequestKind.HISTORICAL, priority=Priority.DISCOVERY)
        assert fake.slept == []
        for _ in range(2):  # past the line: these wait for refill above floor
            budget.acquire(RequestKind.HISTORICAL, priority=Priority.DISCOVERY)
        assert fake.slept, "drawing past the reserve line must wait"

        before = list(fake.slept)
        budget.acquire(RequestKind.HISTORICAL, priority=Priority.EXITS_MANAGEMENT)
        budget.acquire(RequestKind.HISTORICAL, priority=Priority.WORKING_ORDERS)
        assert fake.slept == before, "management must not wait behind a scan"

    def test_pacing_penalty_stops_discovery_but_not_exits(self) -> None:
        """Error 162: discovery raises DiscoveryPaced and stands down;
        an exit acquires from the refilling reserve, slowly but surely."""
        fake = FakeTime()
        budget = PacedRequestBudget(
            historical_per_window=8,
            historical_window_seconds=600.0,
            clock=fake.clock,
            sleeper=fake.sleep,
        )
        budget.penalize(RequestKind.HISTORICAL)
        with pytest.raises(DiscoveryPaced):
            budget.acquire(RequestKind.HISTORICAL, priority=Priority.DISCOVERY)
        budget.acquire(RequestKind.HISTORICAL, priority=Priority.EXITS_MANAGEMENT)
        assert sum(fake.slept) > 0  # slower, never refused
