"""Focused R3 tests for scan lifecycle receipts and recovery inspection."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from engine.options.scan_receipts import (
    ReceiptCorrupt,
    ReceiptStateError,
    ScanReceiptKind,
    ScanReceiptStore,
)


NOW = dt.datetime(2026, 8, 14, 9, 30, tzinfo=dt.UTC)


class TestScanReceipts:
    def test_start_and_shard_are_durable_before_terminal_completion(self, tmp_path: Path) -> None:
        store = ScanReceiptStore(tmp_path)
        started = store.start(
            session_id="paperday-1",
            scan_id="scan-1",
            recorded_at=NOW,
            tick_id="tick-1",
            attempt_id="attempt-1",
            expected_shards=2,
        )
        shard = store.shard_completed(
            session_id="paperday-1",
            scan_id="scan-1",
            shard_id="shard-0",
            recorded_at=NOW + dt.timedelta(seconds=5),
            evaluated=40,
        )
        assert started.kind is ScanReceiptKind.SCAN_STARTED
        assert shard.kind is ScanReceiptKind.SCAN_SHARD_COMPLETED
        state = store.state("scan-1")
        assert state is not None
        assert state.unmatched
        assert state.terminal_kind is None
        assert state.shard_ids == ("shard-0",)
        assert len(store.read("scan-1")) == 2

    def test_start_is_idempotent_but_two_scan_starts_are_not(self, tmp_path: Path) -> None:
        store = ScanReceiptStore(tmp_path)
        first = store.start(
            session_id="paperday-1", scan_id="scan-1", recorded_at=NOW
        )
        assert store.start(
            session_id="paperday-1", scan_id="scan-1", recorded_at=NOW
        ) == first
        with pytest.raises(ReceiptStateError):
            store.start(
                session_id="paperday-2",
                scan_id="scan-1",
                recorded_at=NOW,
            )

    def test_complete_clears_unmatched_recovery_state(self, tmp_path: Path) -> None:
        store = ScanReceiptStore(tmp_path)
        store.start(session_id="paperday-1", scan_id="scan-1", recorded_at=NOW)
        store.complete(
            session_id="paperday-1",
            scan_id="scan-1",
            recorded_at=NOW + dt.timedelta(minutes=1),
            payload={"evaluated": 80, "deferred": 0},
        )
        state = store.state("scan-1")
        assert state is not None
        assert state.complete
        assert not state.recovery_required
        assert store.unmatched() == ()
        assert store.scan_is_resolved("scan-1")

    def test_missing_terminal_is_reported_as_unmatched_not_success(self, tmp_path: Path) -> None:
        store = ScanReceiptStore(tmp_path)
        store.start(session_id="paperday-1", scan_id="scan-1", recorded_at=NOW)
        unmatched = store.unmatched(session_id="paperday-1")
        assert [state.scan_id for state in unmatched] == ["scan-1"]
        assert store.recoverable(session_id="paperday-1") == unmatched
        assert not store.scan_is_resolved("scan-1")

    def test_recovery_marker_requires_explicit_clear_before_resolution(self, tmp_path: Path) -> None:
        store = ScanReceiptStore(tmp_path)
        store.start(session_id="paperday-1", scan_id="scan-1", recorded_at=NOW)
        store.recovery_required(
            session_id="paperday-1",
            scan_id="scan-1",
            recorded_at=NOW + dt.timedelta(seconds=1),
            reason="process died during shard publication",
        )
        assert store.state("scan-1").recovery_required  # type: ignore[union-attr]
        store.recovery_cleared(
            session_id="paperday-1",
            scan_id="scan-1",
            recorded_at=NOW + dt.timedelta(seconds=2),
        )
        # Clearing the recovery marker does not claim that the scan itself
        # finished.  The unmatched start remains blocking until the caller
        # records a terminal outcome after reconciliation.
        assert store.state("scan-1").recovery_required  # type: ignore[union-attr]
        store.complete(
            session_id="paperday-1",
            scan_id="scan-1",
            recorded_at=NOW + dt.timedelta(seconds=3),
        )
        assert not store.state("scan-1").recovery_required  # type: ignore[union-attr]

    def test_abort_is_terminal_and_requires_a_reason(self, tmp_path: Path) -> None:
        store = ScanReceiptStore(tmp_path)
        store.start(session_id="paperday-1", scan_id="scan-1", recorded_at=NOW)
        with pytest.raises(ValueError):
            store.abort(
                session_id="paperday-1",
                scan_id="scan-1",
                recorded_at=NOW,
                reason="",
            )
        store.abort(
            session_id="paperday-1",
            scan_id="scan-1",
            recorded_at=NOW + dt.timedelta(seconds=1),
            reason="broker pacing state was ambiguous",
            reconciled=True,
        )
        state = store.state("scan-1")
        assert state is not None
        assert state.terminal_kind is ScanReceiptKind.SCAN_ABORTED
        assert not state.recovery_required
        with pytest.raises(ReceiptStateError):
            store.complete(
                session_id="paperday-1",
                scan_id="scan-1",
                recorded_at=NOW + dt.timedelta(seconds=2),
            )

    def test_receipts_are_individual_immutable_files_and_legacy_jsonl_is_read(self, tmp_path: Path) -> None:
        store = ScanReceiptStore(tmp_path)
        started = store.start(session_id="paperday-1", scan_id="scan-1", recorded_at=NOW)
        path = tmp_path / "scan-receipts" / "records" / f"{started.receipt_id}.json"
        original = path.read_bytes()
        path.write_bytes(original)
        assert path.read_bytes() == original

        legacy = tmp_path / "legacy.jsonl"
        legacy.write_text(
            json.dumps(
                {
                    "version": "ibkr.scan.receipt/1",
                    "receipt_id": "legacy-start",
                    "kind": "SCAN_STARTED",
                    "session_id": "paperday-legacy",
                    "scan_id": "scan-legacy",
                    "recorded_at": NOW.isoformat(),
                    "tick_id": None,
                    "attempt_id": None,
                    "shard_id": None,
                    "payload": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        migrated = ScanReceiptStore(tmp_path, legacy_jsonl=legacy)
        assert migrated.read("scan-legacy")[0].receipt_id == "legacy-start"
        assert {state.scan_id for state in migrated.unmatched()} == {"scan-1", "scan-legacy"}

    def test_corrupt_receipt_fails_closed_during_recovery_scan(self, tmp_path: Path) -> None:
        store = ScanReceiptStore(tmp_path)
        started = store.start(session_id="paperday-1", scan_id="scan-1", recorded_at=NOW)
        path = tmp_path / "scan-receipts" / "records" / f"{started.receipt_id}.json"
        path.write_text("not-json", encoding="utf-8")
        with pytest.raises(ReceiptCorrupt):
            store.unmatched()
