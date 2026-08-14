"""Focused proof for the catalog-backed unattended universe path.

These tests intentionally use a small catalog and a local SQLite state root.
The legacy scanner tests remain the compatibility proof; this file proves the
new seams around it: one batch cache read, durable fairness, pacing-reserved
deep work, immutable publication, and receipt recovery.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

import engine.options.universe as universe_module
from engine.options.catalog import CatalogEntry, UniverseCatalog
from engine.options.freshness import FreshnessClass, ObservationEnvelope
from engine.options.ivrank import IVObservation
from engine.options.observation_cache import RawObservation, SQLiteObservationCache
from engine.options.pacing import RequestKind
from engine.options.pacing_ledger import PacingLedger
from engine.options.policy import RiskPolicy
from engine.options.regime import VolatilityRegimePolicy
from engine.options.scan_receipts import ScanReceiptKind, ScanReceiptStore
from engine.options.scanbook_store import ScanBookSnapshotStore
from engine.options.universe import (
    CoverageSummary,
    REASON_REFRESH_NOT_REACHED,
    ScanBook,
    ScanBookRow,
    ScanState,
    StructureNomination,
    UniverseScanConfig,
    UniverseScanRecoveryRequired,
    NominatedLeg,
    run_catalog_universe_pass,
)


NOW = dt.datetime(2026, 8, 14, 14, 30, tzinfo=dt.timezone.utc)


def _catalog(*symbols: str) -> UniverseCatalog:
    return UniverseCatalog(
        [
            CatalogEntry(
                symbol=symbol,
                listing_venue="NYSE ARCA",
                optionability=True,
                sector="BROAD_MARKET",
                correlation_group="US_LARGE_CAP",
                scan_eligible=True,
                entry_eligible=True,
            )
            for symbol in symbols
        ],
        version="catalog-test/1",
        source="focused-test",
    )


def _series(*, high: bool) -> list[IVObservation]:
    values = [Decimal("0.10") + Decimal(index) / Decimal("200") for index in range(70)]
    if not high:
        values = list(reversed(values))
    return [
        IVObservation(
            on=NOW.date() - dt.timedelta(days=69 - index),
            implied_volatility=value,
        )
        for index, value in enumerate(values)
    ]


def _seed_cache(
    cache: SQLiteObservationCache,
    catalog: UniverseCatalog,
    symbols: tuple[str, ...],
    *,
    high: bool,
    observed_at: dt.datetime = NOW,
) -> None:
    version = UniverseScanConfig().version
    updates = [
        RawObservation(
            symbol=symbol,
            key="iv-history",
            payload={
                "observations": [
                    {"on": item.on.isoformat(), "iv": str(item.implied_volatility)}
                    for item in _series(high=high)
                ]
            },
            envelope=ObservationEnvelope(
                symbol=symbol,
                session_date=observed_at.date(),
                observed_at=observed_at,
                expires_at=observed_at + dt.timedelta(hours=20),
                source="focused-test",
                freshness_class=FreshnessClass.SLOW_OBSERVATION,
                configuration_version=version,
            ),
            catalog_version=catalog.catalog_version,
        )
        for symbol in symbols
    ]
    cache.write_batch(updates)


def _run(
    root: Path,
    catalog: UniverseCatalog,
    *,
    cache: SQLiteObservationCache,
    ledger: PacingLedger | None = None,
    history=None,
    contract_data=None,
    market_data=None,
    config: UniverseScanConfig | None = None,
    session_id: str = "session",
    tick_id: str | None = None,
    attempt_id: str | None = None,
    now: dt.datetime = NOW,
):
    state = root / "state"
    return run_catalog_universe_pass(
        catalog=catalog,
        observation_cache=cache,
        pacing_ledger=ledger or PacingLedger(state / "pacing.sqlite3"),
        snapshot_store=ScanBookSnapshotStore(state),
        receipt_store=ScanReceiptStore(state),
        session_id=session_id,
        session_date=now.date(),
        policy_hash="1" * 64,
        calendar_hash="2" * 64,
        config_hash="3" * 64,
        policy=RiskPolicy(),
        regime_policy=VolatilityRegimePolicy(),
        config=config or UniverseScanConfig(phase2_limit=1),
        volatility_history=history,
        contract_data=contract_data,
        market_data=market_data,
        now=now,
        tick_id=tick_id,
        attempt_id=attempt_id,
    )


class CountingCache:
    """Proxy proving the adapter performs one breadth batch read."""

    def __init__(self, inner: SQLiteObservationCache) -> None:
        self.inner = inner
        self.refresh_queue = inner.refresh_queue
        self.read_calls: list[tuple[str, ...]] = []

    def read_many(self, symbols, **kwargs):
        normalized = tuple(symbols)
        self.read_calls.append(normalized)
        return self.inner.read_many(normalized, **kwargs)

    def write_batch(self, updates):
        return self.inner.write_batch(updates)


class RecordingHistory:
    def __init__(self, series: dict[str, list[IVObservation]]) -> None:
        self.series = series
        self.calls: list[str] = []

    def implied_volatility_history(self, symbol: str, **_) -> list[IVObservation]:
        self.calls.append(symbol)
        return list(self.series[symbol])


def test_integrated_path_reuses_cache_and_publishes_complete_manifest(tmp_path: Path) -> None:
    catalog = _catalog("AAA", "BBB", "CCC")
    inner = SQLiteObservationCache(tmp_path / "observations.sqlite3")
    cache = CountingCache(inner)
    _seed_cache(inner, catalog, ("AAA", "BBB", "CCC"), high=False)

    result = _run(tmp_path, catalog, cache=cache)

    assert cache.read_calls == [("AAA", "BBB", "CCC")]
    assert result.complete
    assert result.snapshot.expected_symbols == 3
    assert result.snapshot.evaluated_symbols == 3
    assert result.snapshot.deferred_symbols == 0
    assert result.snapshot.unavailable_symbols == 0
    assert len(result.snapshot.rows) == 3
    assert result.snapshot.catalog_hash == catalog.catalog_hash
    assert result.snapshot.tick_id == result.tick_id
    assert result.snapshot.attempt_id == result.attempt_id
    assert ScanBookSnapshotStore(tmp_path / "state").read_latest(NOW.date()).scan_id == result.scan_id

    receipts = ScanReceiptStore(tmp_path / "state").read(result.scan_id)
    assert {receipt.kind for receipt in receipts} == {
        ScanReceiptKind.SCAN_STARTED,
        ScanReceiptKind.SCAN_SHARD_COMPLETED,
        ScanReceiptKind.SCAN_COMPLETED,
    }


def test_fair_refresh_ring_rotates_never_seen_symbols_and_records_outcomes(tmp_path: Path) -> None:
    symbols = ("AAA", "BBB", "CCC", "DDD")
    catalog = _catalog(*symbols)
    cache = SQLiteObservationCache(tmp_path / "observations.sqlite3")
    stale = NOW - dt.timedelta(days=2)
    _seed_cache(cache, catalog, symbols, high=False, observed_at=stale)
    history = RecordingHistory({symbol: _series(high=False) for symbol in symbols})
    config = UniverseScanConfig(refresh_limit=2, phase2_limit=1)

    first = _run(tmp_path, catalog, cache=cache, history=history, config=config, tick_id="t1", attempt_id="a1")
    assert first.diagnostic_only
    first_refreshed = {symbol for symbol in history.calls}
    assert len(first_refreshed) == 2
    assert all(
        next(row for row in first.book.rows if row.symbol == symbol).observation.value == "REFRESHED"
        for symbol in first_refreshed
    )
    assert {
        row.reason for row in first.book.rows if row.symbol not in first_refreshed
    } == {REASON_REFRESH_NOT_REACHED}

    history.calls.clear()
    second = _run(tmp_path, catalog, cache=cache, history=history, config=config, tick_id="t2", attempt_id="a2")
    assert set(history.calls).isdisjoint(first_refreshed)
    assert set(history.calls) == set(symbols) - first_refreshed
    assert second.complete


class CheapContractData:
    def expirations(self, symbol: str) -> list[str]:
        return [(NOW.date() + dt.timedelta(days=45)).strftime("%Y%m%d")]

    def strikes(self, symbol: str, expiry: str, right: str):
        return [Decimal("100"), Decimal("105")]

    def qualify(self, symbol: str, expiry: str, strikes, right: str):
        # Empty qualification ends phase two after three general requests,
        # which is enough to prove the first reservation consumed budget.
        return []


def test_deep_shortlist_reserves_cost_and_preserves_management_floor(tmp_path: Path) -> None:
    catalog = _catalog("AAA", "BBB")
    cache = SQLiteObservationCache(tmp_path / "observations.sqlite3")
    _seed_cache(cache, catalog, ("AAA", "BBB"), high=True)
    ledger = PacingLedger(
        tmp_path / "pacing.sqlite3",
        general_per_window=5,
        management_reserve_fraction=0.20,
    )

    result = _run(
        tmp_path,
        catalog,
        cache=cache,
        ledger=ledger,
        config=UniverseScanConfig(phase2_limit=2, phase2_request_cost=4),
        contract_data=CheapContractData(),
    )

    rows = {row.symbol: row for row in result.book.rows}
    assert rows["AAA"].state is ScanState.INELIGIBLE_LIQUIDITY
    assert rows["BBB"].state is ScanState.DEFERRED_PACING
    general = result.snapshot.pacing_snapshot[RequestKind.GENERAL.value]
    assert general["management_reserve"] == 1
    assert general["consumed"] == 3


def test_unmatched_scan_blocks_new_work_until_recovery(tmp_path: Path) -> None:
    catalog = _catalog("AAA")
    cache = SQLiteObservationCache(tmp_path / "observations.sqlite3")
    _seed_cache(cache, catalog, ("AAA",), high=False)
    state = tmp_path / "state"
    receipts = ScanReceiptStore(state)
    receipts.start(
        session_id="session",
        scan_id="old-scan",
        recorded_at=NOW,
        tick_id="old-tick",
        attempt_id="old-attempt",
    )

    with pytest.raises(UniverseScanRecoveryRequired):
        _run(tmp_path, catalog, cache=cache, tick_id="new-tick", attempt_id="new-attempt")

    assert receipts.unmatched(session_id="session")[0].scan_id == "old-scan"


def test_failure_after_start_leaves_an_explicit_abort_receipt(tmp_path: Path, monkeypatch) -> None:
    catalog = _catalog("AAA")
    cache = SQLiteObservationCache(tmp_path / "observations.sqlite3")
    _seed_cache(cache, catalog, ("AAA",), high=False)

    def explode(**_kwargs):
        raise RuntimeError("synthetic scan failure")

    monkeypatch.setattr(universe_module, "run_universe_pass", explode)
    with pytest.raises(RuntimeError, match="synthetic scan failure"):
        _run(tmp_path, catalog, cache=cache, tick_id="abort-tick", attempt_id="abort-attempt")

    receipts = ScanReceiptStore(tmp_path / "state").read("session_abort-tick_abort-attempt")
    assert {receipt.kind for receipt in receipts} == {
        ScanReceiptKind.SCAN_STARTED,
        ScanReceiptKind.SCAN_ABORTED,
    }


def test_catalog_snapshot_never_promotes_unclassified_candidate(tmp_path: Path, monkeypatch) -> None:
    catalog = UniverseCatalog(
        [
            CatalogEntry(
                symbol="UNKNOWN",
                listing_venue="NASDAQ.NMS",
                optionability=None,
                sector="TECHNOLOGY",
                correlation_group="SECTOR_TECH",
                scan_eligible=True,
                entry_eligible=True,
            )
        ],
        version="catalog-unknown/1",
    )
    cache = SQLiteObservationCache(tmp_path / "observations.sqlite3")
    _seed_cache(cache, catalog, ("UNKNOWN",), high=True)

    nomination = StructureNomination(
        underlying="UNKNOWN",
        family="SHORT_PREMIUM",
        direction="BULLISH",
        expiration=NOW.date() + dt.timedelta(days=45),
        legs=(
            NominatedLeg(con_id=1050, strike=Decimal("105"), right="P", action="SELL"),
            NominatedLeg(con_id=1000, strike=Decimal("100"), right="P", action="BUY"),
        ),
        short_delta=Decimal("-0.30"),
        width=Decimal("5"),
    )
    candidate = ScanBookRow(
        symbol="UNKNOWN",
        state=ScanState.CANDIDATE,
        nomination=nomination,
    )

    def fake_pass(**_kwargs):
        return ScanBook(
            session_date=NOW.date(),
            generated_at=NOW,
            rows=(candidate,),
            coverage=CoverageSummary.from_rows((candidate,)),
        )

    monkeypatch.setattr(universe_module, "run_universe_pass", fake_pass)
    result = _run(tmp_path, catalog, cache=cache)

    assert result.book.rows[0].state is ScanState.INELIGIBLE_REGIME
    assert result.book.rows[0].reason == "UNIVERSE_CATALOG_ENTRY_INELIGIBLE"
    assert result.snapshot.rows[0]["automated_entry_allowed"] is False
