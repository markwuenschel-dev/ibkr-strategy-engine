"""The stop-side operational scenario matrix for the paper-day controller.

Each class is one way a trading day actually ends -- cleanly, twice, with work
in flight, after a crash, with a dead watcher, with yesterday's leftovers, with
a silent reviewer, with a working order still on the wire. The exemplar happy
path and the enforced gate live in ``test_paperday.py``; this file pins what
``stop()`` (and the start-side leftover audit) must do when the day is messy.

One ordering property threads through several cases: ``stop()`` writes the
entry gate CLOSED as its *first* step, so no later failure -- a silent
reviewer, a refused cancel -- can leave a standing licence to open.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from engine.paperday import (
    EXIT_STOP_DIRTY,
    EngineCommandResult,
    GATE_CLOSED,
    GATE_OPEN,
    READY,
    STOPPED,
    read_gate,
    write_gate,
)
from paperday_support import NOW, harness


class TestCleanStopAfterHealthyStart:
    """The baseline: a healthy day must shut down completely.

    Prevents the failure where stop 'succeeds' but leaves a licence behind --
    an OPEN gate, a standing lock, or a live watcher that keeps answering
    handoffs after the operator believes the day is over.
    """

    def test_stop_closes_everything_it_started(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        start = h.controller.start()
        assert start.state == READY, start.render()

        stop = h.controller.stop()
        assert stop.clean, stop.render()
        assert stop.exit_code == 0

        gate = read_gate(h.paths)
        assert gate is not None
        assert gate["entry_gate"] == GATE_CLOSED
        assert gate["state"] == STOPPED

        assert not h.paths.lock.exists()
        assert start.watcher_pid in h.processes.terminated

        shutdown = json.loads(h.paths.last_shutdown.read_text(encoding="utf-8"))
        assert shutdown["clean"] is True
        assert shutdown["session_id"] == start.session_id

    def test_the_summary_names_the_session(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        start = h.controller.start()
        h.controller.stop()
        summaries = list(h.paths.summaries.glob("*.md"))
        assert summaries, "no session summary was written"
        assert any(
            start.session_id in path.read_text(encoding="utf-8") for path in summaries
        )


class TestRepeatedStop:
    """Stopping an already-stopped day must be a safe no-op.

    Prevents the failure where a nervous operator's second stop-paper-day
    raises, or -- worse -- terminates a PID that was recycled to some other
    process after the first stop already killed the watcher.
    """

    def test_second_stop_is_clean_and_terminates_nothing_new(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        h.controller.start()
        first = h.controller.stop()
        assert first.clean, first.render()
        terminated_after_first = list(h.processes.terminated)

        second = h.controller.stop()
        assert second.clean, second.render()
        assert second.exit_code == 0
        assert any("no active session" in step.detail for step in second.steps)
        assert h.processes.terminated == terminated_after_first

        gate = read_gate(h.paths)
        assert gate is not None
        assert gate["entry_gate"] == GATE_CLOSED
        assert gate["state"] == STOPPED


class TestPendingReviewDuringShutdown:
    """An unanswered verification proposal must not survive the day.

    Prevents the failure where a proposal filed near the close sits pending
    overnight and a reviewer answers it tomorrow -- an approval issued against
    a book that has since moved, waiting to be consumed.
    """

    def test_open_proposal_is_closed_session_closed(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.controller.start()
        proposal = h.store.create(
            to="reviewer",
            sender="builder",
            title="VERIFY OPEN: test",
            body="pending",
            tags=["verification", "opening"],
        )

        report = h.controller.stop()
        assert report.clean, report.render()

        settled = h.store.find(proposal.id)
        assert settled.status == "done"
        assert "SESSION_CLOSED" in (settled.note or "")


class TestCrashRecoveryStop:
    """stop() after a crash must clean up without trusting stale records.

    Prevents two failures at once: the standing armed licence (an OPEN gate
    plus a lock that nobody owns), and the stale-PID kill -- terminating
    whatever process the OS handed the dead watcher's number to.
    """

    def test_stop_clears_a_crashed_session_without_killing_strangers(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        # A crashed session on disk: gate OPEN, lock present, watcher pid dead.
        write_gate(
            h.paths,
            entry_gate=GATE_OPEN,
            state=READY,
            session_id="crashed-session",
            now=NOW,
        )
        h.paths.lock.write_text(
            json.dumps(
                {
                    "session_id": "crashed-session",
                    "started_at": NOW.isoformat(),
                    "controller_pid": 1,
                }
            ),
            encoding="utf-8",
        )
        dead_pid = 4242  # never added to the fake process table
        h.paths.watcher_pid.write_text(
            json.dumps({"pid": dead_pid, "started_at": NOW.isoformat()}),
            encoding="utf-8",
        )

        report = h.controller.stop()

        assert report.clean, report.render()
        assert h.processes.terminated == []
        assert not h.paths.lock.exists()
        gate = read_gate(h.paths)
        assert gate is not None
        assert gate["entry_gate"] == GATE_CLOSED
        assert gate["state"] == STOPPED
        assert gate["session_id"] == "crashed-session"


class TestWatcherDiesMidSession:
    """status() must report a dead watcher, not narrate the pid file.

    Prevents the failure where the operator reads 'pid 9001' off a status
    screen and believes handoffs are being answered, when the process behind
    that number died an hour ago (or was reused by something else entirely).
    """

    def test_status_shows_the_dead_watcher(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        start = h.controller.start()
        assert start.watcher_pid is not None
        h.processes.kill_silently(start.watcher_pid)

        rows = dict(h.controller.status().rows)
        assert "DEAD" in rows["claude watcher"]

    def test_grok_watcher_row_tracks_reviewer_presence(self, tmp_path: Path) -> None:
        running = harness(tmp_path / "running")
        rows = dict(running.controller.status().rows)
        assert "HEALTHY" in rows["grok watcher"]
        assert "pids" in rows["grok watcher"]

        absent = harness(tmp_path / "absent", reviewer_running=False)
        rows = dict(absent.controller.status().rows)
        assert "not detected" in rows["grok watcher"]


class TestLeftoverApprovalAtStart:
    """Yesterday's ledger must be audited, not silently inherited.

    Prevents two failures: an expired PROPOSED record staying live-looking on
    disk, and an unexpired approval carrying over *unannounced* -- the operator
    must see it named, even though the digest binding (not this audit) is what
    keeps it from authorizing anything else.
    """

    def test_expired_marked_unexpired_named_and_day_still_ready(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        requests = h.paths.verification_ledger / "requests"
        requests.mkdir(parents=True)
        expired_path = requests / ("a" * 64 + ".json")
        expired_path.write_text(
            json.dumps(
                {
                    "state": "PROPOSED",
                    "spec_digest": "a" * 64,
                    "expires_at": (NOW - dt.timedelta(hours=13)).isoformat(),
                    "spec": {},
                }
            ),
            encoding="utf-8",
        )
        fresh_path = requests / ("b" * 64 + ".json")
        fresh_path.write_text(
            json.dumps(
                {
                    "state": "PROPOSED",
                    "spec_digest": "b" * 64,
                    "expires_at": (NOW + dt.timedelta(hours=2)).isoformat(),
                    "spec": {},
                }
            ),
            encoding="utf-8",
        )

        report = h.controller.start()

        marked = json.loads(expired_path.read_text(encoding="utf-8"))
        assert marked["state"] == "EXPIRED"

        ledger = next(c for c in report.checks if c.name == "approval ledger")
        assert "b" * 12 in ledger.detail

        # Leftovers inform; they do not block. The digest binding enforces.
        assert report.state == READY, report.render()


class TestExpiredProposalHandoffAtStart:
    """A verification proposal left pending overnight must expire at start.

    Prevents the failure where yesterday's unanswered proposal is answered
    this morning and read as a fresh approval -- a stale packet must require a
    fresh one, explicitly, in the handoff's own record.
    """

    def test_stale_proposal_is_completed_expired(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        stale = h.store.create(
            to="reviewer",
            sender="builder",
            title="VERIFY OPEN: stale from yesterday",
            body="unanswered proposal",
            tags=["verification", "opening"],
        )

        report = h.controller.start()
        assert report.state == READY, report.render()

        after = h.store.find(stale.id)
        assert after.status == "done"
        assert "EXPIRED" in (after.note or "")


class TestReviewerSilentAtStop:
    """A silent reviewer must dirty the stop, never block it -- and the gate
    must already be CLOSED when the silence is discovered.

    Prevents two failures: a stop that hangs (or aborts) because the reviewer
    is gone, and the subtler one where a stop that fails partway leaves the
    gate in whatever position the day had -- this is the ordering proof that
    the gate closes *first*.
    """

    def test_stop_completes_dirty_with_the_gate_already_closed(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path, reviewer_answers=False, liveness_timeout=0.05)

        report = h.controller.stop()

        assert not report.clean, report.render()
        assert report.exit_code == EXIT_STOP_DIRTY
        failed = [step.name for step in report.steps if not step.ok]
        assert failed == ["reviewer shutdown"]

        # The gate closed as step one, before the reviewer silence was found.
        assert report.steps[0].name == "entry gate"
        gate = read_gate(h.paths)
        assert gate is not None
        assert gate["entry_gate"] == GATE_CLOSED
        assert gate["state"] == STOPPED

        shutdown = json.loads(h.paths.last_shutdown.read_text(encoding="utf-8"))
        assert shutdown["clean"] is False


class TestWorkingEntryOrderAtStop:
    """A working entry order on the wire must be cancelled, armed, at stop.

    Prevents the failure where the session ends, everyone goes home, and a
    resting limit order fills overnight into a book nobody is managing --
    the cancel must actually transmit (--arm), not dry-run.
    """

    def test_working_entry_is_cancelled_armed(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        strategy_id = "123e4567-e89b-12d3-a456-426614174000"
        h.engine.results["options-positions"] = EngineCommandResult(
            0,
            "reconciled: 1 open position(s), broker agrees\n"
            f"  working entry order {strategy_id}",
        )

        report = h.controller.stop()
        assert report.clean, report.render()

        cancels = [call for call in h.engine.calls if call[0] == "options-cancel"]
        assert len(cancels) == 1
        cancel = cancels[0]
        assert "--strategy-id" in cancel
        assert cancel[cancel.index("--strategy-id") + 1] == strategy_id
        assert "--arm" in cancel
