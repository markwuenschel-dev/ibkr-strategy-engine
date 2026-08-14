"""Focused contract tests for the persistent options-cycle subprocess seam."""

from __future__ import annotations

from pathlib import Path
import datetime as dt
import json

import pytest

from engine.autocycle import (
    AutoCycleConfig,
    CycleContext,
    CycleError,
    CyclePhases,
    CycleMode,
    DueSlot,
    FixedRateSchedule,
    JobKind,
    OptionsCycleWorker,
    RECEIPT_SCHEMA,
    ReceiptStore,
    ReceiptKind,
)
from engine import runtime

NOW = dt.datetime(2026, 8, 14, 14, 0, tzinfo=dt.timezone.utc)


def test_fixed_rate_schedule_persists_slot_selection_until_tick_acknowledgement(
    tmp_path: Path,
):
    now = dt.datetime(2026, 8, 14, 14, 0, tzinfo=dt.timezone.utc)
    identity = {
        "session_id": "session",
        "lease_nonce": "nonce",
        "policy_hash": "a" * 64,
        "catalog_hash": "b" * 64,
    }
    schedule = FixedRateSchedule(
        tmp_path / "autocycle",
        anchor=now - dt.timedelta(minutes=5),
        cadences={job: 300 for job in JobKind},
        **identity,
    )

    selected = schedule.due(now)
    assert selected
    assert schedule.unresolved() is True
    assert schedule.due(now + dt.timedelta(minutes=5)) == ()

    restarted = FixedRateSchedule(
        tmp_path / "autocycle",
        anchor=now - dt.timedelta(minutes=5),
        cadences={job: 300 for job in JobKind},
        **identity,
    )
    assert restarted.pending_slots() == selected

    restarted.acknowledge(selected)
    assert restarted.unresolved() is False
    assert restarted.due(now) == ()


def test_corrupt_cycle_receipts_fail_closed_instead_of_being_skipped(tmp_path: Path):
    receipts = ReceiptStore(tmp_path / "autocycle")
    receipts.emit(ReceiptKind.TICK_STARTED, tick_id="tick", attempt_id="attempt")
    event = next((tmp_path / "autocycle" / "receipts").glob("[0-9]*-*.json"))
    event.write_text("{not-json", encoding="utf-8")

    with pytest.raises(CycleError, match="FAIL-RECOVERY-BLOCKED"):
        receipts.unmatched_ticks()


def test_corrupt_cycle_sequence_cannot_restart_at_zero(tmp_path: Path):
    receipts = ReceiptStore(tmp_path / "autocycle")
    receipts.emit(ReceiptKind.TICK_STARTED, tick_id="tick", attempt_id="attempt")
    receipts.sequence_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(CycleError, match="FAIL-RECOVERY-BLOCKED"):
        receipts.emit(ReceiptKind.TICK_FINISHED, tick_id="tick")


