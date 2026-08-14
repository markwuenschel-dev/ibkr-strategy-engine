"""The scheduler's operational scenarios, as tests rather than as incidents.

The driver is the piece that runs unattended, so every way it can be wrong is a
way to be wrong while nobody is watching. The matrix below is the list of those
ways: a lease that went away, a lease that was taken by somebody else, a session
window that closed, a shutdown that arrived mid-pass, a PID the OS handed to a
stranger, and a scheduler left over from yesterday that runs the very same
script as today's.

Nothing here sleeps or reads a real clock -- see ``scheduler_support``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from engine.scheduler import (
    SchedulerIdentity,
    SchedulerLoop,
    SchedulerSpec,
    TickReceipt,
    TickOutcome,
    adopt_or_spawn,
    announce_ready,
    drain_and_stop,
    quiesce_requested,
    request_quiesce,
    session_id_holding,
)
import engine.scheduler as scheduler_module
from scheduler_support import (  # noqa: E402 - sibling test module, see docstring
    NONCE,
    NOW,
    OTHER_SESSION_ID,
    SESSION_ID,
    FakeClock,
    FakeEngine,
    FakeProcesses,
    always_closed,
    always_open,
    identity,
    paths_for,
    read_receipts,
    write_lock,
    write_terminal,
)

PASS_COMMAND = ("options-run", "--symbol", "SPY", "--arm")


def loop_for(
    tmp_path: Path,
    *,
    engine: FakeEngine | None = None,
    is_open=always_open,
    clock: FakeClock | None = None,
    session_id: str = SESSION_ID,
    lock_session_id: str | None = SESSION_ID,
) -> tuple[SchedulerLoop, FakeClock, FakeEngine]:
    paths = paths_for(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    lock = paths.root / "session.lock"
    if lock_session_id is not None:
        write_lock(paths, lock_session_id)
    clock = clock or FakeClock()
    engine = engine or FakeEngine()
    loop = SchedulerLoop(
        identity=identity(session_id=session_id),
        paths=paths,
        lock=lock,
        cadence_seconds=60.0,
        is_open=is_open,
        command=PASS_COMMAND,
        engine=engine,
        clock=clock,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return loop, clock, engine


# ===========================================================================
# The loop
# ===========================================================================


class TestTheLoopRunsPassesOnItsCadence:
    """A driver that does not actually drive is the gap this closes (G5)."""

    def test_each_tick_runs_one_pass_and_sleeps_the_cadence(self, tmp_path: Path) -> None:
        loop, clock, engine = loop_for(tmp_path)

        loop.run(max_ticks=3)

        assert [call for call in engine.calls] == [list(PASS_COMMAND)] * 3
        assert clock.slept == [60.0, 60.0, 60.0]

    def test_every_tick_lands_a_durable_receipt(self, tmp_path: Path) -> None:
        loop, _clock, _engine = loop_for(tmp_path)

        loop.run(max_ticks=2)

        on_disk = read_receipts(loop.paths)
        assert [r["outcome"] for r in on_disk] == ["RAN", "RAN", "STOPPED_TICK_BUDGET"]
        assert all(r["tick_id"] for r in on_disk)
        assert on_disk[0]["command"] == list(PASS_COMMAND)

    def test_tick_ids_are_unique(self, tmp_path: Path) -> None:
        loop, _clock, _engine = loop_for(tmp_path)

        loop.run(max_ticks=3)

        ids = [r.tick_id for r in loop.receipts]
        assert len(ids) == len(set(ids)), ids


class TestReceiptPublication:
    def _receipt(self, tick_id: str) -> TickReceipt:
        return TickReceipt(
            tick_id=tick_id,
            at=NOW,
            outcome=TickOutcome.RAN,
            detail=f"receipt {tick_id}",
            command=PASS_COMMAND,
        )

    def test_existing_jsonl_is_preserved_and_atomic_writer_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = paths_for(tmp_path)
        path = paths.receipts_for(NOW.date())
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._receipt("existing").to_record()
        path.write_text(json.dumps(existing, sort_keys=True) + "\n", encoding="utf-8")
        real_atomic_writer = scheduler_module._atomic_write_text
        calls: list[tuple[Path, str]] = []

        def recording_atomic_writer(target: Path, content: str) -> None:
            calls.append((target, content))
            real_atomic_writer(target, content)

        monkeypatch.setattr(
            scheduler_module, "_atomic_write_text", recording_atomic_writer
        )

        scheduler_module._append_receipt(paths, self._receipt("new"))

        assert calls and calls[0][0] == path
        assert calls[0][1].count("\n") == 2
        assert [record["tick_id"] for record in read_receipts(paths)] == [
            "existing",
            "new",
        ]

    def test_failed_replacement_leaves_prior_receipts_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = paths_for(tmp_path)
        path = paths.receipts_for(NOW.date())
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._receipt("existing").to_record()
        path.write_text(json.dumps(existing, sort_keys=True) + "\n", encoding="utf-8")
        before = path.read_bytes()

        def fail_replace(_temporary: Path, _target: Path) -> None:
            raise OSError("injected replace failure")

        monkeypatch.setattr(scheduler_module.os, "replace", fail_replace)

        with pytest.raises(OSError, match="injected replace failure"):
            scheduler_module._append_receipt(paths, self._receipt("new"))

        assert path.read_bytes() == before
        assert [record["tick_id"] for record in read_receipts(paths)] == ["existing"]

    def test_malformed_existing_jsonl_fails_closed_without_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = paths_for(tmp_path)
        path = paths.receipts_for(NOW.date())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"tick_id": "existing"}\nnot-json\n', encoding="utf-8")
        before = path.read_bytes()
        atomic_calls: list[Path] = []

        def unexpected_atomic_writer(target: Path, _content: str) -> None:
            atomic_calls.append(target)

        monkeypatch.setattr(
            scheduler_module, "_atomic_write_text", unexpected_atomic_writer
        )

        with pytest.raises(ValueError, match="malformed receipt JSON"):
            scheduler_module._append_receipt(paths, self._receipt("new"))

        assert atomic_calls == []
        assert path.read_bytes() == before


class TestTheWindowIsRespected:
    """A pass outside the session window is broker traffic with nothing to do."""

    def test_a_closed_session_skips_the_pass_entirely(self, tmp_path: Path) -> None:
        loop, _clock, engine = loop_for(tmp_path, is_open=always_closed)

        loop.run(max_ticks=3)

        assert engine.calls == [], "the engine must not be invoked outside the window"
        assert {r.outcome for r in loop.receipts} == {
            TickOutcome.SKIPPED_SESSION_CLOSED,
            TickOutcome.STOPPED_TICK_BUDGET,
        }

    def test_a_skip_is_recorded_not_silently_omitted(self, tmp_path: Path) -> None:
        """A receipt file holding only the ticks that ran cannot tell 'window
        closed' from 'scheduler dead' -- which is the operator's real question."""
        loop, _clock, _engine = loop_for(tmp_path, is_open=always_closed)

        loop.run(max_ticks=2)

        assert [r["outcome"] for r in read_receipts(loop.paths)][:2] == [
            "SKIPPED_SESSION_CLOSED",
            "SKIPPED_SESSION_CLOSED",
        ]


class TestTheLeaseIsWhatAuthorises:
    """The stale-gate race, at the driver: a scheduler must not outlive its session."""

    def test_a_missing_lock_stops_the_loop_before_any_pass(self, tmp_path: Path) -> None:
        loop, _clock, engine = loop_for(tmp_path, lock_session_id=None)

        loop.run(max_ticks=5)

        assert engine.calls == []
        assert loop.receipts[-1].outcome is TickOutcome.STOPPED_LEASE_LOST

    def test_a_lock_held_by_another_session_stops_the_loop(self, tmp_path: Path) -> None:
        """A NEW session acquiring a NEW lock must not re-license a predecessor.
        Checking only that *a* lock exists is the hole this closes."""
        loop, _clock, engine = loop_for(tmp_path, lock_session_id=OTHER_SESSION_ID)

        loop.run(max_ticks=5)

        assert engine.calls == []
        assert loop.receipts[-1].outcome is TickOutcome.STOPPED_LEASE_LOST
        assert SESSION_ID in loop.receipts[-1].detail

    def test_the_lock_disappearing_mid_pass_is_recorded_as_unresolved(
        self, tmp_path: Path
    ) -> None:
        """The pass already ran. It may have transmitted, and nothing is now
        watching for the outcome -- that must not be smoothed into a success."""
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        lock = paths.root / "session.lock"

        engine = FakeEngine(on_run=lambda: lock.unlink())
        loop, clock, engine = loop_for(tmp_path, engine=engine)

        loop.run(max_ticks=5)

        assert len(engine.calls) == 1, "exactly one pass should have run"
        assert loop.receipts[-1].outcome is TickOutcome.UNRESOLVED_LEASE_LOST_MID_TICK
        assert "reconcile" in loop.receipts[-1].detail
        # It must stop *now*, not sleep a cadence and notice on the next tick.
        assert clock.slept == [], "a scheduler that lost its mandate must not wait"
        assert len(loop.receipts) == 1, loop.receipts

    def test_a_malformed_lock_reads_as_no_lease(self, tmp_path: Path) -> None:
        """Fail closed. A lock the scheduler cannot parse is not a mandate."""
        loop, _clock, engine = loop_for(tmp_path)
        loop.lock.write_text("{ this is not json", encoding="utf-8")

        loop.run(max_ticks=3)

        assert engine.calls == []
        assert loop.receipts[-1].outcome is TickOutcome.STOPPED_LEASE_LOST

    def test_a_lock_without_a_session_id_reads_as_no_lease(self, tmp_path: Path) -> None:
        loop, _clock, engine = loop_for(tmp_path)
        loop.lock.write_text('{"controller_pid": 1}', encoding="utf-8")

        loop.run(max_ticks=3)

        assert engine.calls == []
        assert loop.receipts[-1].outcome is TickOutcome.STOPPED_LEASE_LOST

    def test_terminal_receipt_marks_a_normal_loop_exit_as_clean(self, tmp_path: Path) -> None:
        loop, _clock, _engine = loop_for(tmp_path)

        loop.run(max_ticks=0)

        terminal = scheduler_module.read_terminal_receipt(loop.paths)
        assert terminal is not None
        assert terminal["session_id"] == SESSION_ID
        assert terminal["nonce"] == NONCE
        assert terminal["clean_exit"] is True

    def test_unresolved_mid_tick_exit_is_not_a_clean_shutdown(self, tmp_path: Path) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        lock = paths.root / "session.lock"
        write_lock(paths)
        loop = SchedulerLoop(
            identity=identity(), paths=paths, lock=lock, cadence_seconds=60.0,
            is_open=always_open, command=PASS_COMMAND, engine=FakeEngine(on_run=lock.unlink),
        )

        loop.run(max_ticks=5)

        terminal = scheduler_module.read_terminal_receipt(paths)
        assert terminal is not None
        assert terminal["outcome"] == "UNRESOLVED_LEASE_LOST_MID_TICK"
        assert terminal["clean_exit"] is False


class TestQuiesce:
    """Shutdown asks; it does not shoot."""

    def test_a_quiesce_request_stops_the_loop_without_running_a_pass(
        self, tmp_path: Path
    ) -> None:
        loop, _clock, engine = loop_for(tmp_path)
        request_quiesce(loop.paths, reason="paper-day stop", now=NOW)

        loop.run(max_ticks=5)

        assert engine.calls == []
        assert loop.receipts[-1].outcome is TickOutcome.STOPPED_QUIESCED

    def test_a_quiesce_arriving_between_ticks_ends_the_loop_after_the_current_one(
        self, tmp_path: Path
    ) -> None:
        loop, _clock, engine = loop_for(tmp_path)
        original = loop.engine.run

        def run_then_request(args, **kwargs):
            result = original(args, **kwargs)
            request_quiesce(loop.paths, reason="stop", now=NOW)
            return result

        loop.engine.run = run_then_request  # type: ignore[method-assign]

        loop.run(max_ticks=5)

        assert len(engine.calls) == 1, "the in-flight tick completes, the next does not start"
        assert loop.receipts[-1].outcome is TickOutcome.STOPPED_QUIESCED


class TestTheLoopRefusesNonsense:
    """A cadence or a command the caller never chose is a policy default in disguise."""

    def test_a_non_positive_cadence_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cadence_seconds must be positive"):
            loop_for(tmp_path)[0].__class__(
                identity=identity(),
                paths=paths_for(tmp_path),
                lock=tmp_path / "lock",
                cadence_seconds=0,
                is_open=always_open,
                command=PASS_COMMAND,
                engine=FakeEngine(),
            )

    def test_an_empty_command_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="command must name"):
            SchedulerLoop(
                identity=identity(),
                paths=paths_for(tmp_path),
                lock=tmp_path / "lock",
                cadence_seconds=60.0,
                is_open=always_open,
                command=(),
                engine=FakeEngine(),
            )

    def test_an_identity_without_a_nonce_is_refused(self) -> None:
        with pytest.raises(ValueError, match="non-empty nonce"):
            SchedulerIdentity(session_id=SESSION_ID, nonce="")

    def test_a_spec_pointing_at_a_missing_entry_script_is_refused(
        self, tmp_path: Path
    ) -> None:
        """This repository ships no production scheduler entrypoint yet, so a
        spec can easily name one that does not exist. Refused here, where it is
        written, rather than surfacing later as a child that spawns and never
        announces readiness."""
        with pytest.raises(ValueError) as exc:
            SchedulerSpec(
                cadence_seconds=60.0,
                command=PASS_COMMAND,
                entry_script=tmp_path / "not-written-yet.py",
            )
        assert "does not exist" in str(exc.value)
        assert "no production scheduler entrypoint" in str(exc.value)


# ===========================================================================
# Supervision
# ===========================================================================


def spec_for(tmp_path: Path) -> SchedulerSpec:
    # The spec refuses a script that does not exist, so the fixture makes one.
    # There is no production entrypoint in the repository yet -- that is a
    # tracked blocker on unattended operation, not an oversight here.
    script = tmp_path / "run_scheduler.py"
    script.write_text("# placeholder scheduler entrypoint\n", encoding="utf-8")
    return SchedulerSpec(
        cadence_seconds=60.0,
        command=PASS_COMMAND,
        entry_script=script,
    )


def adopt(
    processes: FakeProcesses,
    paths,
    me: SchedulerIdentity,
    tmp_path: Path,
    *,
    clock: FakeClock | None = None,
    ready_timeout: float = 30.0,
):
    clock = clock or FakeClock()
    return adopt_or_spawn(
        processes=processes,
        paths=paths,
        identity=me,
        spec=spec_for(tmp_path),
        cwd=tmp_path,
        env={},
        clock=clock,
        sleep=clock.sleep,
        python="python",
        monotonic=clock.monotonic,
        ready_timeout=ready_timeout,
    )


class TestAdoptOrSpawn:
    """Yesterday's scheduler runs today's script. Identity has to be finer than that."""

    def test_a_scheduler_for_this_identity_is_adopted(self, tmp_path: Path) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses(announce_paths=paths)
        me = identity()
        pid = processes.add(f"python run_scheduler.py {me.needle}")
        paths.pid.write_text(f'{{"pid": {pid}}}', encoding="utf-8")

        adopted, detail = adopt(processes, paths, me, tmp_path)

        assert adopted == pid, detail
        assert processes.spawned == [], "an adopted scheduler must not be duplicated"

    def test_a_previous_sessions_scheduler_is_not_adopted(self, tmp_path: Path) -> None:
        """Same script, same script name, different session. The builder
        watcher's needle-only check would have taken it."""
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses(announce_paths=paths)
        stale = SchedulerIdentity(session_id=OTHER_SESSION_ID, nonce="deadbeef")
        pid = processes.add(f"python run_scheduler.py {stale.needle}")
        paths.pid.write_text(f'{{"pid": {pid}}}', encoding="utf-8")

        adopted, detail = adopt(processes, paths, identity(), tmp_path)

        assert adopted is None, detail
        assert "not terminated" in detail
        assert pid not in processes.terminated, "a stranger's process is never killed"

    def test_a_spawned_scheduler_carries_its_nonce_and_its_policy(
        self, tmp_path: Path
    ) -> None:
        """The child cannot obey a cadence nobody told it."""
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses(announce_paths=paths)
        me = identity()

        pid, detail = adopt(processes, paths, me, tmp_path)

        assert pid is not None, detail
        assert me.needle in processes.cmdline(pid)
        spawned = " ".join(processes.spawned[0])
        assert "--cadence-seconds=60" in spawned, spawned
        assert "options-run --symbol SPY --arm" in spawned, spawned

    def test_an_atomic_start_claim_prevents_a_second_spawn_before_pid_publication(
        self, tmp_path: Path
    ) -> None:
        """Two controllers can race before the child handshake publishes pid.

        The claim is the serialization point; a PID read alone is too late.
        """
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.claim.write_text(
            json.dumps(
                {"v": 1, "session_id": SESSION_ID, "nonce": NONCE}
            ),
            encoding="utf-8",
        )
        processes = FakeProcesses(announce_paths=paths)

        pid, detail = adopt(processes, paths, identity(), tmp_path)

        assert pid is None
        assert "atomic claim" in detail
        assert processes.spawned == []

    def test_a_spawn_failure_is_reported_not_raised(self, tmp_path: Path) -> None:
        """An absent scheduler degrades a day. It does not invalidate the book."""
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses(announce_paths=paths)
        processes.spawn_error = OSError("no python")

        pid, detail = adopt(processes, paths, identity(), tmp_path)

        assert pid is None
        assert "no python" in detail

    def test_a_child_that_never_reports_in_is_not_recorded(self, tmp_path: Path) -> None:
        """Liveness is not readiness. A process that started, failed to load its
        policy and is about to exit is alive for exactly as long as it takes to
        fool a bare ``alive()`` check."""
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses(announce_paths=paths, announces_ready=False)

        pid, detail = adopt(processes, paths, identity(), tmp_path, ready_timeout=5.0)

        assert pid is None
        assert "did not announce readiness" in detail
        assert not paths.pid.exists(), "an unconfirmed child must not be recorded as ours"

    def test_a_heartbeat_from_another_session_is_not_our_handshake(
        self, tmp_path: Path
    ) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses(announce_paths=paths, announces_ready=False)
        announce_ready(
            paths, SchedulerIdentity(session_id=OTHER_SESSION_ID, nonce="deadbeef"), now=NOW
        )

        pid, detail = adopt(processes, paths, identity(), tmp_path, ready_timeout=5.0)

        assert pid is None, detail
        assert "did not announce readiness" in detail

    def test_a_stale_quiesce_flag_is_cleared_before_spawning(self, tmp_path: Path) -> None:
        """Otherwise the new scheduler reads yesterday's shutdown and stops instantly."""
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        request_quiesce(paths, reason="yesterday", now=NOW)
        processes = FakeProcesses(announce_paths=paths)

        adopt(processes, paths, identity(), tmp_path)

        assert not quiesce_requested(paths)

    def test_readiness_is_fenced_again_before_pid_publication(self, tmp_path: Path) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses(announce_paths=paths)
        processes.after_announce = lambda pid: processes.table.__setitem__(pid, "chrome.exe")

        pid, detail = adopt(processes, paths, identity(), tmp_path)

        assert pid is None
        assert "changed identity after announcing readiness" in detail
        assert not paths.pid.exists()

    def test_readiness_fence_rejects_a_child_that_exits_before_pid_publication(
        self, tmp_path: Path
    ) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses(announce_paths=paths)
        alive_calls = 0

        def exit_on_final_fence(pid: int) -> bool:
            nonlocal alive_calls
            alive_calls += 1
            if alive_calls == 2:
                processes.kill_silently(pid)
            return pid in processes.table

        processes.alive = exit_on_final_fence  # type: ignore[method-assign]

        pid, detail = adopt(processes, paths, identity(), tmp_path)

        assert pid is None
        assert "exited after announcing readiness" in detail
        assert not paths.pid.exists()

    def test_heartbeat_and_pid_are_published_with_atomic_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        replacements: list[tuple[Path, Path]] = []
        real_replace = os.replace

        def observe_replace(source: str | bytes, target: str | bytes) -> None:
            replacements.append((Path(source), Path(target)))
            real_replace(source, target)

        monkeypatch.setattr(scheduler_module.os, "replace", observe_replace)
        announce_ready(paths, identity(), now=NOW)
        processes = FakeProcesses(announce_paths=paths)
        adopt(processes, paths, identity(), tmp_path)

        targets = [target for _source, target in replacements]
        assert paths.heartbeat in targets
        assert paths.pid in targets
        assert not list(paths.root.glob("*.tmp"))

    def test_quiesce_and_terminal_markers_are_published_with_atomic_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        replacements: list[tuple[Path, Path]] = []
        real_replace = os.replace

        def observe_replace(source: str | bytes, target: str | bytes) -> None:
            replacements.append((Path(source), Path(target)))
            real_replace(source, target)

        monkeypatch.setattr(scheduler_module.os, "replace", observe_replace)
        request_quiesce(paths, reason="paper-day stop", now=NOW)
        loop, _clock, _engine = loop_for(tmp_path)
        loop.run(max_ticks=0)

        targets = [target for _source, target in replacements]
        assert paths.quiesce in targets
        assert paths.terminal in targets
        assert json.loads(paths.quiesce.read_text(encoding="utf-8"))["v"] == 1
        assert not list(paths.root.glob("*.tmp"))


