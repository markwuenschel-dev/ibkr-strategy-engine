"""Core paper-day controller behavior: the exemplar cases.

The operational scenario matrix lives in ``test_paperday_start_scenarios.py``
and ``test_paperday_stop_scenarios.py``; this file pins the happy path, the
hard blocks, the enforced entry gate, and the real consumption-mechanics proof.
"""

from __future__ import annotations

from pathlib import Path

from engine.paperday import (
    BLOCKED,
    GATE_CLOSED,
    GATE_OPEN,
    GATE_PROOF_ONLY,
    READY,
    _consumption_mechanics_proof,
    entry_gate_preflight,
    read_gate,
    write_gate,
)
from paperday_support import NOW, harness


class TestStartHappyPath:
    def test_everything_healthy_is_ready_and_opens_the_gate(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        report = h.controller.start()
        assert report.state == READY, report.render()
        gate = read_gate(h.paths)
        assert gate is not None and gate["entry_gate"] == GATE_OPEN
        assert h.paths.lock.exists()
        assert report.watcher_pid is not None
        assert h.processes.alive(report.watcher_pid)

    def test_the_liveness_roundtrip_ran_the_real_lifecycle(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.controller.start()
        done = h.store.list(("done",), to="builder")
        assert any("REVIEWER_READY" in str(x.body or "") for x in done)
        # And the request itself was claimed by the reviewer then closed.
        answered = h.store.list(("done",), sender="builder", tag="handshake")
        assert answered and all(x.claimed_by == "reviewer" for x in answered)


class TestStartBlocks:
    def test_broker_unreachable_blocks_and_closes_the_gate(self, tmp_path: Path) -> None:
        h = harness(tmp_path, broker_up=False)
        report = h.controller.start()
        assert report.state == BLOCKED, report.render()
        gate = read_gate(h.paths)
        assert gate is not None and gate["entry_gate"] == GATE_CLOSED


class TestEntryGateEnforcement:
    """The preflight is the code that makes gate.json more than a status file."""

    def test_closed_gate_refuses_even_unarmed(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        write_gate(h.paths, entry_gate=GATE_CLOSED, state=BLOCKED,
                   session_id="s", now=NOW)
        preflight = entry_gate_preflight(h.paths)
        assert preflight(armed=False) is not None
        assert preflight(armed=True) is not None

    def test_proof_only_gate_allows_unarmed_refuses_armed(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        write_gate(h.paths, entry_gate=GATE_PROOF_ONLY, state="PAPER_DAY_DEGRADED",
                   session_id="s", now=NOW)
        preflight = entry_gate_preflight(h.paths)
        assert preflight(armed=False) is None
        refusal = preflight(armed=True)
        assert refusal is not None and "PAPER_DAY_READY" in refusal

    def test_no_gate_file_refuses_only_armed(self, tmp_path: Path) -> None:
        preflight = entry_gate_preflight(harness(tmp_path).paths)
        assert preflight(armed=False) is None
        assert preflight(armed=True) is not None

    def test_open_gate_without_lock_refuses_armed(self, tmp_path: Path) -> None:
        """A crashed session must not leave a standing armed licence."""
        h = harness(tmp_path)
        write_gate(h.paths, entry_gate=GATE_OPEN, state=READY, session_id="s", now=NOW)
        preflight = entry_gate_preflight(h.paths)
        assert preflight(armed=False) is None
        refusal = preflight(armed=True)
        assert refusal is not None and "crashed" in refusal


class TestConsumptionMechanicsProof:
    def test_the_real_proof_passes_on_the_shipped_gate(self) -> None:
        ok, detail = _consumption_mechanics_proof()
        assert ok, detail
        assert "reuse REFUSED" in detail


class TestStopNeverKillsAStranger:
    """A PID is a name the OS reuses. Stopping "the watcher" by number alone
    would kill whatever inherited that number -- found unpinned by a mutation
    sweep on 2026-08-01 (disabling the cmdline check left the suite green)."""

    def test_a_reused_pid_is_not_terminated_at_stop(self, tmp_path: Path) -> None:
        import json

        h = harness(tmp_path)
        stranger = h.processes.add("chrome.exe --renderer")
        h.paths.root.mkdir(parents=True, exist_ok=True)
        h.paths.watcher_pid.write_text(
            json.dumps({"pid": stranger, "started_at": NOW.isoformat()}),
            encoding="utf-8",
        )
        report = h.controller.stop()
        assert stranger not in h.processes.terminated, report.render()
        assert h.processes.alive(stranger)
        assert not h.paths.watcher_pid.exists(), "the stale record must still be discarded"
