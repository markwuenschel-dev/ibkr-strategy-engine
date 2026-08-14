from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from engine.errors import EXIT_OK, ConfigError
from engine.runtime import EngineCommandRunner
from engine.scheduler import (
    SchedulerIdentity,
    SchedulerPaths,
    TickOutcome,
    adopt_or_spawn,
)
from engine.scheduler_bootstrap import (
    MISSED_TICK_POLICY,
    POLICY_SCHEMA,
    build_scheduler_loop,
    build_scheduler_spec,
    load_scheduler_policy,
)
import engine.scheduler_main as scheduler_main
from scheduler_support import (
    FakeClock,
    FakeEngine,
    FakeProcesses,
    NOW,
    read_receipts,
    write_lock,
)


SESSION_ID = "paperday-20260813-bootstrap"
NONCE = "abc123nonce"


def policy_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": POLICY_SCHEMA,
        "mandate": "MANAGE_ONLY",
        "calendar": {
            "timezone": "America/New_York",
            "regular_open": "09:30",
            "regular_close": "16:00",
            "weekend_days": [5, 6],
            "holidays": ["2026-01-01", "2026-11-26"],
            "early_closes": [{"date": "2026-11-27", "close": "13:00"}],
        },
        "cadence_seconds": 60,
        "missed_tick_policy": MISSED_TICK_POLICY,
        "command": ["options-run", "--symbol", "SPY", "--arm"],
        "command_timeout_seconds": 30,
    }
    record.update(overrides)
    return record


def write_policy(tmp_path: Path, record: dict[str, Any] | list[Any] | str) -> tuple[Path, str]:
    path = tmp_path / "schedule.json"
    if isinstance(record, str):
        raw = record.encode("utf-8")
        path.write_bytes(raw)
    else:
        raw = json.dumps(record, sort_keys=True).encode("utf-8")
        path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def identity() -> SchedulerIdentity:
    return SchedulerIdentity(session_id=SESSION_ID, nonce=NONCE)


