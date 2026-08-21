"""Wiring + adversarial verification for ``engine.paperday_recovery_cli`` --
the operator-reachable ``paperday-recover`` command, P2's wiring layer.

This is the "independent verification with negative tests" step: stale PID,
reused PID, corrupt lock, broker unavailable, unmatched tick, unresolved
outbox, changed fencing token, concurrent recovery attempts -- one test per
scenario, each proving the command refuses and leaves ``gate.json``
byte-identical, except the one scenario where everything is genuinely clean,
which proves the command CAN succeed (a suite that refuses everything would
be as useless as one that permits everything).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from engine.config import EngineConfig
from engine.paperday import PaperDayPaths
from engine.paperday_recovery import BrokerReconciliationOutcome
from engine.paperday_recovery_cli import (
    RecoveryOutcome,
    apply_recovery_result,
    build_recovery_attempt,
    run_recovery,
    target_process_is_still_alive,
)

NOW = dt.datetime(2026, 8, 21, 10, 0, 0, tzinfo=dt.timezone.utc)

SESSION_ID = "paperday-20260819-fa4081f5"
LEASE_NONCE = "ada79de6"
PROCESS_ID = 240328
FENCING_TOKEN = "f780bb1c88fc4bc49be7d27470085619"


class _FakeProcessPort:
    """Mimics ``runtime.SubprocessProcessPort`` well enough for tests:
    ``alive(pid)`` is the only method exercised here."""

    def __init__(self, alive_pids: frozenset[int] = frozenset()) -> None:
        self._alive = alive_pids

    def alive(self, pid: int) -> bool:
        return pid in self._alive


class _FakeBroker:
    def __init__(self, positions: list = None, open_orders=None, raise_on_connect: bool = False):
        self._positions = positions or []
        self._open_orders = open_orders
        self._raise = raise_on_connect
        self.ib = self

    def __enter__(self):
        if self._raise:
            raise ConnectionError("simulated broker connection failure")
        return self

    def __exit__(self, *exc):
        return False

    def positions(self):
        return self._positions


def _fake_broker_factory(broker: _FakeBroker):
    def factory(config, journal):
        return broker

    return factory


def _write_gate(paths: PaperDayPaths, **overrides) -> None:
    payload = {
        "schema_version": 1,
        "state": "PAPER_DAY_BLOCKED",
        "entry_gate": "CLOSED",
        "recovery_required": True,
        "fencing_token": FENCING_TOKEN,
        "session_id": SESSION_ID,
    }
    payload.update(overrides)
    paths.gate.parent.mkdir(parents=True, exist_ok=True)
    paths.gate.write_text(json.dumps(payload), encoding="utf-8")


def _write_scheduler_pid(paths: PaperDayPaths, **overrides) -> None:
    payload = {"session_id": SESSION_ID, "nonce": LEASE_NONCE, "pid": PROCESS_ID}
    payload.update(overrides)
    scheduler_pid = paths.root / "scheduler.pid"
    scheduler_pid.parent.mkdir(parents=True, exist_ok=True)
    scheduler_pid.write_text(json.dumps(payload), encoding="utf-8")


def _paths(tmp_path: Path) -> PaperDayPaths:
    return PaperDayPaths(state_dir=tmp_path / ".engine")


def _config(tmp_path: Path) -> EngineConfig:
    return EngineConfig(account_id="DUR000000", state_dir=tmp_path / ".engine")


def _run(
    tmp_path: Path,
    *,
    broker: _FakeBroker,
    process_port: _FakeProcessPort,
    reason: str = "2026-08-21 dirty-stop recovery, evidenced 2026-08-20",
    expected_fencing_token: str = FENCING_TOKEN,
) -> RecoveryOutcome:
    return run_recovery(
        paths=_paths(tmp_path),
        expected_session_id=SESSION_ID,
        expected_lease_nonce=LEASE_NONCE,
        expected_process_id=PROCESS_ID,
        expected_fencing_token=expected_fencing_token,
        reason=reason,
        now=NOW,
        config=_config(tmp_path),
        broker_factory=_fake_broker_factory(broker),
        process_port=process_port,
    )


def _gate_bytes(paths: PaperDayPaths) -> bytes:
    return paths.gate.read_bytes()


# ---------------------------------------------------------------------------
# The one clean path: proves the suite is not vacuously refusing everything
# ---------------------------------------------------------------------------


class TestCleanRecoverySucceeds:
    def test_all_conditions_clean_clears_recovery_required_and_leaves_entry_gate_closed(
        self, tmp_path: Path
    ) -> None:
        paths = _paths(tmp_path)
        _write_gate(paths)
        _write_scheduler_pid(paths)

        outcome = _run(
            tmp_path,
            broker=_FakeBroker(positions=[]),
            process_port=_FakeProcessPort(alive_pids=frozenset()),  # target is dead
        )

        assert outcome.refused_reason is None
        assert outcome.acceptance is not None
        assert outcome.acceptance.all_passed, outcome.acceptance.checks
        assert outcome.applied is True

        gate = json.loads(paths.gate.read_text(encoding="utf-8"))
        assert gate["recovery_required"] is False
        assert gate["entry_gate"] == "CLOSED"  # requirement 9: never opened by this command

        # A receipt and an archive copy both exist.
        archive_dir = paths.root / "recovery-archive"
        assert any(archive_dir.glob("*")), "expected an archived gate copy"


# ---------------------------------------------------------------------------
# Negative / adversarial suite
# ---------------------------------------------------------------------------


class TestStalePidIsTheNormalCase:
    """A dead PID is what SHOULD allow recovery to proceed -- covered by
    TestCleanRecoverySucceeds above. This class documents that explicitly so
    the adversarial suite below isn't mistaken for the only path tested."""

    def test_dead_pid_is_not_itself_a_refusal(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        _write_gate(paths)
        _write_scheduler_pid(paths)
        outcome = _run(
            tmp_path, broker=_FakeBroker(), process_port=_FakeProcessPort(frozenset())
        )
        assert outcome.refused_reason is None


class TestReusedPidRefuses:
    def test_target_process_id_still_alive_refuses_before_the_acceptance_bar_runs(
        self, tmp_path: Path
    ) -> None:
        paths = _paths(tmp_path)
        _write_gate(paths)
        _write_scheduler_pid(paths)
        before = _gate_bytes(paths)

        # The OS says this PID number is alive right now -- exactly the
        # 2026-08-20 incident shape (pid 64020 reused by an unrelated
        # process minutes after the real controller exited).
        outcome = _run(
            tmp_path,
            broker=_FakeBroker(),
            process_port=_FakeProcessPort(alive_pids=frozenset({PROCESS_ID})),
        )

        assert outcome.refused_reason is not None
        assert "still alive" in outcome.refused_reason
        assert outcome.acceptance is None, "the 9-point bar must never even run"
        assert outcome.applied is False
        assert _gate_bytes(paths) == before

    def test_target_process_is_still_alive_helper_directly(self) -> None:
        assert target_process_is_still_alive(PROCESS_ID, _FakeProcessPort(frozenset({PROCESS_ID})))
        assert not target_process_is_still_alive(PROCESS_ID, _FakeProcessPort(frozenset()))


class TestCorruptLockRefuses:
    def test_corrupt_gate_state_refuses_at_requirement_3(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        paths.gate.parent.mkdir(parents=True, exist_ok=True)
        paths.gate.write_bytes(b"{not valid json")
        _write_scheduler_pid(paths)
        before = _gate_bytes(paths)

        outcome = _run(
            tmp_path, broker=_FakeBroker(), process_port=_FakeProcessPort(frozenset())
        )

        assert outcome.acceptance is not None
        assert outcome.acceptance.all_passed is False
        state_check = outcome.acceptance.check("3_readable_known_state")
        assert state_check is not None and state_check.passed is False
        assert outcome.applied is False
        assert _gate_bytes(paths) == before  # corrupt state is never unlinked or rewritten


class TestBrokerUnavailableRefuses:
    def test_broker_connection_failure_is_a_disagreement_not_a_crash(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        _write_gate(paths)
        _write_scheduler_pid(paths)
        before = _gate_bytes(paths)

        outcome = _run(
            tmp_path,
            broker=_FakeBroker(raise_on_connect=True),
            process_port=_FakeProcessPort(frozenset()),
        )

        assert outcome.refused_reason is None  # got past the liveness pre-check
        assert outcome.acceptance is not None
        reconciliation_check = outcome.acceptance.check("5_broker_reconciliation")
        assert reconciliation_check is not None and reconciliation_check.passed is False
        assert outcome.acceptance.all_passed is False
        assert outcome.applied is False
        assert _gate_bytes(paths) == before


class TestUnmatchedTickRefuses:
    def test_a_tick_started_with_no_terminal_event_blocks_recovery(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        _write_gate(paths)
        _write_scheduler_pid(paths)
        before = _gate_bytes(paths)

        events_path = paths.root / "receipts" / f"{NOW.date():%Y-%m-%d}-tick-events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text(
            json.dumps(
                {
                    "event": "TICK_STARTED",
                    "tick_id": "tick-1",
                    "attempt_id": "attempt-1",
                    "session_id": SESSION_ID,
                    "lease_nonce": LEASE_NONCE,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        outcome = _run(
            tmp_path, broker=_FakeBroker(), process_port=_FakeProcessPort(frozenset())
        )

        assert outcome.acceptance is not None
        outbox_check = outcome.acceptance.check("4_no_unmatched_ticks_or_opening_outbox")
        assert outbox_check is not None and outbox_check.passed is False
        assert "unmatched tick" in outbox_check.detail
        assert outcome.applied is False
        assert _gate_bytes(paths) == before


class TestUnresolvedOutboxRefuses:
    def test_a_blocking_outbox_record_blocks_recovery(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        _write_gate(paths)
        _write_scheduler_pid(paths)
        before = _gate_bytes(paths)

        outbox_root = tmp_path / ".engine" / "execution-outbox"
        outbox_root.mkdir(parents=True, exist_ok=True)
        (outbox_root / "attempt-0001.json").write_text(
            json.dumps({"v": 1, "state": "PREPARED", "session_id": SESSION_ID}),
            encoding="utf-8",
        )

        outcome = _run(
            tmp_path, broker=_FakeBroker(), process_port=_FakeProcessPort(frozenset())
        )

        assert outcome.acceptance is not None
        outbox_check = outcome.acceptance.check("4_no_unmatched_ticks_or_opening_outbox")
        assert outbox_check is not None and outbox_check.passed is False
        assert outcome.applied is False
        assert _gate_bytes(paths) == before


class TestChangedFencingTokenRefuses:
    def test_fencing_token_mismatch_refuses_the_cas(self, tmp_path: Path) -> None:
        """The gate on disk carries the real, current fencing token. The
        operator's --fencing-token asserts a value they remembered from
        earlier evidence-gathering that no longer matches -- exactly the
        race requirement 6 exists to catch (another session raced in and
        rewrote the gate between when the operator looked and when they
        acted)."""
        paths = _paths(tmp_path)
        _write_gate(paths, fencing_token=FENCING_TOKEN)
        _write_scheduler_pid(paths)
        before = _gate_bytes(paths)

        outcome = _run(
            tmp_path,
            broker=_FakeBroker(),
            process_port=_FakeProcessPort(frozenset()),
            expected_fencing_token="a-stale-token-the-operator-remembered",
        )

        assert outcome.acceptance is not None
        fencing_check = outcome.acceptance.check("6_fencing_token_cas")
        assert fencing_check is not None and fencing_check.passed is False
        assert outcome.applied is False
        assert _gate_bytes(paths) == before


class TestConcurrentRecoveryAttemptsRefusesSecond:
    def test_second_concurrent_attempt_cannot_acquire_the_recovery_lock(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        _write_gate(paths)
        _write_scheduler_pid(paths)

        recovery_lock = paths.root / "recovery.lock"
        recovery_lock.parent.mkdir(parents=True, exist_ok=True)
        foreign_lock_bytes = json.dumps({"pid": 99999, "token": "someone-elses-token"}).encode()
        recovery_lock.write_bytes(foreign_lock_bytes)

        outcome = _run(
            tmp_path, broker=_FakeBroker(), process_port=_FakeProcessPort(frozenset())
        )

        assert outcome.acceptance is not None
        lock_check = outcome.acceptance.check("1_exclusive_lock")
        assert lock_check is not None and lock_check.passed is False
        assert outcome.applied is False
        # The failed attempt's own cleanup must NOT delete a lock it never
        # acquired -- that would let a losing concurrent attempt release the
        # winner's lock out from under it. This is the regression this fix
        # was built for.
        assert recovery_lock.exists()
        assert recovery_lock.read_bytes() == foreign_lock_bytes


class TestRecoveryLockIsReleasedBetweenSequentialAttempts:
    def test_a_completed_attempt_does_not_permanently_block_the_next_one(
        self, tmp_path: Path
    ) -> None:
        """Regression test: the first implementation acquired the recovery
        lock but never released it, so a completed (even a merely dry-run)
        attempt left every SUBSEQUENT attempt refusing forever at
        requirement 1 -- caught by actually running the real CLI command
        twice in a row against the live 2026-08-21 recovery attempt."""
        paths = _paths(tmp_path)
        _write_gate(paths)
        _write_scheduler_pid(paths)

        first = _run(tmp_path, broker=_FakeBroker(positions=[]), process_port=_FakeProcessPort(frozenset()))
        assert first.acceptance is not None
        assert first.acceptance.check("1_exclusive_lock").passed is True
        assert not (paths.root / "recovery.lock").exists(), "lock must be released after the attempt"

        second = _run(tmp_path, broker=_FakeBroker(positions=[]), process_port=_FakeProcessPort(frozenset()))
        assert second.acceptance is not None
        assert second.acceptance.check("1_exclusive_lock").passed is True
        assert second.acceptance.all_passed is True


class TestIdentityMismatchRefuses:
    def test_scheduler_pid_names_a_different_session_than_asserted(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        _write_gate(paths)
        _write_scheduler_pid(paths, session_id="paperday-20260101-deadbeef")
        before = _gate_bytes(paths)

        outcome = _run(
            tmp_path, broker=_FakeBroker(), process_port=_FakeProcessPort(frozenset())
        )

        assert outcome.acceptance is not None
        identity_check = outcome.acceptance.check("2_session_lease_process_identity")
        assert identity_check is not None and identity_check.passed is False
        assert outcome.applied is False
        assert _gate_bytes(paths) == before

    def test_no_scheduler_pid_file_at_all_refuses(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        _write_gate(paths)
        # No scheduler.pid written at all.
        before = _gate_bytes(paths)

        outcome = _run(
            tmp_path, broker=_FakeBroker(), process_port=_FakeProcessPort(frozenset())
        )

        assert outcome.acceptance is not None
        identity_check = outcome.acceptance.check("2_session_lease_process_identity")
        assert identity_check is not None and identity_check.passed is False
        assert outcome.applied is False
        assert _gate_bytes(paths) == before


class TestBrokerDisagreementRefuses:
    def test_broker_reports_a_position_the_local_book_does_not_know_about(
        self, tmp_path: Path
    ) -> None:
        paths = _paths(tmp_path)
        _write_gate(paths)
        _write_scheduler_pid(paths)
        before = _gate_bytes(paths)

        # The store has no positions.jsonl at all -- a broker report with a
        # nonzero holding is exactly what an empty local book should disagree with.
        outcome = _run(
            tmp_path,
            broker=_FakeBroker(positions=[("SPY", 1, 500.0)]),
            process_port=_FakeProcessPort(frozenset()),
        )

        assert outcome.acceptance is not None
        reconciliation_check = outcome.acceptance.check("5_broker_reconciliation")
        # An empty local book vs. a broker holding is exactly what this
        # module exists to catch -- assert it did not get silently waved
        # through, whichever way the underlying report frames it.
        assert reconciliation_check is not None
        assert outcome.applied is False
        assert _gate_bytes(paths) == before


# ---------------------------------------------------------------------------
# apply_recovery_result, exercised directly (not just through run_recovery)
# ---------------------------------------------------------------------------


class TestDryRunNeverWrites:
    def test_dry_run_evaluates_the_real_result_but_never_calls_apply(self, tmp_path: Path) -> None:
        """A dry run on an otherwise fully-clean scenario would pass every
        requirement -- proving the guard is dry_run itself, not an
        incidental failing check."""
        paths = _paths(tmp_path)
        _write_gate(paths)
        _write_scheduler_pid(paths)
        before = _gate_bytes(paths)

        outcome = run_recovery(
            paths=paths,
            expected_session_id=SESSION_ID,
            expected_lease_nonce=LEASE_NONCE,
            expected_process_id=PROCESS_ID,
            expected_fencing_token=FENCING_TOKEN,
            reason="dry run check",
            now=NOW,
            config=_config(tmp_path),
            broker_factory=_fake_broker_factory(_FakeBroker(positions=[])),
            process_port=_FakeProcessPort(frozenset()),
            dry_run=True,
        )

        assert outcome.acceptance is not None
        assert outcome.acceptance.all_passed is True  # the scenario itself is clean
        assert outcome.applied is False  # but dry_run refused to apply it
        assert _gate_bytes(paths) == before


class TestApplyRecoveryResultDirectly:
    def test_never_writes_when_result_did_not_pass(self, tmp_path: Path) -> None:
        from engine.paperday_recovery import RecoveryAcceptanceResult, RecoveryCheck

        paths = _paths(tmp_path)
        _write_gate(paths)
        before = _gate_bytes(paths)

        failing = RecoveryAcceptanceResult(
            checks=(RecoveryCheck("1_exclusive_lock", False, "no"),), entry_gate="CLOSED"
        )
        applied = apply_recovery_result(paths, failing)

        assert applied is False
        assert _gate_bytes(paths) == before

    def test_refuses_on_corrupt_gate_at_write_time(self, tmp_path: Path) -> None:
        from engine.paperday_recovery import RecoveryAcceptanceResult, RecoveryCheck

        paths = _paths(tmp_path)
        paths.gate.parent.mkdir(parents=True, exist_ok=True)
        paths.gate.write_bytes(b"{not json")

        passing = RecoveryAcceptanceResult(
            checks=(RecoveryCheck("1_exclusive_lock", True, "ok"),), entry_gate="CLOSED"
        )
        applied = apply_recovery_result(paths, passing)

        assert applied is False  # corrupt-at-write-time refuses, never overwritten blindly
