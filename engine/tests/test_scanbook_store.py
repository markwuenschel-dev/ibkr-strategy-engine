"""Focused R3 tests for immutable ScanBook publication and claim ownership."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from engine.options.scanbook_store import (
    ClaimConflict,
    ClaimCorrupt,
    ClaimLedger,
    ClaimState,
    ImmutableScanBookClaimWriter,
    ImmutableSnapshotError,
    ObservationAges,
    PhaseCoverage,
    ScanBookSnapshot,
    ScanBookSnapshotStore,
    SnapshotAdmission,
    SnapshotCorrupt,
)


NOW = dt.datetime(2026, 8, 14, 10, 0, tzinfo=dt.UTC)
TODAY = NOW.date()


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def snapshot(
    *,
    scan_id: str = "scan-1",
    generated_at: dt.datetime = NOW,
    rows: tuple[dict[str, object], ...] = (
        {"symbol": "SPY", "state": "CANDIDATE", "facts": {"ivr": 61}},
        {"symbol": "QQQ", "state": "INELIGIBLE", "facts": {"ivr": 41}},
    ),
    expected: int = 2,
    evaluated: int = 2,
    deferred: int = 0,
    unavailable: int = 0,
    catalog: str = "catalog",
    policy: str = "policy",
    calendar: str = "calendar",
    config: str = "config",
    breadth_complete: bool = True,
) -> ScanBookSnapshot:
    return ScanBookSnapshot(
        scan_id=scan_id,
        session_id="paperday-1",
        session_date=TODAY,
        generated_at=generated_at,
        catalog_hash=digest(catalog),
        policy_hash=digest(policy),
        calendar_hash=digest(calendar),
        config_hash=digest(config),
        expected_symbols=expected,
        evaluated_symbols=evaluated,
        deferred_symbols=deferred,
        unavailable_symbols=unavailable,
        rows=rows,
        phase_coverage={
            "breadth": PhaseCoverage(
                expected=expected,
                completed=expected if breadth_complete else max(expected - 1, 0),
                deferred=deferred,
                unavailable=unavailable,
                required=True,
            ),
            "deep": PhaseCoverage(
                expected=1,
                completed=1,
                required=True,
            ),
        },
        observation_ages=ObservationAges(oldest_seconds=120, newest_seconds=15),
        pacing_snapshot={"general_tokens": 17, "reserved": 4},
        cycle_state={"cycle_id": "cycle-1", "status": "COMPLETE"},
        shard_state={"0": {"status": "COMPLETE"}},
        tick_id="tick-1",
        attempt_id="attempt-1",
    )


class TestImmutableSnapshots:
    def test_constructor_recursively_freezes_rows_and_diagnostics(self) -> None:
        row = {"symbol": "SPY", "facts": {"source": ["cache"]}}
        pacing = {"reserve": {"historical": 10}}
        book = ScanBookSnapshot(
            **{
                **snapshot(rows=(row, {"symbol": "QQQ"})).__dict__,
                "pacing_snapshot": pacing,
            }
        )

        row["facts"]["source"].append("mutated-after-construction")  # type: ignore[index]
        pacing["reserve"] = {"historical": 0}
        assert book.rows[0]["facts"]["source"] == ("cache",)
        assert book.pacing_snapshot["reserve"]["historical"] == 10  # type: ignore[index]
        with pytest.raises(TypeError):
            book.rows[0]["state"] = "CORRUPTED"  # type: ignore[index]

    def test_snapshot_id_is_immutable_and_same_content_is_idempotent(self, tmp_path: Path) -> None:
        store = ScanBookSnapshotStore(tmp_path)
        first = snapshot()
        path = store.publish(first)
        assert path.exists()
        assert store.read_snapshot("scan-1", TODAY) == first

        # A retry of the same immutable publication is harmless.
        assert store.publish(first) == path

        different = snapshot(scan_id="scan-1", rows=({"symbol": "SPY"}, {"symbol": "QQQ"}))
        with pytest.raises(ImmutableSnapshotError):
            store.publish(different)

    def test_latest_pointer_is_atomic_and_never_rewrites_old_snapshot(self, tmp_path: Path) -> None:
        store = ScanBookSnapshotStore(tmp_path)
        first = snapshot(scan_id="scan-1")
        second = snapshot(scan_id="scan-2", generated_at=NOW + dt.timedelta(minutes=1))
        first_path = store.publish(first)
        first_bytes = first_path.read_bytes()
        store.publish(second)

        assert store.read_latest(TODAY) == second
        assert first_path.read_bytes() == first_bytes
        pointer = json.loads(store.latest_pointer_path(TODAY).read_text(encoding="utf-8"))
        assert pointer["scan_id"] == "scan-2"
        assert not list((tmp_path / "scanbook" / "latest").glob("*.tmp"))

    def test_current_complete_snapshot_is_entry_admissible(self, tmp_path: Path) -> None:
        store = ScanBookSnapshotStore(tmp_path)
        store.publish(snapshot())
        result = store.admit_latest(
            session_date=TODAY,
            now=NOW + dt.timedelta(minutes=5),
            max_age=dt.timedelta(hours=1),
            catalog_hash=digest("catalog"),
            policy_hash=digest("policy"),
            calendar_hash=digest("calendar"),
            config_hash=digest("config"),
        )
        assert result.status is SnapshotAdmission.ACCEPTED
        assert result.entry_admissible

    @pytest.mark.parametrize(
        ("kwargs", "status"),
        [
            ({"evaluated": 1, "deferred": 1, "breadth_complete": False}, SnapshotAdmission.INCOMPLETE),
            ({"generated_at": NOW - dt.timedelta(hours=2)}, SnapshotAdmission.STALE),
        ],
    )
    def test_partial_and_stale_books_are_not_entry_admissible(
        self,
        tmp_path: Path,
        kwargs: dict[str, object],
        status: SnapshotAdmission,
    ) -> None:
        store = ScanBookSnapshotStore(tmp_path)
        store.publish(snapshot(**kwargs))  # type: ignore[arg-type]
        result = store.admit_latest(
            session_date=TODAY,
            now=NOW,
            max_age=dt.timedelta(hours=1),
            catalog_hash=digest("catalog"),
            policy_hash=digest("policy"),
            calendar_hash=digest("calendar"),
            config_hash=digest("config"),
        )
        assert result.status is status
        assert not result.entry_admissible
        assert result.diagnostic_only

    def test_wrong_manifest_future_and_missing_latest_fail_closed(self, tmp_path: Path) -> None:
        store = ScanBookSnapshotStore(tmp_path)
        assert store.admit_latest(
            session_date=TODAY,
            now=NOW,
            max_age=dt.timedelta(hours=1),
            catalog_hash=digest("catalog"),
            policy_hash=digest("policy"),
            calendar_hash=digest("calendar"),
            config_hash=digest("config"),
        ).status is SnapshotAdmission.MISSING

        store.publish(snapshot())
        wrong = store.admit_latest(
            session_date=TODAY,
            now=NOW,
            max_age=dt.timedelta(hours=1),
            catalog_hash=digest("changed-catalog"),
            policy_hash=digest("policy"),
            calendar_hash=digest("calendar"),
            config_hash=digest("config"),
        )
        assert wrong.status is SnapshotAdmission.MANIFEST_MISMATCH
        assert not wrong.entry_admissible

        future_store = ScanBookSnapshotStore(tmp_path / "future")
        future_store.publish(snapshot(generated_at=NOW + dt.timedelta(minutes=1)))
        future = future_store.admit_latest(
            session_date=TODAY,
            now=NOW,
            max_age=dt.timedelta(hours=1),
            catalog_hash=digest("catalog"),
            policy_hash=digest("policy"),
            calendar_hash=digest("calendar"),
            config_hash=digest("config"),
        )
        assert future.status is SnapshotAdmission.FUTURE

    def test_corrupt_latest_pointer_is_diagnostic_not_admissible(self, tmp_path: Path) -> None:
        store = ScanBookSnapshotStore(tmp_path)
        store.publish(snapshot())
        store.latest_pointer_path(TODAY).write_text("{not-json", encoding="utf-8")
        result = store.admit_latest(
            session_date=TODAY,
            now=NOW,
            max_age=dt.timedelta(hours=1),
            catalog_hash=digest("catalog"),
            policy_hash=digest("policy"),
            calendar_hash=digest("calendar"),
            config_hash=digest("config"),
        )
        assert result.status is SnapshotAdmission.CORRUPT

    def test_snapshot_file_corruption_is_not_silently_treated_as_missing(self, tmp_path: Path) -> None:
        store = ScanBookSnapshotStore(tmp_path)
        path = store.publish(snapshot())
        path.write_text("{}", encoding="utf-8")
        with pytest.raises(SnapshotCorrupt):
            store.read_latest(TODAY)


class TestClaimLedger:
    def test_claim_is_separate_from_snapshot_publication_and_idempotent(self, tmp_path: Path) -> None:
        snapshot_store = ScanBookSnapshotStore(tmp_path)
        claims = ClaimLedger(tmp_path)
        snapshot_store.publish(snapshot(scan_id="scan-1"))
        claimed = claims.claim("scan-1", "SPY", "entry-1", at=NOW)
        snapshot_store.publish(snapshot(scan_id="scan-2", generated_at=NOW + dt.timedelta(minutes=1)))

        assert claims.read("SPY") == claimed
        assert claims.read("SPY").state is ClaimState.CLAIMED  # type: ignore[union-attr]
        assert claims.claim("scan-2", "SPY", "entry-1", at=NOW) == claimed
        assert len(claims.history("SPY")) == 1

    def test_second_owner_and_stale_version_are_rejected(self, tmp_path: Path) -> None:
        claims = ClaimLedger(tmp_path)
        first = claims.claim("scan-1", "SPY", "entry-1", at=NOW)
        with pytest.raises(ClaimConflict) as owner_error:
            claims.claim("scan-2", "SPY", "entry-2", at=NOW)
        assert owner_error.value.current == first

        with pytest.raises(ClaimConflict) as version_error:
            claims.compare_and_set(
                "SPY",
                expected_version=0,
                state=ClaimState.RELEASED,
                owner_id="entry-1",
                scan_id="scan-1",
                symbol="SPY",
                at=NOW,
            )
        assert version_error.value.actual_version == 1
        assert claims.read("SPY") == first

    def test_release_then_new_owner_is_a_new_cas_version(self, tmp_path: Path) -> None:
        claims = ClaimLedger(tmp_path)
        first = claims.claim("scan-1", "SPY", "entry-1", at=NOW)
        released = claims.release(
            "SPY",
            owner_id="entry-1",
            expected_version=first.version,
            scan_id="scan-1",
            symbol="SPY",
            at=NOW + dt.timedelta(minutes=1),
        )
        second = claims.claim("scan-2", "SPY", "entry-2", at=NOW + dt.timedelta(minutes=2))
        assert released.state is ClaimState.RELEASED
        assert second.state is ClaimState.CLAIMED
        assert second.version == 3
        assert [record.state for record in claims.history("SPY")] == [
            ClaimState.CLAIMED,
            ClaimState.RELEASED,
            ClaimState.CLAIMED,
        ]

    def test_event_survives_loss_of_rebuildable_current_pointer(self, tmp_path: Path) -> None:
        claims = ClaimLedger(tmp_path)
        claimed = claims.claim("scan-1", "QQQ", "entry-1", at=NOW)
        current = claims._current_path("QQQ")
        current.unlink()

        assert claims.read("QQQ") == claimed
        assert claims.repair_current("QQQ") == claimed
        assert current.exists()
        assert claims.read("QQQ") == claimed

    def test_corrupt_event_stream_fails_closed(self, tmp_path: Path) -> None:
        claims = ClaimLedger(tmp_path)
        claimed = claims.claim("scan-1", "IWM", "entry-1", at=NOW)
        event = next((tmp_path / "claims" / "events").rglob("*.json"))
        event.write_text(json.dumps({"version": "bad"}), encoding="utf-8")
        with pytest.raises(ClaimCorrupt):
            claims.read("IWM")
        assert claimed.version == 1

    def test_active_only_returns_current_claims(self, tmp_path: Path) -> None:
        claims = ClaimLedger(tmp_path)
        first = claims.claim("scan-1", "SPY", "entry-1", at=NOW)
        claims.claim("scan-1", "QQQ", "entry-2", at=NOW)
        claims.release(
            "SPY",
            owner_id="entry-1",
            expected_version=first.version,
            scan_id="scan-1",
            symbol="SPY",
            at=NOW + dt.timedelta(minutes=1),
        )
        assert [record.symbol for record in claims.active()] == ["QQQ"]


class TestImmutableClaimWriter:
    def test_candidate_claim_is_idempotent_and_does_not_rewrite_snapshot(
        self, tmp_path: Path
    ) -> None:
        snapshots = ScanBookSnapshotStore(tmp_path)
        claims = ClaimLedger(tmp_path)
        original = snapshot(scan_id="scan-1")
        snapshots.publish(original)
        writer = ImmutableScanBookClaimWriter(
            snapshots,
            claims,
            session_date=TODAY,
            owner_id="paperday-1",
        )

        assert writer.mark_claimed("SPY", entry_id="entry-1", at=NOW)
        assert writer.mark_claimed("SPY", entry_id="entry-1", at=NOW)
        assert not writer.mark_claimed("SPY", entry_id="entry-2", at=NOW)
        assert snapshots.read_snapshot("scan-1", TODAY) == original
        assert claims.read("SPY").claim_id == "entry-1"  # type: ignore[union-attr]

    def test_non_candidate_latest_snapshot_cannot_claim_an_old_candidate(
        self, tmp_path: Path
    ) -> None:
        snapshots = ScanBookSnapshotStore(tmp_path)
        claims = ClaimLedger(tmp_path)
        snapshots.publish(snapshot(scan_id="scan-1"))
        writer = ImmutableScanBookClaimWriter(
            snapshots,
            claims,
            session_date=TODAY,
            owner_id="paperday-1",
        )
        assert writer.mark_claimed("SPY", entry_id="entry-1", at=NOW)

        replacement = snapshot(
            scan_id="scan-2",
            generated_at=NOW + dt.timedelta(minutes=1),
            rows=(
                {"symbol": "SPY", "state": "INELIGIBLE", "facts": {}},
                {"symbol": "QQQ", "state": "INELIGIBLE", "facts": {}},
            ),
        )
        snapshots.publish(replacement)
        assert not writer.mark_claimed("QQQ", entry_id="entry-2", at=NOW)
        assert claims.read("SPY").scan_id == "scan-1"  # type: ignore[union-attr]

    def test_admitted_scan_id_rejects_publication_interleaving_before_claim(
        self, tmp_path: Path
    ) -> None:
        snapshots = ScanBookSnapshotStore(tmp_path)
        claims = ClaimLedger(tmp_path)
        first = snapshot(scan_id="scan-1")
        snapshots.publish(first)

        admitted = snapshots.read_latest(TODAY)
        assert admitted is not None
        writer = ImmutableScanBookClaimWriter(
            snapshots,
            claims,
            session_date=TODAY,
            owner_id="paperday-1",
        ).for_snapshot(admitted.scan_id)

        # Model the interleaving between admission and the logical-entry CAS:
        # the replaceable latest pointer advances before the claim is attempted.
        snapshots.publish(
            snapshot(
                scan_id="scan-2",
                generated_at=NOW + dt.timedelta(minutes=1),
                rows=(
                    {"symbol": "SPY", "state": "CANDIDATE", "facts": {"ivr": 72}},
                    {"symbol": "QQQ", "state": "INELIGIBLE", "facts": {}},
                ),
            )
        )

        assert not writer.mark_claimed("SPY", entry_id="entry-1", at=NOW)
        assert claims.read("SPY") is None