class TestSchedulerPolicyLoading:
    def test_happy_policy_constructs_explicit_calendar_and_loop(self, tmp_path: Path) -> None:
        config, digest = write_policy(tmp_path, policy_record())
        state_dir = tmp_path / "state"

        policy = load_scheduler_policy(config, digest)

        assert policy.missed_tick_policy == MISSED_TICK_POLICY

        loop = build_scheduler_loop(
            identity=identity(),
            state_dir=state_dir,
            schedule_config=config,
            schedule_config_sha256=digest,
        )

        assert loop.identity == identity()
        assert loop.paths == SchedulerPaths(root=state_dir / "paperday")
        assert loop.lock == state_dir / "paperday" / "session.lock"
        assert loop.cadence_seconds == 60.0
        assert loop.command == ("options-run", "--symbol", "SPY", "--arm")
        assert loop.command_timeout == 30.0
        assert isinstance(loop.engine, EngineCommandRunner)
        assert loop.is_open(dt.datetime(2026, 8, 13, 15, 0, tzinfo=dt.timezone.utc))
        assert not loop.is_open(dt.datetime(2026, 8, 15, 15, 0, tzinfo=dt.timezone.utc))

    def test_missing_policy_file_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="does not exist"):
            load_scheduler_policy(tmp_path / "missing.json", "0" * 64)

    def test_bad_digest_shape_fails_closed(self, tmp_path: Path) -> None:
        config, _digest = write_policy(tmp_path, policy_record())
        with pytest.raises(ConfigError, match="64-character hex"):
            load_scheduler_policy(config, "not-a-digest")

    def test_digest_mismatch_fails_closed(self, tmp_path: Path) -> None:
        config, _digest = write_policy(tmp_path, policy_record())
        with pytest.raises(ConfigError, match="digest mismatch"):
            load_scheduler_policy(config, "f" * 64)

    def test_malformed_json_fails_closed_after_digest_match(self, tmp_path: Path) -> None:
        config, digest = write_policy(tmp_path, '{"schema": ')
        with pytest.raises(ConfigError, match="malformed JSON"):
            load_scheduler_policy(config, digest)

    def test_non_object_json_fails_closed(self, tmp_path: Path) -> None:
        config, digest = write_policy(tmp_path, [])
        with pytest.raises(ConfigError, match="root must be a JSON object"):
            load_scheduler_policy(config, digest)

    def test_unknown_schema_fails_closed(self, tmp_path: Path) -> None:
        config, digest = write_policy(
            tmp_path, policy_record(schema="ibkr.scheduler_bootstrap/2")
        )
        with pytest.raises(ConfigError, match="unknown scheduler policy schema"):
            load_scheduler_policy(config, digest)

    @pytest.mark.parametrize(
        "missing",
        [
            "mandate",
            "cadence_seconds",
            "missed_tick_policy",
            "command",
            "command_timeout_seconds",
        ],
    )
    def test_missing_top_level_field_fails_closed(
        self, tmp_path: Path, missing: str
    ) -> None:
        record = policy_record()
        del record[missing]
        config, digest = write_policy(tmp_path, record)

        with pytest.raises(ConfigError, match="missing required field"):
            load_scheduler_policy(config, digest)

    def test_unsupported_missed_tick_policy_fails_closed(self, tmp_path: Path) -> None:
        config, digest = write_policy(
            tmp_path, policy_record(missed_tick_policy="CATCH_UP_MISSED_TICKS")
        )

        with pytest.raises(ConfigError, match="unsupported scheduler missed_tick_policy"):
            load_scheduler_policy(config, digest)

    @pytest.mark.parametrize("missing", ["timezone", "holidays", "early_closes"])
    def test_missing_calendar_field_fails_closed(
        self, tmp_path: Path, missing: str
    ) -> None:
        record = policy_record()
        del record["calendar"][missing]
        config, digest = write_policy(tmp_path, record)

        with pytest.raises(ConfigError, match="missing required field"):
            load_scheduler_policy(config, digest)

    def test_unknown_field_fails_closed_instead_of_being_ignored(
        self, tmp_path: Path
    ) -> None:
        config, digest = write_policy(tmp_path, policy_record(default_cadence=60))
        with pytest.raises(ConfigError, match="unknown field"):
            load_scheduler_policy(config, digest)

    @pytest.mark.parametrize(
        ("calendar_patch", "message"),
        [
            ({"timezone": "Mars/Olympus_Mons"}, "timezone"),
            ({"regular_open": "16:00", "regular_close": "09:30"}, "calendar data"),
            ({"weekend_days": [0, 1, 2, 3, 4, 5, 6]}, "calendar data"),
            ({"holidays": ["not-a-date"]}, "not an ISO date"),
            ({"early_closes": [{"date": "2026-11-27", "close": "09:30"}]}, "calendar data"),
        ],
    )
    def test_invalid_timezone_or_calendar_data_fails_closed(
        self, tmp_path: Path, calendar_patch: dict[str, Any], message: str
    ) -> None:
        record = policy_record()
        record["calendar"].update(calendar_patch)
        config, digest = write_policy(tmp_path, record)

        with pytest.raises(ConfigError, match=message):
            load_scheduler_policy(config, digest)

    @pytest.mark.parametrize("value", [0, -1, -0.5])
    def test_non_positive_cadence_fails_closed(
        self, tmp_path: Path, value: float
    ) -> None:
        config, digest = write_policy(tmp_path, policy_record(cadence_seconds=value))
        with pytest.raises(ConfigError, match="cadence_seconds must be positive"):
            load_scheduler_policy(config, digest)

    @pytest.mark.parametrize("command", [[], [""], ["trade", "--arm"]])
    def test_empty_or_wrong_command_fails_closed(
        self, tmp_path: Path, command: list[str]
    ) -> None:
        config, digest = write_policy(tmp_path, policy_record(command=command))
        with pytest.raises(ConfigError):
            load_scheduler_policy(config, digest)

    @pytest.mark.parametrize(
        ("patch", "message"),
        [
            ({"mandate": "TRADE"}, "MANAGE_ONLY"),
            ({"command": ["options-run", "--symbol", "SPY"]}, "must include --arm"),
            (
                {"command": ["options-run", "--symbol", "SPY", "--arm", "--enable-entry"]},
                "must not include --enable-entry",
            ),
        ],
    )
    def test_production_management_mandate_fails_closed(
        self, tmp_path: Path, patch: dict[str, Any], message: str
    ) -> None:
        config, digest = write_policy(tmp_path, policy_record(**patch))
        with pytest.raises(ConfigError, match=message):
            load_scheduler_policy(config, digest)


