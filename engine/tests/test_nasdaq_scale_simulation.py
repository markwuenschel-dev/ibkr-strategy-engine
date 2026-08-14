"""Synthetic scale checks for the catalog/refresh foundation."""

from __future__ import annotations

import datetime as dt

import pytest

from engine.options.catalog import CatalogEntry, CatalogSnapshot
from engine.options.observation_cache import FairRefreshQueue


@pytest.mark.parametrize("count", [500, 2_000, 10_000])
def test_refresh_queue_can_seed_and_select_large_catalogs(tmp_path, count: int) -> None:
    now = dt.datetime(2026, 8, 14, 14, 0, tzinfo=dt.timezone.utc)
    snapshot = CatalogSnapshot(
        version=f"synthetic/{count}",
        entries=tuple(
            CatalogEntry(
                symbol=f"N{i:05d}",
                listing_venue="NASDAQ.NMS",
                optionability=True,
                sector="SYNTHETIC",
                correlation_group=f"G{i % 50}",
                entry_eligible=True,
            )
            for i in range(count)
        ),
    )
    queue = FairRefreshQueue(tmp_path / f"scale-{count}.sqlite3")
    queue.seed(
        snapshot.symbols,
        catalog_version=snapshot.catalog_hash,
        configuration_version="config/1",
        now=now,
    )
    due = queue.select_due(
        now=now,
        limit=25,
        catalog_version=snapshot.catalog_hash,
        configuration_version="config/1",
    )
    assert len(due) == 25
    assert len({state.symbol for state in due}) == 25
