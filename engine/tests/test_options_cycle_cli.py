from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.cli import build_parser, cmd_options_cycle
from engine.cycle_adapter import _identity
from engine.errors import ConfigError


def test_options_cycle_is_a_registered_persistent_command():
    args = build_parser().parse_args(
        [
            "--account",
            "DU123",
            "--state-dir",
            str(Path.cwd() / "state"),
            "options-cycle",
            "--schedule-config",
            "C:/policy.json",
            "--schedule-config-sha256",
            "a" * 64,
            "--scheduler-session",
            "paperday-1:nonce-1",
            "--arm",
            "--max-cycles",
            "1",
        ]
    )
    assert args.command == "options-cycle"
    assert args.schedule_config == "C:/policy.json"
    assert args.schedule_config_sha256 == "a" * 64
    assert args.scheduler_session == "paperday-1:nonce-1"
    assert args.arm is True
    assert args.max_cycles == 1


def test_subcommand_state_dir_does_not_overwrite_global_state_dir(monkeypatch):
    args = build_parser().parse_args(
        [
            "--state-dir",
            "C:/global-state",
            "options-cycle",
            "--state-dir",
            "C:/worker-state",
            "--schedule-config",
            "C:/policy.json",
            "--schedule-config-sha256",
            "b" * 64,
        ]
    )
    assert args.state_dir == "C:/worker-state"


def test_options_cycle_handler_delegates_to_application_adapter(monkeypatch):
    import engine.cycle_adapter as adapter
    import engine.cli as cli

    args = build_parser().parse_args(
        [
            "--account",
            "DU123",
            "options-cycle",
            "--schedule-config",
            "C:/policy.json",
            "--schedule-config-sha256",
            "c" * 64,
        ]
    )
    sentinel = object()
    monkeypatch.setattr(cli, "config_from", lambda received: sentinel)
    observed = {}

    def fake_run(received, *, config, broker_factory):
        observed["args"] = received
        observed["config"] = config
        observed["broker_factory"] = broker_factory
        return 37

    monkeypatch.setattr(adapter, "run_options_cycle", fake_run)
    assert cmd_options_cycle(args) == 37
    assert observed["args"] is args
    assert observed["config"] is sentinel


@pytest.mark.parametrize(
    "supplied",
    ["replacement-session:live-nonce", "live-session:replacement-nonce"],
)
def test_cycle_identity_rejects_identity_different_from_live_lease(
    tmp_path: Path, supplied: str
):
    paperday = tmp_path / "paperday"
    paperday.mkdir()
    (paperday / "session.lock").write_text(
        json.dumps({"session_id": "live-session", "fencing_token": "live-nonce"}),
        encoding="utf-8",
    )
    (paperday / "scheduler.pid").write_text(
        json.dumps({"session_id": "live-session", "nonce": "live-nonce", "pid": 1234}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="does not match live paper-day lease"):
        _identity(tmp_path, SimpleNamespace(scheduler_session=supplied))
