"""BLOCKER-1 requirement 2: corrupt-vs-missing session.lock identity.

``_read_json`` collapses "path does not exist" and "path exists but failed to
parse" to the same ``None``, so every caller built on it -- start's lock
acquire, stop's ownership checks, status's lock line -- could not tell a
torn/corrupted ``session.lock`` apart from a genuinely absent one
(docs/paper-day-recovery/open-questions.md, BLOCKER-1). This file pins the
three call sites that must now diverge for a corrupt lock, plus the
2026-08-20 dirty-stop incident sequence BLOCKER-1 exists to prevent
recurring: a controller that never reached a clean stop must not silently
reopen entry authority on its next start, regardless of what state its
watcher's PID is in.
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.paperday import (
    BLOCKED,
    GATE_CLOSED,
    GATE_RECOVERY_REQUIRED,
    STOPPED,
    StartReport,
    StopReport,
    read_gate,
    write_gate,
)
from paperday_support import NOW, harness


def _check(report: StartReport, name: str):
    matches = [c for c in report.checks if c.name == name]
    assert matches, f"no check named {name!r} in:\n{report.render()}"
    return matches[-1]


def _step(report: StopReport, name: str):
    matches = [s for s in report.steps if s.name == name]
    assert matches, f"no step named {name!r} in:\n{report.render()}"
    return matches[-1]


def _write_corrupt_lock(paths, *, garbage: bytes = b"{\"session_id\": \"trunc") -> None:
    """Simulate a torn/corrupted session.lock: present, but not valid JSON."""
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.lock.write_bytes(garbage)


class TestTornLockRecoveryAtStart:
    """start() must refuse a corrupt lock with its own diagnostic, not the
    "another start acquired the lock concurrently" message meant for a real
    collision -- that message gives no path forward because a corrupt file
    reproduces identically on every re-run.
    """

    def test_corrupt_lock_refuses_with_a_distinct_diagnostic(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        _write_corrupt_lock(h.paths)
        original_bytes = h.paths.lock.read_bytes()

        report = h.controller.start()

        assert report.state == BLOCKED, report.render()
        lock_check = _check(report, "session lock")
        assert not lock_check.ok, report.render()
        assert "not valid JSON" in lock_check.detail, report.render()
        assert "another start acquired the lock concurrently" not in lock_check.detail, (
            report.render()
        )
        # Refused fail-closed: the corrupt file is neither deleted nor
        # silently overwritten with a fresh lock.
        assert h.paths.lock.read_bytes() == original_bytes
        assert h.processes.spawned == [], report.render()


class TestTornLockRecoveryAtStop:
    """stop() must actually reach its corrupt-lock branch and refuse there
    with the right diagnostic -- the pre-fix bug is that
    ``_stop_owns``/``_current_stop_lock_identity`` computed ("invalid-lock",)
    for the corrupt file while stop's own ``expected_owner`` (derived
    straight from ``_read_json`` collapsing to ``None``) computed
    ("no-session",); the mismatch made stop think a replacement session had
    taken over and it returned early with a misleading message, before ever
    reaching a branch that names the lock as corrupt.

    Reaching that branch does not mean unlinking the file: per
    ``TestStopFailsClosedOnAnUnreadableLock``
    (``tests/test_paperday_scheduler.py``), "we cannot prove who owns this"
    is not licence to act on it, the same way it is not licence to treat it
    as absent. The gate closes (risk-reducing, so it proceeds under unknown
    ownership); the lock file itself is left exactly as found.
    """

    def test_stop_reaches_the_corrupt_branch_and_refuses_without_unlinking(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        write_gate(
            h.paths,
            entry_gate="OPEN",
            state="PAPER_DAY_READY",
            session_id="corrupt-session",
            now=NOW,
        )
        _write_corrupt_lock(h.paths)
        lock_bytes_before = h.paths.lock.read_bytes()

        report = h.controller.stop()

        lock_step = _step(report, "session lock")
        assert not lock_step.ok, report.render()
        assert "not valid JSON" in lock_step.detail, report.render()
        assert not report.clean, report.render()

        # Reached the corrupt-lock branch, not stuck behind the false
        # "replacement session acquired the lock" takeover message.
        assert not any(
            "replacement session acquired the lock" in step.detail for step in report.steps
        ), report.render()

        # An unprovable lock is left exactly as found -- not unlinked.
        assert h.paths.lock.exists(), report.render()
        assert h.paths.lock.read_bytes() == lock_bytes_before

        # Ownership of the broker-affecting work was never proven, so nothing
        # that transmits ran.
        assert not any(call[0] == "options-cancel" for call in h.engine.calls)
        assert not any(call[0] == "options-positions" for call in h.engine.calls)

        gate = read_gate(h.paths)
        assert gate is not None
        assert gate["entry_gate"] == GATE_CLOSED
        assert gate[GATE_RECOVERY_REQUIRED] is True, gate


class TestTornLockRecoveryAtStatus:
    """status() must not report a corrupt lock as merely 'held'."""

    def test_status_reports_corrupt_lock_distinctly_from_held(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        _write_corrupt_lock(h.paths)

        rows = dict(h.controller.status().rows)

        assert rows["session lock"] != "held", rows
        assert rows["session lock"] != "none", rows
        assert "CORRUPT" in rows["session lock"], rows


class TestDirtyStopIncidentRegression:
    """The 2026-08-20 incident sequence: a controller acquired session.lock
    and never reached a clean stop -- crashing leaves a torn (unparseable)
    lock on disk under the pre-atomic-writer failure mode BLOCKER-1
    documents, and its watcher's PID record is left over from before the
    crash too. Whether that watcher process is simply dead, or its number
    has since been recycled by an unrelated process, the next start must
    land BLOCKED with recovery_required still latched true, and must not
    delete or mutate any state file while refusing.
    """

    def _post_crash_state(self, h) -> dict:
        """gate.json as a genuinely dirty stop would leave it: CLOSED,
        recovery_required latched true, from the last thing this controller
        proved before it crashed."""
        write_gate(
            h.paths,
            entry_gate=GATE_CLOSED,
            state=STOPPED,
            session_id="crashed-session",
            now=NOW,
            fencing_token="fence-crashed",
            recovery_required=True,
        )
        gate_before = read_gate(h.paths)
        assert gate_before is not None and gate_before[GATE_RECOVERY_REQUIRED] is True
        _write_corrupt_lock(
            h.paths,
            garbage=b'{"session_id": "crashed-session", "fencing_to',
        )
        h.paths.watcher_pid.write_text(
            json.dumps(
                {
                    "pid": 4242,
                    "session_id": "crashed-session",
                    "fencing_token": "fence-crashed",
                }
            ),
            encoding="utf-8",
        )
        return gate_before

    def _assert_blocked_and_untouched(self, h, gate_before: dict, lock_bytes_before: bytes) -> None:
        state_files = {"lock": h.paths.lock, "gate": h.paths.gate, "watcher_pid": h.paths.watcher_pid}
        existed_before = {name: p.exists() for name, p in state_files.items()}

        report = h.controller.start()

        assert report.state == BLOCKED, report.render()
        lock_check = _check(report, "session lock")
        assert not lock_check.ok, report.render()
        assert "not valid JSON" in lock_check.detail, report.render()

        # No state file was deleted, and the corrupt lock and prior gate were
        # not mutated by the refusal -- recovery_required stays latched
        # exactly as the dirty stop left it, not cleared and not rewritten.
        for name, p in state_files.items():
            assert p.exists() == existed_before[name], f"{name} existence changed"
        assert h.paths.lock.read_bytes() == lock_bytes_before
        gate_after = read_gate(h.paths)
        assert gate_after == gate_before, (gate_before, gate_after)
        assert gate_after[GATE_RECOVERY_REQUIRED] is True

        assert h.processes.spawned == [], report.render()
        assert not any(call[0] == "options-cancel" for call in h.engine.calls)

    def test_dead_watcher_lands_blocked_recovery_latched(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        gate_before = self._post_crash_state(h)
        assert not h.processes.alive(4242)  # dead: never registered
        lock_bytes_before = h.paths.lock.read_bytes()

        self._assert_blocked_and_untouched(h, gate_before, lock_bytes_before)

    def test_reused_watcher_pid_lands_blocked_recovery_latched(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        gate_before = self._post_crash_state(h)
        h.processes.add("chrome.exe", pid=4242)  # alive, but a stranger
        lock_bytes_before = h.paths.lock.read_bytes()

        self._assert_blocked_and_untouched(h, gate_before, lock_bytes_before)

        # The foreign process must never be touched by a refused start.
        assert 4242 not in h.processes.terminated
        assert h.processes.alive(4242)
