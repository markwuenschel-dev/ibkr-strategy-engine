"""The operational start-scenario matrix for the paper-day controller.

Each class pins one of the ways a real morning goes sideways -- double starts,
stale PIDs, crashed sessions, an absent verifier, a broken watcher spawn, a
live-port config -- and asserts the controller lands in the documented state
with the documented gate, instead of becoming an incident.

The happy path, hard broker blocks, and gate enforcement live in
``test_paperday.py``; stop-side scenarios live in
``test_paperday_stop_scenarios.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.errors import ConfigError, EngineError, UnsafeConfigError
from engine.paperday import (
    BLOCKED,
    DEGRADED,
    GATE_CLOSED,
    GATE_OPEN,
    GATE_PROOF_ONLY,
    READY,
    StartReport,
    entry_gate_preflight,
    read_gate,
)
from paperday_support import NOW, WATCHER_CMD, harness


def _check(report: StartReport, name: str):
    matches = [c for c in report.checks if c.name == name]
    assert matches, f"no check named {name!r} in:\n{report.render()}"
    return matches[-1]


def _failed_names(report: StartReport) -> set[str]:
    return {c.name for c in report.checks if not c.ok}


def _write_json_by_hand(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class TestRepeatedStart:
    """A second start must re-verify, not double the session.

    Prevents: two watchers racing over one handoff inbox, and a re-run
    operator command silently rotating the session identity out from under a
    running day.
    """

    def test_second_start_is_idempotent(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        first = h.controller.start()
        assert first.state == READY, first.render()
        assert not first.already_running, first.render()

        second = h.controller.start()
        assert second.state == READY, second.render()
        assert second.already_running, second.render()

        # Exactly ONE watcher spawn across both starts.
        assert len(h.processes.spawned) == 1, second.render()

        # The lock survives and still names the FIRST session.
        lock = json.loads(h.paths.lock.read_text(encoding="utf-8"))
        assert lock["session_id"] == first.session_id, second.render()
        assert second.session_id == first.session_id, second.render()


class TestStalePid:
    """A recorded watcher PID is a hypothesis, not a fact.

    Prevents: trusting a dead PID (no watcher all day), and -- worse --
    killing or adopting a stranger's process that inherited the number after
    an OS PID reuse.
    """

    def test_dead_pid_is_discarded_and_a_fresh_watcher_spawned(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        _write_json_by_hand(
            h.paths.watcher_pid,
            {"pid": 4242, "started_at": NOW.isoformat(),
             "needle": "watch-for-claude-handoffs.py"},
        )
        assert not h.processes.alive(4242)  # 4242 is nowhere in the table

        report = h.controller.start()
        assert report.state == READY, report.render()
        assert len(h.processes.spawned) == 1, report.render()
        assert report.watcher_pid is not None and report.watcher_pid != 4242

        rewritten = json.loads(h.paths.watcher_pid.read_text(encoding="utf-8"))
        assert rewritten["pid"] == report.watcher_pid, report.render()

    def test_reused_pid_is_not_terminated_and_a_fresh_watcher_spawned(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        h.processes.add("chrome.exe", pid=4242)  # alive, but not our watcher
        _write_json_by_hand(
            h.paths.watcher_pid,
            {"pid": 4242, "started_at": NOW.isoformat(),
             "needle": "watch-for-claude-handoffs.py"},
        )

        report = h.controller.start()
        assert report.state == READY, report.render()

        # CRITICAL: the foreign process was never touched.
        assert 4242 not in h.processes.terminated, report.render()
        assert h.processes.alive(4242)
        assert h.processes.cmdline(4242) == "chrome.exe"

        # And a real watcher of ours now exists, recorded in the pid file.
        assert len(h.processes.spawned) == 1, report.render()
        assert report.watcher_pid is not None and report.watcher_pid != 4242
        rewritten = json.loads(h.paths.watcher_pid.read_text(encoding="utf-8"))
        assert rewritten["pid"] == report.watcher_pid
        assert h.processes.cmdline(report.watcher_pid) == WATCHER_CMD


class TestStaleLockRecovery:
    """A lock left by a crashed session must not brick the next morning.

    Prevents: a dead session's lock (watcher.pid absent, so nothing is
    actually running) forcing a human to hand-delete state files before the
    day can start.
    """

    def test_start_recovers_a_dead_sessions_lock(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        _write_json_by_hand(
            h.paths.lock,
            {"session_id": "paperday-19990101-deadbeef",
             "started_at": "1999-01-01T09:30:00+00:00",
             "controller_pid": 1},
        )
        assert not h.paths.watcher_pid.exists()  # crashed: no watcher record

        report = h.controller.start()
        assert report.state == READY, report.render()
        assert not report.already_running, report.render()

        # A fresh lock with a fresh session id -- not the corpse's.
        assert report.session_id != "paperday-19990101-deadbeef"
        lock = json.loads(h.paths.lock.read_text(encoding="utf-8"))
        assert lock["session_id"] == report.session_id, report.render()
        gate = read_gate(h.paths)
        assert gate is not None and gate["entry_gate"] == GATE_OPEN

    def test_live_watcher_does_not_make_yesterday_lock_current(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        old_session = "paperday-yesterday"
        old_fence = "fence-yesterday"
        watcher = h.processes.add(WATCHER_CMD)
        _write_json_by_hand(
            h.paths.lock,
            {
                "session_id": old_session,
                "fencing_token": old_fence,
                "session_date": "2026-07-31",
                "started_at": "2026-07-31T09:30:00+00:00",
            },
        )
        _write_json_by_hand(
            h.paths.watcher_pid,
            {
                "pid": watcher,
                "session_id": old_session,
                "fencing_token": old_fence,
            },
        )
        from engine.paperday import write_gate

        write_gate(
            h.paths,
            entry_gate=GATE_OPEN,
            state=READY,
            session_id=old_session,
            now=NOW.replace(day=31),
            fencing_token=old_fence,
        )

        report = h.controller.start()

        assert report.session_id != old_session, report.render()
        assert report.already_running is False, report.render()
        assert h.processes.alive(watcher), "the stale watcher must not be killed"
        assert len(h.processes.spawned) == 1, report.render()


class TestGrokUnavailable:
    """No verifier means no new opening risk -- and nothing else lost.

    Prevents: an absent reviewer either being ignored (armed entries with no
    independent verifier) or over-reacted to (a BLOCKED book that also stops
    exits and management).
    """

    def test_no_reviewer_watcher_degrades_to_proof_only(self, tmp_path: Path) -> None:
        h = harness(
            tmp_path,
            reviewer_running=False,
            reviewer_answers=False,
            liveness_timeout=0.05,
        )
        report = h.controller.start()
        assert report.state == DEGRADED, report.render()

        gate = read_gate(h.paths)
        assert gate is not None and gate["entry_gate"] == GATE_PROOF_ONLY

        # Exactly the reviewer-watcher and liveness checks failed.
        assert _failed_names(report) == {"reviewer watcher", "verifier liveness"}, (
            report.render()
        )

        preflight = entry_gate_preflight(h.paths)
        assert preflight(armed=True) is not None, report.render()
        assert preflight(armed=False) is None, report.render()

    def test_present_but_silent_reviewer_fails_liveness(self, tmp_path: Path) -> None:
        """The watcher process existing proves nothing; only a reply does."""
        h = harness(
            tmp_path,
            reviewer_running=True,
            reviewer_answers=False,
            liveness_timeout=0.05,
        )
        report = h.controller.start()
        assert report.state == DEGRADED, report.render()

        assert _check(report, "reviewer watcher").ok, report.render()
        liveness = _check(report, "verifier liveness")
        assert not liveness.ok, report.render()
        assert "no reviewer reply" in liveness.detail, report.render()

        gate = read_gate(h.paths)
        assert gate is not None and gate["entry_gate"] == GATE_PROOF_ONLY


class TestClaudeWatcherUnavailable:
    """A watcher that cannot start degrades the day; it does not block it.

    Prevents: a broken spawn (missing python, bad path) being treated as an
    untrustworthy book -- the book is fine, only the handoff plumbing is out,
    so management continues and opens refuse.
    """

    def test_spawn_failure_is_degraded_not_blocked(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.processes.spawn_error = OSError("no python")

        report = h.controller.start()
        assert report.state == DEGRADED, report.render()
        assert report.state != BLOCKED, report.render()

        watcher = _check(report, "builder watcher")
        assert not watcher.ok, report.render()
        assert "no python" in watcher.detail, report.render()

        gate = read_gate(h.paths)
        assert gate is not None and gate["entry_gate"] == GATE_PROOF_ONLY


class TestExistingPositionVerifierDown:
    """DEGRADED means exactly: management available, opens refused.

    Prevents: the degraded state quietly widening -- an open position must
    still reconcile and mark (the management path the position depends on)
    even while the missing verifier keeps the entry gate at PROOF_ONLY.
    """

    def test_management_path_stays_healthy_while_degraded(self, tmp_path: Path) -> None:
        h = harness(tmp_path, reviewer_running=False, liveness_timeout=0.05)
        # Harness defaults: options-positions -> "broker agrees",
        # options-mark -> MARKED -- i.e. an existing position, managed fine.

        report = h.controller.start()
        assert report.state == DEGRADED, report.render()

        assert _check(report, "reconciliation").ok, report.render()
        assert _check(report, "marking").ok, report.render()

        gate = read_gate(h.paths)
        assert gate is not None and gate["entry_gate"] == GATE_PROOF_ONLY
        preflight = entry_gate_preflight(h.paths)
        assert preflight(armed=True) is not None  # opens refused
        assert preflight(armed=False) is None  # proofs / management untouched


class TestLivePortConfiguration:
    """A live-port config is a hard stop before anything is touched.

    Prevents: the controller acquiring a lock, spawning a watcher, or leaving
    a permissive gate behind after config already told it the endpoints could
    reach a live account.
    """

    def test_unsafe_config_blocks_before_any_side_effect(self, tmp_path: Path) -> None:
        # UnsafeConfigError (errors.py:41) subclasses ConfigError -> EngineError,
        # so start()'s `except EngineError` on the config check catches it.
        assert issubclass(UnsafeConfigError, ConfigError)
        assert issubclass(UnsafeConfigError, EngineError)

        h = harness(
            tmp_path,
            config=UnsafeConfigError("port 7496 is a live trading port"),
        )
        report = h.controller.start()
        assert report.state == BLOCKED, report.render()

        config_check = _check(report, "configuration")
        assert not config_check.ok, report.render()
        assert "live trading port" in config_check.detail, report.render()

        # Blocked at configuration: nothing downstream may have happened.
        assert not h.paths.lock.exists(), report.render()
        assert h.processes.spawned == [], report.render()

        gate = read_gate(h.paths)
        assert gate is not None and gate["entry_gate"] == GATE_CLOSED