class TestAStaleTerminalMarkerCannotForgeACleanExit:
    """A marker left by a previous scheduler must not vouch for its successor.

    Dangerous precisely BECAUSE idempotent restart reuses the nonce.
    ``_clean_exit_proven`` matches on (session_id, nonce), so a marker written
    by an earlier scheduler of the SAME session satisfies it for the next one.
    If that successor then dies uncleanly, stop reads the ancestor's marker and
    reports a clean shutdown for a tick nobody accounted for. Clearing the
    marker at spawn is the only thing between those two facts -- and a mutation
    sweep on 2026-08-13 found it pinning nothing.
    """

    def test_spawning_clears_a_marker_left_by_the_previous_scheduler(
        self, tmp_path: Path
    ) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses(announce_paths=paths)
        me = identity()
        paths.terminal.write_text(
            json.dumps(
                {"session_id": me.session_id, "nonce": me.nonce, "clean_exit": True}
            ),
            encoding="utf-8",
        )

        pid, detail = adopt(processes, paths, me, tmp_path)

        assert pid is not None, detail
        assert not paths.terminal.exists(), (
            "the ancestor's clean-exit marker survived the spawn and can now "
            "vouch for a scheduler that never exited cleanly"
        )

    def test_a_successor_that_dies_uncleanly_is_not_excused_by_its_ancestor(
        self, tmp_path: Path
    ) -> None:
        """The end-to-end consequence, not merely the file's absence."""
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses(announce_paths=paths)
        me = identity()
        paths.terminal.write_text(
            json.dumps(
                {"session_id": me.session_id, "nonce": me.nonce, "clean_exit": True}
            ),
            encoding="utf-8",
        )

        pid, detail = adopt(processes, paths, me, tmp_path)
        assert pid is not None, detail
        processes.kill_silently(pid)  # the successor dies without reporting in

        clock = FakeClock()
        clean, stop_detail = drain_and_stop(
            processes=processes, paths=paths, identity=me, now=NOW,
            drain_timeout=5.0, sleep=clock.sleep, monotonic=clock.monotonic,
        )

        assert not clean, "an ancestor's marker excused an unclean death: " + stop_detail
        assert "STOP_DIRTY" in stop_detail


