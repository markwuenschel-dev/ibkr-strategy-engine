from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from engine.autotrader_policy import (
    ARMED,
    AUTOTRADER_POLICY_SCHEMA,
    FULL,
    MANAGE_ONLY,
    REVIEW_ONLY,
    SHADOW,
    load_autotrader_policy,
    parse_autotrader_policy,
)
from engine.errors import ConfigError
from engine.scheduler import SchedulerIdentity
from engine.scheduler_bootstrap import build_scheduler_loop, build_scheduler_spec


def policy_record(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": AUTOTRADER_POLICY_SCHEMA,
        "mandate": FULL,
        "mode": SHADOW,
        "calendar": {
            "timezone": "America/New_York",
            "regular_open": "09:30",
            "regular_close": "16:00",
            "weekend_days": [5, 6],
            "holidays": ["2026-01-01"],
            "early_closes": [{"date": "2026-11-27", "close": "13:00"}],
        },
        "cadences": {
            "management_seconds": 300,
            "discovery_seconds": 1800,
            "probe_seconds": 600,
            "entry_seconds": 300,
        },
        "missed_tick_policy": "SKIP_MISSED_TICKS",
        "windows": {
            "management": {"kind": "SESSION"},
            "breadth_discovery": {"kind": "PRE_OPEN"},
            "candidate_probe": {
                "kind": "SESSION_RELATIVE",
                "start": "OPEN",
                "end": "CLOSE_MINUS",
                "minutes_before_close": 15,
            },
            "entry": {"kind": "WALL_CLOCK", "start": "10:00", "end": "15:00"},
        },
        "worker_command": ["options-cycle", "--arm"],
        "command_timeout_seconds": 300,
        "state_dir": str(tmp_path / "state"),
        "catalog": {
            "path": str(tmp_path / "catalog.json"),
            "version": "seed-80-v1",
            "sha256": "a" * 64,
        },
        "discovery": {
            "refresh_limit": 100,
            "phase2_limit": 5,
            "coverage_sla_seconds": 86400,
        },
        "entry": {
            "max_pending_entries": 3,
            "max_new_openings_per_pass": 1,
            "transmission_limit_per_session": 25,
            "review_ttl_seconds": 1800,
            "packet_ttl_seconds": 900,
        },
        "pacing_reserve": {
            "management_fraction": 0.25,
            "discovery_fraction": 0.50,
            "minimum_management_requests": 1,
        },
    }
    for key, value in overrides.items():
        record[key] = value
    return record


