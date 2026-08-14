"""Focused R7 authority and operator-lifecycle proofs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.paperday import (
    BLOCKED,
    GATE_CONFIGURATION_FINGERPRINT,
    GATE_CLOSED,
    GATE_OPEN,
    GATE_RECOVERY_REQUIRED,
    MANDATE_FULL,
    PaperDayPaths,
    entry_gate_preflight,
    read_gate,
    write_gate,
)
from engine.scheduler import SchedulerPaths, announce_ready
from paperday_support import NOW, harness

HASH = "a" * 64


def _strict_gate(paths: PaperDayPaths, *, session_date=None) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    session_id = "paperday-authority"
    nonce = "nonce-authority"
    pid = 9123
    paths.lock.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "fencing_token": "fence-authority",
                "started_at": NOW.isoformat(),
                "controller_pid": 123,
            }
        ),
        encoding="utf-8",
    )
    scheduler_paths = SchedulerPaths(root=paths.root)
    scheduler_paths.pid.write_text(
        json.dumps(
            {
                "v": 1,
                "pid": pid,
                "session_id": session_id,
                "nonce": nonce,
                "needle": f"--scheduler-session={session_id}:{nonce}",
            }
        ),
        encoding="utf-8",
    )
    announce_ready(
        scheduler_paths,
        type("Identity", (), {"session_id": session_id, "nonce": nonce})(),
        now=NOW,
    )
    (paths.last_verification).write_text(
        json.dumps(
            {
                "session_id": session_id,
                "reviewer_liveness_epoch": "reviewer-epoch",
                "liveness_at": NOW.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    write_gate(
        paths,
        entry_gate=GATE_OPEN,
        state="PAPER_DAY_READY",
        session_id=session_id,
        now=session_date or NOW,
        fencing_token="fence-authority",
        mandate=MANDATE_FULL,
        policy_sha256=HASH,
        catalog_sha256=HASH,
        config_sha256=HASH,
        configuration_fingerprint=HASH,
        authority_required=True,
        controller_pid=123,
        scheduler_identity={"session_id": session_id, "nonce": nonce, "pid": pid},
        reviewer_liveness_epoch="reviewer-epoch",
        reviewer_liveness_at=NOW.isoformat(),
    )


class TestPaperDayAuthority:
    def test_relative_state_dir_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            PaperDayPaths(state_dir=Path("relative-state"))

    def test_full_controller_requires_configuration_fingerprint(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.controller.mandate = MANDATE_FULL
        h.controller.policy_sha256 = HASH
        h.controller.catalog_sha256 = HASH
        h.controller.config_sha256 = HASH

        report = h.controller.start()

        assert report.state == BLOCKED, report.render()
        authority = next(step for step in report.checks if step.name == "authority inputs")
        assert not authority.ok
        assert "configuration_fingerprint" in authority.detail

    def test_full_authority_rejects_a_stale_session_date(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        _strict_gate(h.paths, session_date=NOW.replace(day=31))
        pid = h.processes.add(
            "python scheduler_main.py "
            "--scheduler-session=paperday-authority:nonce-authority",
            pid=9123,
        )

        refusal = entry_gate_preflight(
            h.paths, processes=h.processes
        )(armed=True, now=NOW)

        assert pid == 9123
        assert refusal is not None
        assert "FAIL-STALE-PAPERDAY-AUTHORITY" in refusal
        assert "session date" in refusal

    def test_full_authority_rejects_a_missing_configuration_fingerprint(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        _strict_gate(h.paths)
        gate = read_gate(h.paths)
        assert gate is not None
        gate.pop(GATE_CONFIGURATION_FINGERPRINT, None)
        h.paths.gate.write_text(json.dumps(gate), encoding="utf-8")
        h.processes.add(
            "python scheduler_main.py "
            "--scheduler-session=paperday-authority:nonce-authority",
            pid=9123,
        )

        refusal = entry_gate_preflight(h.paths, processes=h.processes)(
            armed=True, now=NOW
        )

        assert refusal is not None
        assert "FAIL-CONFIGURATION-FINGERPRINT" in refusal

    def test_full_authority_rejects_dead_scheduler_without_a_current_heartbeat(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        _strict_gate(h.paths)
        h.processes.add(
            "python scheduler_main.py "
            "--scheduler-session=paperday-authority:nonce-authority",
            pid=9123,
        )
        h.processes.kill_silently(9123)

        refusal = entry_gate_preflight(
            h.paths, processes=h.processes
        )(armed=True, now=NOW)

        assert refusal is not None
        assert "scheduler PID is not alive" in refusal

    def test_missing_scheduler_proof_makes_stop_dirty_and_records_recovery(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        _strict_gate(h.paths)

        report = h.controller.stop()

        assert not report.clean, report.render()
        assert any(
            step.name == "scheduler" and "STOP_DIRTY" in step.detail
            for step in report.steps
        ), report.render()
        gate = read_gate(h.paths)
        assert gate is not None and gate[GATE_RECOVERY_REQUIRED] is True
        assert gate["entry_gate"] == GATE_CLOSED

    def test_status_exposes_authority_recovery_scheduler_and_tick_rows(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        report = h.controller.status()
        rows = dict(report.rows)

        assert rows["StateDir"] == str(h.paths.state_dir)
        assert "entry authority" in rows
        assert "recovery" in rows
        assert "scheduler authority" in rows
        assert "latest tick" in rows
        assert "policy SHA-256" in rows
        assert "catalog SHA-256" in rows
        assert "config SHA-256" in rows

    def test_status_surfaces_a_worker_failure_code_from_the_latest_tick(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        tick_path = SchedulerPaths(root=h.paths.root).receipts_for(NOW.date())
        tick_path.parent.mkdir(parents=True, exist_ok=True)
        tick_path.write_text(
            json.dumps(
                {
                    "outcome": "STOPPED_TICK_ABORTED",
                    "tick_id": "failed-tick",
                    "at": NOW.isoformat(),
                    "exit_code": 17,
                    "failure_code": "FAIL-WORKER-NONZERO-EXIT",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        rows = dict(h.controller.status().rows)

        assert "STOPPED_TICK_ABORTED" in rows["latest tick"]
        assert "exit=17" in rows["latest tick"]
        assert "FAIL-WORKER-NONZERO-EXIT" in rows["latest tick"]