class TestDrainAndStop:
    """Killing a tick that has already transmitted strands the outcome."""

    def test_a_scheduler_that_exits_in_time_is_a_clean_stop(self, tmp_path: Path) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses()
        me = identity()
        pid = processes.add(f"python run_scheduler.py {me.needle}")
        paths.pid.write_text(f'{{"pid": {pid}}}', encoding="utf-8")
        write_terminal(paths, me)
        clock = FakeClock()

        def sleep_then_exit(seconds: float) -> None:
            clock.sleep(seconds)
            processes.kill_silently(pid)

        clean, detail = drain_and_stop(
            processes=processes, paths=paths, identity=me, now=NOW,
            drain_timeout=30.0, sleep=sleep_then_exit, monotonic=clock.monotonic,
        )

        assert clean, detail
        assert pid not in processes.terminated, "a drained scheduler is never forced"
        assert not paths.pid.exists()

    @pytest.mark.parametrize("pid_value", [None, "9000", -1, True])
    def test_missing_or_invalid_pid_without_clean_receipt_is_stop_dirty(
        self, tmp_path: Path, pid_value: object
    ) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        record = {"session_id": SESSION_ID, "nonce": NONCE}
        if pid_value is not None:
            record["pid"] = pid_value
        paths.pid.write_text(__import__("json").dumps(record), encoding="utf-8")

        clean, detail = drain_and_stop(
            processes=FakeProcesses(), paths=paths, identity=identity(), now=NOW,
            drain_timeout=5.0, sleep=FakeClock().sleep, monotonic=FakeClock().monotonic,
        )

        assert not clean
        assert "STOP_DIRTY" in detail
        assert paths.pid.exists(), "unproven state must not be erased"

    def test_malformed_pid_file_is_stop_dirty(self, tmp_path: Path) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.pid.write_text("{ torn", encoding="utf-8")

        clean, detail = drain_and_stop(
            processes=FakeProcesses(), paths=paths, identity=identity(), now=NOW,
            drain_timeout=5.0, sleep=FakeClock().sleep, monotonic=FakeClock().monotonic,
        )

        assert not clean
        assert "unreadable or malformed" in detail
        assert quiesce_requested(paths)

    def test_dead_pid_with_durable_clean_exit_receipt_is_clean(self, tmp_path: Path) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        me = identity()
        paths.pid.write_text('{"pid": 9000, "session_id": "%s", "nonce": "%s"}' % (SESSION_ID, NONCE), encoding="utf-8")
        write_terminal(paths, me)

        clean, detail = drain_and_stop(
            processes=FakeProcesses(), paths=paths, identity=me, now=NOW,
            drain_timeout=5.0, sleep=FakeClock().sleep, monotonic=FakeClock().monotonic,
        )

        assert clean
        assert "durable clean exit" in detail
        assert not paths.pid.exists()

    def test_dead_pid_with_unresolved_terminal_receipt_is_stop_dirty(self, tmp_path: Path) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        me = identity()
        paths.pid.write_text('{"pid": 9000, "session_id": "%s", "nonce": "%s"}' % (SESSION_ID, NONCE), encoding="utf-8")
        write_terminal(paths, me, clean_exit=False, outcome="UNRESOLVED_LEASE_LOST_MID_TICK")

        clean, detail = drain_and_stop(
            processes=FakeProcesses(), paths=paths, identity=me, now=NOW,
            drain_timeout=5.0, sleep=FakeClock().sleep, monotonic=FakeClock().monotonic,
        )

        assert not clean
        assert "STOP_DIRTY" in detail

    def test_a_scheduler_that_overruns_the_bound_is_stop_dirty(self, tmp_path: Path) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses()
        me = identity()
        pid = processes.add(f"python run_scheduler.py {me.needle}")
        paths.pid.write_text(f'{{"pid": {pid}}}', encoding="utf-8")
        clock = FakeClock()

        clean, detail = drain_and_stop(
            processes=processes, paths=paths, identity=me, now=NOW,
            drain_timeout=5.0, sleep=clock.sleep, monotonic=clock.monotonic,
        )

        assert not clean
        assert "STOP_DIRTY" in detail
        assert "reconcile" in detail
        assert pid in processes.terminated

    def test_a_reused_pid_is_never_terminated(self, tmp_path: Path) -> None:
        """A PID is a name the OS reuses -- the 2026-08-01 mutation sweep found
        this exact guard unpinned on the builder watcher.

        The stop is reported DIRTY, not clean: our scheduler could not be
        located, so nothing here proves it stopped. The quiesce flag is what
        actually halts a live tick; this result is the honest statement that we
        did not watch it happen."""
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses()
        pid = processes.add("chrome.exe")
        paths.pid.write_text(f'{{"pid": {pid}}}', encoding="utf-8")
        clock = FakeClock()

        clean, detail = drain_and_stop(
            processes=processes, paths=paths, identity=identity(), now=NOW,
            drain_timeout=5.0, sleep=clock.sleep, monotonic=clock.monotonic,
        )

        assert not clean, "an unlocatable scheduler is an unproven shutdown"
        assert "STOP_DIRTY" in detail
        assert pid not in processes.terminated
        assert processes.alive(pid)
        assert processes.cmdline(pid) == "chrome.exe"
        assert quiesce_requested(paths), "the flag must be down even when we cannot watch"

    def test_a_pid_reused_during_the_drain_is_not_terminated(self, tmp_path: Path) -> None:
        """The identity check happens up to drain_timeout before the kill. In
        that window our scheduler can exit and the OS hand its number away."""
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses()
        me = identity()
        pid = processes.add(f"python run_scheduler.py {me.needle}")
        paths.pid.write_text(f'{{"pid": {pid}}}', encoding="utf-8")
        clock = FakeClock()

        def sleep_then_swap(seconds: float) -> None:
            clock.sleep(seconds)
            processes.table[pid] = "chrome.exe"  # same number, different process

        clean, detail = drain_and_stop(
            processes=processes, paths=paths, identity=me, now=NOW,
            drain_timeout=5.0, sleep=sleep_then_swap, monotonic=clock.monotonic,
        )

        assert not clean
        assert "stopped matching this scheduler" in detail
        assert pid not in processes.terminated, "the new occupant must survive"
        assert processes.cmdline(pid) == "chrome.exe"

    def test_stopping_with_no_pid_file_is_clean(self, tmp_path: Path) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        clock = FakeClock()

        clean, detail = drain_and_stop(
            processes=FakeProcesses(), paths=paths, identity=identity(), now=NOW,
            drain_timeout=5.0, sleep=clock.sleep, monotonic=clock.monotonic,
        )

        assert clean
        assert "nothing to stop" in detail

    def test_stop_requests_the_quiesce_before_waiting(self, tmp_path: Path) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        processes = FakeProcesses()
        me = identity()
        pid = processes.add(f"python run_scheduler.py {me.needle}")
        paths.pid.write_text(f'{{"pid": {pid}}}', encoding="utf-8")
        clock = FakeClock()
        seen: list[bool] = []

        def observe(seconds: float) -> None:
            seen.append(quiesce_requested(paths))
            clock.sleep(seconds)
            processes.kill_silently(pid)

        drain_and_stop(
            processes=processes, paths=paths, identity=me, now=NOW,
            drain_timeout=30.0, sleep=observe, monotonic=clock.monotonic,
        )

        assert seen and all(seen), "the quiesce must be visible before the first wait"


class TestSessionIdHolding:
    """The lease reader, on its own -- everything above depends on it."""

    def test_a_well_formed_lock_yields_its_session_id(self, tmp_path: Path) -> None:
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        lock = write_lock(paths)

        assert session_id_holding(lock) == SESSION_ID

    def test_an_absent_lock_yields_none(self, tmp_path: Path) -> None:
        assert session_id_holding(tmp_path / "nope.lock") is None