def test_receipt_sequence_schema_and_filename_are_fail_closed(tmp_path: Path):
    root = tmp_path / "autocycle"
    receipts = ReceiptStore(root)
    events = root / "receipts"
    events.mkdir(parents=True)
    receipts.sequence_path.write_text(
        json.dumps({"schema": "wrong/1", "sequence": 1}), encoding="utf-8"
    )
    with pytest.raises(CycleError, match="receipt sequence is corrupt"):
        receipts.emit(ReceiptKind.TICK_STARTED, at=NOW)

    receipts.sequence_path.write_text(
        json.dumps({"schema": RECEIPT_SCHEMA, "sequence": 1}), encoding="utf-8"
    )
    (events / "00000000000000000001-TICK_STARTED.json").write_text(
        json.dumps(
            {
                "schema": RECEIPT_SCHEMA,
                "sequence": 2,
                "event": ReceiptKind.TICK_STARTED.value,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CycleError, match="receipt sequence collision"):
        receipts.records()


def test_fixed_rate_schedule_identity_mismatch_fails_closed(tmp_path: Path):
    now = dt.datetime(2026, 8, 14, 14, 0, tzinfo=dt.timezone.utc)
    kwargs = {
        "anchor": now - dt.timedelta(minutes=5),
        "cadences": {job: 300 for job in JobKind},
        "session_id": "session",
        "lease_nonce": "nonce",
        "policy_hash": "a" * 64,
        "catalog_hash": "b" * 64,
    }
    schedule = FixedRateSchedule(tmp_path / "autocycle", **kwargs)
    schedule.due(now)

    with pytest.raises(CycleError, match="FAIL-STALE-PAPERDAY-AUTHORITY"):
        FixedRateSchedule(
            tmp_path / "autocycle",
            **{**kwargs, "lease_nonce": "replacement-nonce"},
        ).unresolved()


def test_unbound_existing_schedule_cannot_be_upgraded_to_current_authority(
    tmp_path: Path,
):
    now = dt.datetime(2026, 8, 14, 14, 0, tzinfo=dt.timezone.utc)
    schedule = FixedRateSchedule(
        tmp_path / "autocycle",
        anchor=now - dt.timedelta(minutes=5),
        cadences={job: 300 for job in JobKind},
    )
    schedule.due(now)

    with pytest.raises(CycleError, match="refusing to adopt"):
        schedule.bind_identity(
            session_id="session",
            lease_nonce="nonce",
            policy_hash="a" * 64,
            catalog_hash="b" * 64,
        )


def test_persistent_worker_does_not_receive_scheduler_timeout(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    runtime.EngineCommandRunner(tmp_path).run(["options-cycle", "--policy"], timeout=17.0)

    assert "timeout" not in captured
    assert captured["check"] is False


def test_legacy_command_keeps_finite_timeout(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    runtime.EngineCommandRunner(tmp_path).run(["options-run", "--arm"], timeout=17.0)

    assert captured["timeout"] == 17.0


def test_unmatched_tick_cannot_call_entry_even_when_recovery_callback_clears(
    tmp_path: Path,
):
    now = dt.datetime(2026, 8, 14, 14, 0, tzinfo=dt.timezone.utc)
    hashes = {"policy_hash": "a" * 64, "catalog_hash": "b" * 64}
    config = AutoCycleConfig(
        mandate="FULL",
        mode=CycleMode.REVIEW_ONLY,
        management_seconds=300,
        discovery_seconds=1800,
        probe_seconds=600,
        entry_seconds=300,
        missed_tick_policy="SKIP_MISSED_TICKS",
        entry_start=dt.time(10, 0),
        entry_end=dt.time(15, 0),
        coverage_sla_seconds=86400,
        max_pending_entries=3,
        max_new_entries_per_pass=1,
        phase2_limit=5,
        state_dir=tmp_path,
        **hashes,
    )
    receipts = ReceiptStore(tmp_path / "autocycle")
    receipts.emit(
        ReceiptKind.TICK_STARTED,
        context=CycleContext(
            session_id="session",
            lease_nonce="nonce",
            tick_id="old-tick",
            attempt_id="old-attempt",
            started_at=now,
            session_date=now.date(),
            **hashes,
        ),
    )
    calls: list[str] = []

    schedule = FixedRateSchedule(
        tmp_path / "autocycle",
        anchor=now - dt.timedelta(minutes=5),
        cadences={job: 300 for job in JobKind},
    )
    worker = OptionsCycleWorker(
        config=config,
        session_id="session",
        lease_nonce="nonce",
        broker_factory=lambda: object(),
        phases=CyclePhases(
            management=lambda _context: calls.append("management") or {},
            discovery=lambda _context: calls.append("discovery") or {},
            probe=lambda _context: calls.append("probe") or {},
            entry=lambda _context: calls.append("entry") or {"transmissions": 1},
            reconcile=lambda _context: {"recovery_cleared": True, "reason": "test"},
        ),
        receipts=receipts,
        schedule=schedule,
        clock=lambda: now,
    )
    due = schedule.due(now)

    result = worker.run_tick(
        due=due,
        broker=object(),
    )

    assert result.recovery_blocked is False
    assert calls == ["management"]
    assert "entry" not in calls
    assert result.phases["ENTRY_SERVICE"]["blocked"] == "FAIL-RECOVERY-BLOCKED"


def test_unresolved_tick_stays_blocked_until_explicit_reconciliation(
    tmp_path: Path,
):
    now = dt.datetime(2026, 8, 14, 14, 0, tzinfo=dt.timezone.utc)
    hashes = {"policy_hash": "a" * 64, "catalog_hash": "b" * 64}
    context = CycleContext(
        session_id="session",
        lease_nonce="nonce",
        tick_id="ambiguous-tick",
        attempt_id="ambiguous-attempt",
        started_at=now,
        session_date=now.date(),
        **hashes,
    )
    receipts = ReceiptStore(tmp_path / "autocycle")
    receipts.emit(ReceiptKind.TICK_STARTED, context=context)
    receipts.emit(
        ReceiptKind.TICK_UNRESOLVED,
        context=context,
        outcome="UNRESOLVED",
        recovery_required=True,
    )

    assert len(receipts.unmatched_ticks(session_id="session")) == 1

    # A contradictory late finish cannot erase the ambiguity.  This models a
    # supervisor that observed a child exit after the broker outcome was lost.
    receipts.emit(ReceiptKind.TICK_FINISHED, context=context, outcome="FINISHED")
    assert len(receipts.unmatched_ticks(session_id="session")) == 1

    receipts.emit(ReceiptKind.TICK_RECONCILED, context=context, outcome="RECONCILED")
    assert receipts.unmatched_ticks(session_id="session") == ()


def test_reprice_transmissions_count_as_one_logical_opening(
    tmp_path: Path,
):
    now = dt.datetime(2026, 8, 14, 14, 0, tzinfo=dt.timezone.utc)
    hashes = {"policy_hash": "a" * 64, "catalog_hash": "b" * 64}
    config = AutoCycleConfig(
        mandate="FULL",
        mode=CycleMode.ARMED,
        management_seconds=300,
        discovery_seconds=1800,
        probe_seconds=600,
        entry_seconds=300,
        missed_tick_policy="SKIP_MISSED_TICKS",
        entry_start=dt.time(10, 0),
        entry_end=dt.time(15, 0),
        coverage_sla_seconds=86400,
        max_pending_entries=3,
        max_new_entries_per_pass=1,
        phase2_limit=5,
        state_dir=tmp_path,
        **hashes,
    )
    calls: list[str] = []
    schedule = FixedRateSchedule(
        tmp_path / "autocycle",
        anchor=now - dt.timedelta(minutes=5),
        cadences={job: 300 for job in JobKind},
    )
    worker = OptionsCycleWorker(
        config=config,
        session_id="session",
        lease_nonce="nonce",
        broker_factory=lambda: object(),
        phases=CyclePhases(
            management=lambda _context: {},
            discovery=lambda _context: {},
            probe=lambda _context: {},
            entry=lambda _context: calls.append("entry") or {
                "transmissions": [{"order_id": 1}, {"order_id": 2}, {"order_id": 3}],
                "new_openings": 1,
            },
        ),
        receipts=ReceiptStore(tmp_path / "autocycle"),
        schedule=schedule,
        clock=lambda: now,
    )
    due = schedule.due(now)

    result = worker.run_tick(
        due=due,
        arm=True,
        broker=object(),
    )

    assert result.outcome == "FINISHED"
    assert calls == ["entry"]
