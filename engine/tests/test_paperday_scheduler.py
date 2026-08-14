"""The scheduler's place in the paper day: when it starts, and when it is drained.

Two properties carry the weight here.

**A day without a scheduler policy is the day that existed before.** No spec
means no check, no process, no behaviour change -- which is why the whole
pre-existing paper-day matrix still passes untouched. A default cadence hiding
in the controller would have made "unattended" the accident rather than the
decision.

**Stop drains the scheduler before anything transmits.** Closing the entry gate
bounds what a live tick may still *open*; it does nothing about a tick that
already passed the gate and is inside the engine. Step 2 cancels working orders
with ``--arm`` and the session lock survives to step 8, so a scheduler still
ticking through them is a pass racing a cancel. The drain therefore sits between
step 1 and step 2, and the ordering is asserted rather than commented.
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.paperday import DEGRADED, READY
from engine.scheduler import SchedulerIdentity, SchedulerPaths, SchedulerSpec

from paperday_support import NOW, WATCHER_CMD, harness  # noqa: E402 - sibling test module

NONCE = "cafe1234"


def spec_for(tmp_path: Path) -> SchedulerSpec:
    # The spec refuses a script that does not exist, so the fixture makes one.
    # There is no production entrypoint in the repository yet -- that is a
    # tracked blocker on unattended operation, not an oversight here.
    script = tmp_path / "run_scheduler.py"
    script.write_text("# placeholder scheduler entrypoint\n", encoding="utf-8")
    return SchedulerSpec(
        cadence_seconds=300.0,
        command=("options-run", "--symbol", "SPY", "--arm"),
        entry_script=script,
    )


def _step_names(report) -> list[str]:
    return [step.name for step in report.steps]


def _write_lock(h, session_id: str, fencing_token: str) -> None:
    h.paths.root.mkdir(parents=True, exist_ok=True)
    h.paths.lock.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "fencing_token": fencing_token,
                "started_at": NOW.isoformat(),
                "controller_pid": 1234,
            }
        ),
        encoding="utf-8",
    )


def _child_reports_in(h) -> None:
    """Make the harness's spawned child complete the startup handshake.

    It learns which session and nonce it belongs to from its own argv, which is
    exactly how the real child will learn it -- the supervisor cannot hand it in
    any other way, because the process does not exist until it is spawned.
    """
    from engine.scheduler import SchedulerIdentity, SchedulerPaths, announce_ready

    inner = h.processes.spawn_detached
    paths = SchedulerPaths(root=h.paths.root)

    def spawn(args, *, env, cwd, log):
        pid = inner(args, env=env, cwd=cwd, log=log)
        rendered = [str(a) for a in args]
        for token in rendered:
            if token.startswith("--scheduler-session="):
                # paperday_support's fake records a fixed watcher command line
                # and ignores its args. The scheduler's whole identity check
                # reads the child's cmdline, so it has to be the real one.
                h.processes.table[pid] = " ".join(rendered)
                session_id, _, nonce = token.split("=", 1)[1].partition(":")
                announce_ready(
                    paths,
                    SchedulerIdentity(session_id=session_id, nonce=nonce),
                    now=NOW,
                )
        return pid

    h.processes.spawn_detached = spawn  # type: ignore[method-assign]


class TestADayWithoutASchedulerPolicyIsUnchanged:
    """A default cadence would make unattended trading an accident, not a choice."""

    def test_no_spec_means_no_scheduler_check_at_all(self, tmp_path: Path) -> None:
        h = harness(tmp_path)

        report = h.controller.start()

        assert report.state == READY, report.render()
        assert "scheduler" not in [check.name for check in report.checks]

    def test_no_spec_means_no_process_is_spawned_for_it(self, tmp_path: Path) -> None:
        h = harness(tmp_path)

        h.controller.start()

        assert not any(
            "run_scheduler" in " ".join(args) for args in h.processes.spawned
        ), h.processes.spawned


class TestStartingTheScheduler:
    """The nonce has to reach the child, or stop can never identify it again."""

    def test_a_spec_starts_a_scheduler_carrying_this_sessions_nonce(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        h.controller.scheduler = spec_for(tmp_path)
        h.controller.nonce_factory = lambda: NONCE
        _child_reports_in(h)

        report = h.controller.start()

        assert report.state == READY, report.render()
        spawned = [" ".join(args) for args in h.processes.spawned]
        assert any(f":{NONCE}" in line for line in spawned), spawned
        assert any(report.session_id in line for line in spawned), spawned

    def test_the_record_names_the_session_and_the_nonce(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.controller.scheduler = spec_for(tmp_path)
        h.controller.nonce_factory = lambda: NONCE
        _child_reports_in(h)

        report = h.controller.start()

        record = json.loads(
            SchedulerPaths(root=h.paths.root).pid.read_text(encoding="utf-8")
        )
        assert record["nonce"] == NONCE
        assert record["session_id"] == report.session_id

    def test_the_scheduler_starts_only_after_this_sessions_gate_is_published(
        self, tmp_path: Path
    ) -> None:
        """The child runs ``options-run --arm`` and its first tick reads whatever
        gate.json says at that instant. Started earlier it would read the
        PREVIOUS session's gate -- which may say OPEN -- while this session is
        still reconciling and proving the verifier. An armed child acting on a
        stale opening licence is the stale-gate race."""
        h = harness(tmp_path)
        h.controller.scheduler = spec_for(tmp_path)
        h.controller.nonce_factory = lambda: NONCE
        _child_reports_in(h)

        # Plant a PREVIOUS session's gate, wide open, exactly as a crash leaves it.
        h.paths.root.mkdir(parents=True, exist_ok=True)
        h.paths.gate.write_text(
            json.dumps(
                {"entry_gate": "OPEN", "state": "PAPER_DAY_READY",
                 "session_id": "paperday-YESTERDAY", "as_of": "2026-08-12T13:00:00+00:00"}
            ),
            encoding="utf-8",
        )

        seen: list[dict] = []
        inner = h.processes.spawn_detached

        def spawn(args, *, env, cwd, log):
            # Only the SCHEDULER's spawn. The builder watcher is spawned at
            # step 5, long before the gate is written, and capturing that one
            # would make this test pass or fail for the wrong reason.
            if any(str(a).startswith("--scheduler-session=") for a in args):
                seen.append(json.loads(h.paths.gate.read_text(encoding="utf-8")))
            return inner(args, env=env, cwd=cwd, log=log)

        h.processes.spawn_detached = spawn  # type: ignore[method-assign]

        report = h.controller.start()

        assert seen, "the scheduler was never spawned"
        gate_at_spawn = seen[0]
        assert gate_at_spawn["session_id"] == report.session_id, gate_at_spawn
        assert gate_at_spawn["session_id"] != "paperday-YESTERDAY", gate_at_spawn

    def test_a_scheduler_that_will_not_start_degrades_the_day(
        self, tmp_path: Path
    ) -> None:
        """The book is still trustworthy and every manual command still works.
        Refusing the whole day over an absent driver is the wrong trade."""
        h = harness(tmp_path)
        h.controller.scheduler = spec_for(tmp_path)
        h.processes.spawn_error = OSError("no python")

        report = h.controller.start()

        assert report.state == DEGRADED, report.render()
        assert report.state != "PAPER_DAY_BLOCKED", report.render()
        failed = [c for c in report.checks if c.name == "scheduler" and not c.ok]
        assert failed and "no python" in failed[0].detail, report.render()


class TestIdempotentRestart:
    """Re-running start on a healthy day is documented as safe. It has to be."""

    def test_a_second_start_adopts_the_running_scheduler(self, tmp_path: Path) -> None:
        """A fresh nonce per start would make adoption compare a new nonce against
        a child carrying the old one, fail the match, and report a perfectly
        healthy scheduler as a stranger while the day degraded."""
        h = harness(tmp_path)
        h.controller.scheduler = spec_for(tmp_path)
        nonces = iter(["first-nonce", "second-nonce"])
        h.controller.nonce_factory = lambda: next(nonces)
        _child_reports_in(h)

        first = h.controller.start()
        assert first.state == READY, first.render()
        spawned_after_first = len(h.processes.spawned)

        second = h.controller.start()

        assert second.state == READY, second.render()
        assert len(h.processes.spawned) == spawned_after_first, (
            "the running scheduler must be adopted, not duplicated"
        )
        record = json.loads(
            SchedulerPaths(root=h.paths.root).pid.read_text(encoding="utf-8")
        )
        assert record["nonce"] == "first-nonce", record

    def test_a_scheduler_from_a_different_session_is_not_adopted_on_restart(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        h.controller.scheduler = spec_for(tmp_path)
        h.controller.nonce_factory = lambda: NONCE
        _child_reports_in(h)
        paths = SchedulerPaths(root=h.paths.root)
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.pid.write_text(
            json.dumps(
                {"pid": 4242, "session_id": "paperday-SOMEONE-ELSE", "nonce": "zzzz"}
            ),
            encoding="utf-8",
        )

        report = h.controller.start()

        record = json.loads(paths.pid.read_text(encoding="utf-8"))
        assert record["session_id"] == report.session_id, record
        assert record["nonce"] == NONCE, record


class TestStoppingDrainsBeforeAnythingTransmits:
    """A tick already inside the engine is a pass racing stop's own cancels."""

    def _planted(self, h, tmp_path: Path, *, alive: bool) -> int:
        paths = SchedulerPaths(root=h.paths.root)
        paths.root.mkdir(parents=True, exist_ok=True)
        _write_lock(h, "paperday-x", "fence-x")
        identity = SchedulerIdentity(session_id="paperday-x", nonce=NONCE)
        pid = h.processes.add(f"python run_scheduler.py {identity.needle}")
        if not alive:
            h.processes.kill_silently(pid)
        paths.pid.write_text(
            json.dumps(
                {"pid": pid, "session_id": "paperday-x", "nonce": NONCE,
                 "needle": identity.needle}
            ),
            encoding="utf-8",
        )
        if not alive:
            # A dead PID is clean only when the child left a durable terminal
            # receipt; without this marker the crash-aware drain must be dirty.
            paths.terminal.write_text(
                json.dumps(
                    {
                        "v": 1,
                        "session_id": "paperday-x",
                        "nonce": NONCE,
                        "tick_id": "tick-1",
                        "outcome": "STOPPED_QUIESCED",
                        "at": NOW.isoformat(),
                        "clean_exit": True,
                    }
                ),
                encoding="utf-8",
            )
        return pid

    def test_the_gate_still_closes_first(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        self._planted(h, tmp_path, alive=False)

        report = h.controller.stop()

        assert _step_names(report)[0] == "entry gate", _step_names(report)

    def test_the_scheduler_is_drained_before_working_orders_are_cancelled(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        self._planted(h, tmp_path, alive=False)

        report = h.controller.stop()

        names = _step_names(report)
        assert "scheduler" in names, names
        assert names.index("scheduler") < names.index("working entry orders"), names

    def test_a_dead_scheduler_stops_cleanly(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        pid = self._planted(h, tmp_path, alive=False)

        report = h.controller.stop()

        assert pid not in h.processes.terminated
        assert not SchedulerPaths(root=h.paths.root).pid.exists()
        assert report.clean, report.render()

    def test_an_unidentifiable_record_still_gets_a_quiesce(self, tmp_path: Path) -> None:
        """Every later branch can return without stopping anything, and stop then
        goes on to cancel with --arm and release the lock. The scheduler we
        cannot identify is the one we most need to have asked to stop."""
        from engine.scheduler import quiesce_requested

        h = harness(tmp_path)
        paths = SchedulerPaths(root=h.paths.root)
        paths.root.mkdir(parents=True, exist_ok=True)
        pid = h.processes.add("chrome.exe")
        paths.pid.write_text(json.dumps({"pid": pid}), encoding="utf-8")

        h.controller.stop()

        assert quiesce_requested(paths), "a live tick must still be told to stop"

    def test_an_unidentifiable_record_terminates_nothing(self, tmp_path: Path) -> None:
        """A record naming no session or nonce describes a process we cannot
        prove is ours. Killing it on the strength of a number is the stale-PID
        failure with extra steps."""
        h = harness(tmp_path)
        paths = SchedulerPaths(root=h.paths.root)
        paths.root.mkdir(parents=True, exist_ok=True)
        pid = h.processes.add("chrome.exe")
        paths.pid.write_text(json.dumps({"pid": pid}), encoding="utf-8")

        report = h.controller.stop()

        assert pid not in h.processes.terminated
        assert h.processes.alive(pid)
        assert not report.clean, "an unidentifiable scheduler record is a dirty stop"

    def test_no_record_means_no_scheduler_step_and_a_clean_stop(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)

        report = h.controller.stop()

        assert "scheduler" not in _step_names(report), _step_names(report)
        assert report.clean, report.render()


class TestStopFailsClosedOnAnUnreadableLock:
    """Ownership checks must not turn the gate-closes-first rule into a fail-OPEN.

    The teardown checks below exist so an older stop cannot disarm a replacement
    session. Applied to the gate write as strict equality, they also made a
    *corrupt* lock abandon stop before it wrote the gate at all -- so a crash
    mid-lock-write left the entry gate wherever the day had it, possibly OPEN,
    which is exactly what closing the gate first exists to prevent.

    The distinction is between "we cannot prove who owns this" and "somebody
    else provably owns this". Closing the gate only ever refuses new entries, so
    it proceeds on the first; the risk-bearing steps still demand proof.
    """

    def _corrupt_lock(self, h) -> None:
        h.paths.root.mkdir(parents=True, exist_ok=True)
        h.paths.lock.write_text('{"session_id": "paperday-liv', encoding="utf-8")

    def test_a_truncated_lock_still_closes_the_entry_gate(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.paths.root.mkdir(parents=True, exist_ok=True)
        h.paths.gate.write_text(
            json.dumps(
                {"entry_gate": "OPEN", "state": "PAPER_DAY_READY",
                 "session_id": "paperday-live", "as_of": "2026-08-13T13:00:00+00:00"}
            ),
            encoding="utf-8",
        )
        self._corrupt_lock(h)

        report = h.controller.stop()

        gate = json.loads(h.paths.gate.read_text(encoding="utf-8"))
        assert gate["entry_gate"] == "CLOSED", (
            f"a corrupt lock left the gate {gate['entry_gate']} -- fail-open"
        )
        assert _step_names(report)[0] == "entry gate", _step_names(report)
        assert not report.clean, "ownership could not be proven, so the stop is dirty"

    def test_a_truncated_lock_cancels_nothing_and_releases_nothing(
        self, tmp_path: Path
    ) -> None:
        """Closing the gate is risk-reducing and proceeds. Everything that bears
        risk still needs positive proof of ownership."""
        h = harness(tmp_path)
        self._corrupt_lock(h)

        report = h.controller.stop()

        assert not any(call[0] == "options-cancel" for call in h.engine.calls), h.engine.calls
        assert h.paths.lock.exists(), "an unprovable lock must not be unlinked"
        assert not report.clean

    def test_the_fencing_token_distinguishes_a_reused_session_id(
        self, tmp_path: Path
    ) -> None:
        """What the token is FOR, and the only thing that proves it earns its place.

        Session ids are generated per start, so a replacement almost always has
        a different one and comparing ids alone appears sufficient. The token
        exists for the case where it is not -- a restart that lands on the same
        id. A mutation sweep on 2026-08-13 dropped the token from the ownership
        identity and no test noticed, meaning the CAS was comparing ids only.
        """
        h = harness(tmp_path)
        h.paths.root.mkdir(parents=True, exist_ok=True)
        _write_lock(h, "paperday-same-id", "fence-first")
        first = h.controller._stop_lock_identity(
            json.loads(h.paths.lock.read_text(encoding="utf-8"))
        )

        # Same session id, different lease. This IS a different session.
        _write_lock(h, "paperday-same-id", "fence-second")

        assert h.controller._taken_over_by_another_session(first) is True, (
            "a replacement that reused the session id was not detected as a "
            "takeover -- the fencing token is contributing nothing"
        )
        assert h.controller._stop_owns(first) is False

    def test_the_takeover_predicate_separates_unknown_from_someone_else(
        self, tmp_path: Path
    ) -> None:
        """The whole fix in one assertion set: only a readable, different
        session counts as a takeover."""
        h = harness(tmp_path)
        h.paths.root.mkdir(parents=True, exist_ok=True)
        _write_lock(h, "paperday-mine", "fence-mine")
        mine = h.controller._stop_lock_identity(
            json.loads(h.paths.lock.read_text(encoding="utf-8"))
        )

        assert h.controller._taken_over_by_another_session(mine) is False

        self._corrupt_lock(h)
        assert h.controller._taken_over_by_another_session(mine) is False, (
            "an unreadable lock is unknown ownership, not a takeover"
        )

        h.paths.lock.unlink()
        assert h.controller._taken_over_by_another_session(mine) is False, (
            "an absent lock is unknown ownership, not a takeover"
        )

        _write_lock(h, "paperday-theirs", "fence-theirs")
        assert h.controller._taken_over_by_another_session(mine) is True, (
            "a readable lock naming another session IS a takeover"
        )


class TestStopTeardownOwnership:
    """A stop from one lease must never tear down a replacement lease."""

    def test_replacement_before_scheduler_drain_survives(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        h = harness(tmp_path)
        _write_lock(h, "paperday-old", "fence-old")
        paths = SchedulerPaths(root=h.paths.root)
        pid = h.processes.add("python run_scheduler.py paperday-old:fence-old")
        paths.pid.write_text(
            json.dumps(
                {
                    "pid": pid,
                    "session_id": "paperday-old",
                    "nonce": NONCE,
                    "needle": "paperday-old:fence-old",
                }
            ),
            encoding="utf-8",
        )

        original = h.controller._stop_scheduler

        def replace_before_drain(report, now, expected):
            _write_lock(h, "paperday-new", "fence-new")
            original(report, now, expected)

        monkeypatch.setattr(h.controller, "_stop_scheduler", replace_before_drain)
        report = h.controller.stop()

        assert not report.clean, report.render()
        assert json.loads(h.paths.lock.read_text(encoding="utf-8"))["session_id"] == (
            "paperday-new"
        )
        assert h.processes.alive(pid), "replacement session must not kill the old survivor"
        assert not paths.quiesce.exists()
        assert not h.paths.last_shutdown.exists()
        assert not any(call[0] == "options-cancel" for call in h.engine.calls)

    def test_replacement_after_order_phase_is_not_cancelled_or_released(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        h = harness(tmp_path)
        _write_lock(h, "paperday-old", "fence-old")
        replacement_watcher = h.processes.add("python.exe tools/watch-for-claude-handoffs.py")

        original = h.controller._settle_handoffs

        def replace_after_orders(report, now):
            result = original(report, now)
            _write_lock(h, "paperday-new", "fence-new")
            h.paths.watcher_pid.write_text(
                json.dumps({"pid": replacement_watcher}), encoding="utf-8"
            )
            return result

        monkeypatch.setattr(h.controller, "_settle_handoffs", replace_after_orders)
        report = h.controller.stop()

        assert not report.clean, report.render()
        assert any(call[0] == "options-cancel" for call in h.engine.calls), (
            "the working-order fixture must reach the destructive cancel path"
        )
        assert h.processes.alive(replacement_watcher)
        assert h.paths.lock.exists()
        assert h.paths.watcher_pid.exists()
        assert not h.paths.last_shutdown.exists()

    def test_replacement_watcher_record_is_not_terminated(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The watcher-record CAS is independent of the session-lock CAS."""
        h = harness(tmp_path)
        _write_lock(h, "paperday-old", "fence-old")
        replacement_watcher = h.processes.add(WATCHER_CMD)
        original = h.controller._settle_handoffs

        def replace_watcher_record(report, now):
            result = original(report, now)
            h.paths.watcher_pid.write_text(
                json.dumps({"pid": replacement_watcher}), encoding="utf-8"
            )
            return result

        monkeypatch.setattr(h.controller, "_settle_handoffs", replace_watcher_record)
        report = h.controller.stop()

        assert not report.clean, report.render()
        assert h.processes.alive(replacement_watcher), (
            "a replacement watcher record must not be terminated"
        )
        assert json.loads(h.paths.watcher_pid.read_text(encoding="utf-8")) == {
            "pid": replacement_watcher
        }

    def test_replacement_before_working_order_cancel_is_not_cancelled(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The working-order CAS must protect the actual cancel call.

        The engine fixture includes a parseable strategy UUID. The race is
        injected after the working-order listing and immediately before the
        per-order ownership check, so removing that check reaches
        ``options-cancel`` and is observable.
        """
        h = harness(tmp_path)
        _write_lock(h, "paperday-old", "fence-old")
        original_run = h.engine.run
        replaced = False

        def run(args, **kwargs):
            nonlocal replaced
            result = original_run(args, **kwargs)
            if args[0] == "options-positions" and not replaced:
                replaced = True
                _write_lock(h, "paperday-new", "fence-new")
            return result

        monkeypatch.setattr(h.engine, "run", run)
        report = h.controller.stop()

        assert not report.clean, report.render()
        assert replaced, "the race must occur after listing working orders"
        assert not any(call[0] == "options-cancel" for call in h.engine.calls), (
            "replacement ownership must block the destructive cancel"
        )
        assert json.loads(h.paths.lock.read_text(encoding="utf-8"))["session_id"] == (
            "paperday-new"
        )
        assert not h.paths.last_shutdown.exists()

    def test_replacement_before_watcher_termination_is_not_killed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A matching watcher record must reach the termination ownership check."""
        h = harness(tmp_path, watcher_running=True)
        _write_lock(h, "paperday-old", "fence-old")
        expected_watcher = json.loads(h.paths.watcher_pid.read_text(encoding="utf-8"))
        watcher_pid = expected_watcher["pid"]
        original_cmdline = h.processes.cmdline
        replaced = False

        def cmdline(pid: int) -> str:
            nonlocal replaced
            value = original_cmdline(pid)
            if pid == watcher_pid and not replaced:
                replaced = True
                _write_lock(h, "paperday-new", "fence-new")
            return value

        monkeypatch.setattr(h.processes, "cmdline", cmdline)
        report = h.controller.stop()

        assert not report.clean, report.render()
        assert replaced, "the race must occur before watcher termination"
        assert h.processes.alive(watcher_pid), "replacement must not kill the watcher"
        assert json.loads(h.paths.lock.read_text(encoding="utf-8"))["session_id"] == (
            "paperday-new"
        )
        assert h.paths.watcher_pid.exists()
        assert not h.paths.last_shutdown.exists()

    def test_replacement_immediately_before_lock_release_is_not_unlinked(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The final lock identity comparison must be a real release boundary."""
        h = harness(tmp_path)
        _write_lock(h, "paperday-old", "fence-old")
        original_require = h.controller._require_stop_ownership
        original_stop_owns = h.controller._stop_owns
        armed = False

        def require(report, expected, phase):
            nonlocal armed
            if phase == "session release" and not armed:
                armed = True

                def replace_after_ownership_check(candidate):
                    result = original_stop_owns(candidate)
                    _write_lock(h, "paperday-new", "fence-new")
                    return result

                monkeypatch.setattr(
                    h.controller, "_stop_owns", replace_after_ownership_check
                )
            return original_require(report, expected, phase)

        monkeypatch.setattr(h.controller, "_require_stop_ownership", require)
        report = h.controller.stop()

        assert not report.clean, report.render()
        assert armed, "the release boundary was not reached"
        assert json.loads(h.paths.lock.read_text(encoding="utf-8"))["session_id"] == (
            "paperday-new"
        )
        assert not h.paths.last_shutdown.exists()

    def test_replacement_before_release_keeps_watcher_and_lock(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        h = harness(tmp_path)
        _write_lock(h, "paperday-old", "fence-old")
        replacement_watcher = h.processes.add("python.exe tools/watch-for-claude-handoffs.py")

        original = h.controller._stop_watcher

        def replace_before_release(report, expected, expected_record):
            original(report, expected, expected_record)
            _write_lock(h, "paperday-new", "fence-new")
            h.paths.watcher_pid.write_text(
                json.dumps({"pid": replacement_watcher}), encoding="utf-8"
            )

        monkeypatch.setattr(h.controller, "_stop_watcher", replace_before_release)
        report = h.controller.stop()

        assert not report.clean, report.render()
        assert h.processes.alive(replacement_watcher)
        assert json.loads(h.paths.lock.read_text(encoding="utf-8"))["fencing_token"] == (
            "fence-new"
        )
        assert h.paths.watcher_pid.exists()
        assert not h.paths.last_shutdown.exists()

    def test_replacement_during_last_shutdown_cas_keeps_new_session_state(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        h = harness(tmp_path)
        _write_lock(h, "paperday-old", "fence-old")
        original = h.controller._publish_last_shutdown

        def replace_before_marker(report, expected, payload):
            _write_lock(h, "paperday-new", "fence-new")
            return original(report, expected, payload)

        monkeypatch.setattr(h.controller, "_publish_last_shutdown", replace_before_marker)
        report = h.controller.stop()

        assert not report.clean, report.render()
        assert json.loads(h.paths.lock.read_text(encoding="utf-8"))["session_id"] == (
            "paperday-new"
        )
        assert not h.paths.last_shutdown.exists()
