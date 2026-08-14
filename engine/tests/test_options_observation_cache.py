"""Durable raw observations and starvation-free refresh state."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from engine.options.freshness import FreshnessClass, ObservationEnvelope
from engine.options.observation_cache import (
    CacheWriteRefused,
    RawObservation,
    SQLiteObservationCache,
)

NOW = dt.datetime(2026, 8, 14, 14, 0, tzinfo=dt.timezone.utc)


def observation(
    symbol: str = "SPY",
    *,
    key: str = "iv-history",
    freshness: FreshnessClass = FreshnessClass.SLOW_OBSERVATION,
    catalog_version: str = "catalog/1",
) -> RawObservation:
    return RawObservation(
        symbol=symbol,
        key=key,
        payload={"raw": "0.25", "source_field": "close"},
        envelope=ObservationEnvelope(
            symbol=symbol,
            session_date=NOW.date(),
            observed_at=NOW,
            expires_at=NOW + dt.timedelta(hours=4),
            source="test",
            freshness_class=freshness,
            configuration_version="config/1",
        ),
        catalog_version=catalog_version,
    )


class TestSQLiteObservationCache:
    def test_batch_round_trip_preserves_provenance(self, tmp_path) -> None:
        cache = SQLiteObservationCache(tmp_path / "observations.sqlite3")
        cache.write_batch([observation(), observation("QQQ", key="oi")])
        result = cache.read_many(
            ["SPY", "QQQ"],
            now=NOW + dt.timedelta(minutes=10),
            session_date=NOW.date(),
            catalog_version="catalog/1",
            configuration_version="config/1",
        )
        assert result["SPY"][0].payload["raw"] == "0.25"
        assert result["SPY"][0].envelope.freshness_class is FreshnessClass.SLOW_OBSERVATION
        assert result["QQQ"][0].catalog_version == "catalog/1"

    def test_perishable_observations_are_rejected_at_construction(self) -> None:
        with pytest.raises(CacheWriteRefused, match="never cacheable"):
            observation(freshness=FreshnessClass.PERISHABLE)

    def test_corrupt_row_is_quarantined_without_hiding_other_symbols(self, tmp_path) -> None:
        path = tmp_path / "observations.sqlite3"
        cache = SQLiteObservationCache(path)
        cache.write_batch([observation(), observation("QQQ")])
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE observations SET payload_json = ? WHERE symbol = ?",
                ("{not-json", "SPY"),
            )
        result = cache.read_many(["SPY", "QQQ"], now=NOW, session_date=NOW.date())
        assert result["SPY"] == ()
        assert len(result["QQQ"]) == 1
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM cache_quarantine").fetchone()[0] == 1

    def test_catalog_and_configuration_versions_filter_reads(self, tmp_path) -> None:
        cache = SQLiteObservationCache(tmp_path / "observations.sqlite3")
        cache.write_batch([observation(catalog_version="old/1")])
        assert cache.read_many(
            ["SPY"], now=NOW, session_date=NOW.date(), catalog_version="new/1"
        )["SPY"] == ()


class TestRefreshFairness:
    def test_never_seen_symbols_are_not_starved_by_rank(self, tmp_path) -> None:
        cache = SQLiteObservationCache(tmp_path / "observations.sqlite3")
        symbols = [f"S{i:03d}" for i in range(10)]
        cache.seed_refresh(
            symbols,
            catalog_version="catalog/1",
            configuration_version="config/1",
            now=NOW,
        )
        seen: set[str] = set()
        for cycle in range(4):
            due = cache.due(
                now=NOW + dt.timedelta(minutes=cycle),
                limit=3,
                catalog_version="catalog/1",
                configuration_version="config/1",
            )
            assert due
            seen.update(state.symbol for state in due)
            for state in due:
                cache.refresh_queue.mark_phase_one(
                    state.symbol,
                    observed_at=NOW + dt.timedelta(minutes=cycle),
                    next_due_at=NOW + dt.timedelta(hours=1),
                    previous_rank=100.0 if state.symbol == "S000" else 1.0,
                )
        assert seen == set(symbols)
