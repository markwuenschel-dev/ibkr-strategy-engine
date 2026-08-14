from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.cli import build_parser, cmd_options_cycle
from engine.cycle_adapter import _CycleRuntime, _identity
from engine.errors import ConfigError
from engine.options import order_outbox


def test_cycle_adapter_execution_outbox_is_the_order_outbox_class():
    """Pin the class the corridor actually writes to, not the unwired saga tracker.

    ``engine.options.execution_outbox.ExecutionOutbox`` has a different method
    surface (``record_approval_consumed``, ``unresolved``, no ``assert_clear``)
    from ``engine.options.order_outbox.ExecutionOutbox`` (``assert_clear``,
    ``prepare``, ``approval_consumed``, ``blocking_records``) -- the one
    ``authorize_open``/``run_once`` actually call. Importing the wrong one made
    every real ``options-cycle`` entry pass crash with ``AttributeError`` the
    moment it reached a real candidate.
    """
    import engine.cycle_adapter as adapter

    assert adapter.ExecutionOutbox is order_outbox.ExecutionOutbox
    for method in ("assert_clear", "prepare", "approval_consumed", "blocking_records"):
        assert hasattr(adapter.ExecutionOutbox, method), method


def test_cycle_runtime_reconcile_blocks_on_a_real_blocking_outbox_record(
    tmp_path: Path,
):
    """``reconcile()`` must see the same outbox ``entry()`` writes to.

    Before the fix, ``reconcile()`` checked an ``ExecutionOutbox`` instance
    that nothing ever wrote to, so this recovery gate was silently a no-op.
    """
    outbox = order_outbox.ExecutionOutbox(tmp_path / "execution-outbox")
    intent = SimpleNamespace(
        strategy_id="11111111-1111-1111-1111-111111111111",
        strategy_action=SimpleNamespace(value="OPEN"),
        quantity=1,
        underlying="SPY",
        legs=[],
    )
    attempt_id = outbox.prepare(
        intent,
        structure_digest="d" * 64,
        spec_digest="e" * 64,
        account="DU123",
    )

    runtime = _CycleRuntime.__new__(_CycleRuntime)
    runtime.execution_outbox = outbox

    result = _CycleRuntime.reconcile(runtime, None)

    assert result["outcome"] == "RECOVERY_REQUIRED"
    assert result["failure_code"] == "FAIL-BROKER-AMBIGUOUS"
    assert result["unresolved_execution_sagas"] == [attempt_id]


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
