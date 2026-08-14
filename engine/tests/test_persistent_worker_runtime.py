"""Focused contract tests for the persistent options-cycle subprocess seam."""

from __future__ import annotations

from pathlib import Path

from engine import runtime


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
    runtime.EngineCommandRunner(tmp_path).run(["options-cycle", "--policy"], timeout=None)

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