def write_policy(tmp_path: Path, record: dict[str, Any]) -> tuple[Path, str]:
    path = tmp_path / "autotrader.json"
    raw = json.dumps(record, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


class TestAutotraderPolicy:
    def test_loads_hash_pinned_policy_and_exposes_all_four_cadences(self, tmp_path: Path) -> None:
        path, digest = write_policy(tmp_path, policy_record(tmp_path))

        policy = load_autotrader_policy(path, digest)

        assert policy.policy_hash == digest
        assert policy.cadences.management_seconds == 300
        assert policy.cadences.discovery_seconds == 1800
        assert policy.cadences.probe_seconds == 600
        assert policy.cadences.entry_seconds == 300
        assert policy.windows["entry"].start == "10:00"
        assert policy.discovery.phase2_limit == 5
        assert policy.entry.max_new_openings_per_pass == 1
        assert policy.catalog.sha256 == "a" * 64
        assert not policy.entry_enabled

    def test_digest_mismatch_fails_before_policy_parsing(self, tmp_path: Path) -> None:
        path, _digest = write_policy(tmp_path, policy_record(tmp_path))

        with pytest.raises(ConfigError, match="digest mismatch"):
            load_autotrader_policy(path, "b" * 64)

    @pytest.mark.parametrize("missing", ["mode", "cadences", "windows", "catalog", "pacing_reserve"])
    def test_missing_top_level_field_is_not_defaulted(self, tmp_path: Path, missing: str) -> None:
        record = policy_record(tmp_path)
        del record[missing]

        with pytest.raises(ConfigError, match="missing required field"):
            parse_autotrader_policy(record)

    def test_unknown_field_is_rejected(self, tmp_path: Path) -> None:
        record = policy_record(tmp_path, operator_default="guess")

        with pytest.raises(ConfigError, match="unknown field"):
            parse_autotrader_policy(record)

    @pytest.mark.parametrize("mode", [REVIEW_ONLY, ARMED])
    def test_entry_modes_require_full_mandate(self, tmp_path: Path, mode: str) -> None:
        record = policy_record(tmp_path, mandate=MANAGE_ONLY, mode=mode)

        with pytest.raises(ConfigError, match="requires mandate FULL"):
            parse_autotrader_policy(record)

    def test_armed_requires_arm_inside_pinned_worker_command(self, tmp_path: Path) -> None:
        record = policy_record(tmp_path, mode=ARMED)
        record["worker_command"] = ["options-cycle"]

        with pytest.raises(ConfigError, match="requires --arm"):
            parse_autotrader_policy(record)

    @pytest.mark.parametrize(
        "token",
        [
            "--mode",
            "--mandate",
            "--enable-entry",
            "--policy-hash",
            "--schedule-config",
            "--schedule-config-sha256",
            "--state-dir",
            "--schedule-config=/tmp/other.json",
            "--schedule-config-sha256=deadbeef",
            "--state-dir=/tmp/other-state",
            "--scheduler-session",
            "--scheduler-session=foreign-session:foreign-nonce",
        ],
    )
    def test_policy_cannot_be_overridden_by_worker_cli(self, tmp_path: Path, token: str) -> None:
        record = policy_record(tmp_path)
        record["worker_command"] = ["options-cycle", token, "ARMED"]

        with pytest.raises(ConfigError, match="cannot override policy"):
            parse_autotrader_policy(record)

    def test_non_absolute_state_or_catalog_path_is_rejected(self, tmp_path: Path) -> None:
        record = policy_record(tmp_path, state_dir="relative-state")

        with pytest.raises(ConfigError, match="state_dir must be absolute"):
            parse_autotrader_policy(record)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("management_seconds", 0),
            ("discovery_seconds", -1),
            ("probe_seconds", 0),
            ("entry_seconds", 0),
        ],
    )
    def test_non_positive_cadence_is_rejected(
        self, tmp_path: Path, field: str, value: int
    ) -> None:
        record = policy_record(tmp_path)
        record["cadences"][field] = value

        with pytest.raises(ConfigError, match="positive"):
            parse_autotrader_policy(record)

    def test_fingerprint_is_json_safe_and_contains_catalog_identity(self, tmp_path: Path) -> None:
        policy = parse_autotrader_policy(policy_record(tmp_path), policy_hash="c" * 64)

        encoded = json.dumps(policy.fingerprint_record(), sort_keys=True)

        assert "seed-80-v1" in encoded
        assert json.loads(encoded)["state_dir"] == str(policy.state_dir)

    def test_scheduler_bootstrap_routes_autotrader_policy_to_one_management_driver(
        self, tmp_path: Path
    ) -> None:
        record = policy_record(tmp_path)
        path, digest = write_policy(tmp_path, record)
        state_dir = Path(record["state_dir"])
        entrypoint = tmp_path / "scheduler_main.py"
        entrypoint.write_text("# focused fixture\n", encoding="utf-8")

        loop = build_scheduler_loop(
            identity=SchedulerIdentity(session_id="paperday-test", nonce="nonce-1"),
            state_dir=state_dir,
            schedule_config=path,
            schedule_config_sha256=digest,
            engine=object(),
        )
        spec = build_scheduler_spec(
            schedule_config=path,
            schedule_config_sha256=digest,
            state_dir=state_dir,
            entry_script=entrypoint,
        )

        assert loop.cadence_seconds == 300
        assert loop.command[:2] == ("options-cycle", "--arm")
        assert loop.lifecycle_receipts
        assert loop.policy_hash == digest
        assert loop.catalog_hash == "a" * 64
        assert spec.cadence_seconds == 300
        assert spec.command[:2] == ("options-cycle", "--arm")
        assert "--schedule-config" in loop.command
        assert str(path.resolve()) in loop.command
        assert digest in loop.command
        assert str(state_dir.resolve()) in loop.command