class TestSchedulerLoopFromBootstrap:
    def test_controller_spec_normalizes_relative_entry_paths_to_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, digest = write_policy(tmp_path, policy_record())
        entrypoint = tmp_path / "scheduler_main.py"
        entrypoint.write_text("# test entrypoint\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        spec = build_scheduler_spec(
            schedule_config=Path(config.name),
            schedule_config_sha256=digest,
            state_dir=Path("state"),
            entry_script=Path(entrypoint.name),
        )

        assert spec.entry_script == entrypoint.resolve()
        assert spec.entry_args == (
            f"--schedule-config={config.resolve()}",
            f"--schedule-config-sha256={digest}",
            f"--state-dir={(tmp_path / 'state').resolve()}",
        )

    def test_controller_spec_uses_the_pinned_policy_and_real_entrypoint(
        self, tmp_path: Path
    ) -> None:
        config, digest = write_policy(tmp_path, policy_record())
        entrypoint = tmp_path / "scheduler_main.py"
        entrypoint.write_text("# test entrypoint\n", encoding="utf-8")

        spec = build_scheduler_spec(
            schedule_config=config,
            schedule_config_sha256=digest,
            state_dir=tmp_path / "state",
            entry_script=entrypoint,
        )

        assert spec.cadence_seconds == 60.0
        assert spec.command == ("options-run", "--symbol", "SPY", "--arm")
        assert spec.entry_script == entrypoint
        assert spec.entry_args == (
            f"--schedule-config={config}",
            f"--schedule-config-sha256={digest}",
            f"--state-dir={tmp_path / 'state'}",
        )

    def test_max_ticks_loop_runs_with_fakes_and_no_broker(self, tmp_path: Path) -> None:
        config, digest = write_policy(tmp_path, policy_record())
        state_dir = tmp_path / "state"
        paths = SchedulerPaths(root=state_dir / "paperday")
        write_lock(paths, SESSION_ID)
        clock = FakeClock(start=NOW)
        engine = FakeEngine()

        loop = build_scheduler_loop(
            identity=identity(),
            state_dir=state_dir,
            schedule_config=config,
            schedule_config_sha256=digest,
            engine=engine,
            clock=clock,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        receipts = loop.run(max_ticks=2)

        assert [call for call in engine.calls] == [
            ["options-run", "--symbol", "SPY", "--arm"],
            ["options-run", "--symbol", "SPY", "--arm"],
        ]
        assert [receipt.outcome for receipt in receipts] == [
            TickOutcome.RAN,
            TickOutcome.RAN,
            TickOutcome.STOPPED_TICK_BUDGET,
        ]
        assert clock.slept == [60.0, 60.0]
        assert [record["outcome"] for record in read_receipts(paths)] == [
            "RAN",
            "RAN",
            "STOPPED_TICK_BUDGET",
        ]


class TestSchedulerMain:
    def test_main_accepts_explicit_args_and_runs_the_constructed_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, digest = write_policy(tmp_path, policy_record())
        state_dir = tmp_path / "state"
        ran: list[bool] = []
        seen: dict[str, Any] = {}

        class FakeLoop:
            def run(self) -> None:
                ran.append(True)

        def fake_build_scheduler_loop(**kwargs: Any) -> FakeLoop:
            seen.update(kwargs)
            return FakeLoop()

        monkeypatch.setattr(
            scheduler_main, "build_scheduler_loop", fake_build_scheduler_loop
        )

        code = scheduler_main.main(
            [
                f"--scheduler-session={SESSION_ID}:{NONCE}",
                f"--schedule-config={config}",
                f"--schedule-config-sha256={digest}",
                f"--state-dir={state_dir}",
            ]
        )

        assert code == EXIT_OK
        assert ran == [True]
        assert seen["identity"] == identity()
        assert seen["schedule_config"] == config
        assert seen["schedule_config_sha256"] == digest
        assert seen["state_dir"] == state_dir

    def test_main_accepts_the_exact_supervisor_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config, digest = write_policy(tmp_path, policy_record())
        state_dir = tmp_path / "state"
        entrypoint = tmp_path / "scheduler_main.py"
        entrypoint.write_text("# fixture\n", encoding="utf-8")
        spec = build_scheduler_spec(
            schedule_config=config,
            schedule_config_sha256=digest,
            state_dir=state_dir,
            entry_script=entrypoint,
        )
        ran: list[bool] = []

        class FakeLoop:
            cadence_seconds = spec.cadence_seconds
            command = spec.command

            def run(self) -> None:
                ran.append(True)

        monkeypatch.setattr(
            scheduler_main,
            "build_scheduler_loop",
            lambda **_kwargs: FakeLoop(),
        )
        scheduler_paths = SchedulerPaths(root=state_dir / "paperday")
        write_lock(scheduler_paths)
        clock = FakeClock()
        processes = FakeProcesses(announce_paths=scheduler_paths)
        pid, _detail = adopt_or_spawn(
            processes=processes,
            paths=scheduler_paths,
            identity=identity(),
            spec=spec,
            cwd=tmp_path,
            env={},
            clock=clock,
            sleep=clock.sleep,
            python="python",
            monotonic=clock.monotonic,
            ready_timeout=2.0,
            ready_poll=0.5,
        )
        assert pid is not None
        spawned = processes.spawned[0]
        assert spawned[:2] == ["python", str(spec.entry_script)]
        # This is the exact suffix assembled by adopt_or_spawn, including the
        # supervisor envelope separator and policy-derived worker command.
        argv = spawned[2:]

        assert scheduler_main.main(argv) == EXIT_OK
        assert ran == [True]

        ran.clear()
        monkeypatch.setattr(scheduler_main.sys, "argv", ["engine-scheduler", *argv])
        assert scheduler_main.main() == EXIT_OK
        assert ran == [True]
