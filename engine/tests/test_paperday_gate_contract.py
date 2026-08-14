"""Paper-day Gate 11: FULL sessions bind to a config/risk fingerprint."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from engine.cli import _paper_day_preflight
from engine.config import EngineConfig
from engine.options.approval import configuration_fingerprint
from engine.options.policy import RiskPolicy
from engine.paperday import (
    GATE_CONFIGURATION_FINGERPRINT,
    GATE_OPEN,
    MANDATE_FULL,
    READY,
    PaperDayPaths,
    entry_gate_preflight,
    read_gate,
    write_gate,
)
from paperday_support import NOW


def config_for(tmp_path: Path) -> EngineConfig:
    return EngineConfig(account_id="DU1234567", state_dir=tmp_path / "state")


def fingerprint_for(tmp_path: Path, policy: RiskPolicy | None = None) -> str:
    return configuration_fingerprint(config_for(tmp_path), policy or RiskPolicy())


def open_full_session(
    paths: PaperDayPaths,
    *,
    configuration_fingerprint: str | None,
) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.lock.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "fencing_token": "fence-1",
                "started_at": NOW.isoformat(),
                "controller_pid": 123,
            }
        ),
        encoding="utf-8",
    )
    write_gate(
        paths,
        entry_gate=GATE_OPEN,
        state=READY,
        session_id="session-1",
        now=NOW,
        fencing_token="fence-1",
        mandate=MANDATE_FULL,
        configuration_fingerprint=configuration_fingerprint,
    )


def test_full_armed_entry_allows_matching_fingerprint(tmp_path: Path) -> None:
    paths = PaperDayPaths(state_dir=tmp_path / "state")
    expected = fingerprint_for(tmp_path)

    open_full_session(paths, configuration_fingerprint=expected)

    gate = read_gate(paths)
    assert gate is not None
    assert gate[GATE_CONFIGURATION_FINGERPRINT] == expected
    preflight = entry_gate_preflight(
        paths,
        expected_configuration_fingerprint=expected,
    )
    assert preflight(armed=True) is None


def test_full_armed_entry_refuses_missing_session_fingerprint(tmp_path: Path) -> None:
    paths = PaperDayPaths(state_dir=tmp_path / "state")
    expected = fingerprint_for(tmp_path)

    open_full_session(paths, configuration_fingerprint=None)

    preflight = entry_gate_preflight(
        paths,
        expected_configuration_fingerprint=expected,
    )
    refusal = preflight(armed=True)
    assert refusal is not None
    assert "ENTRY_REFUSED_BY_FINGERPRINT" in refusal
    assert "recorded no risk/configuration fingerprint" in refusal
    assert preflight(armed=False) is None


def test_full_armed_entry_refuses_mismatched_fingerprint(tmp_path: Path) -> None:
    paths = PaperDayPaths(state_dir=tmp_path / "state")

    open_full_session(paths, configuration_fingerprint="old-fingerprint")

    preflight = entry_gate_preflight(
        paths,
        expected_configuration_fingerprint="live-fingerprint",
    )
    refusal = preflight(armed=True)
    assert refusal is not None
    assert "ENTRY_REFUSED_BY_FINGERPRINT" in refusal
    assert "different risk/configuration fingerprint" in refusal


def test_fingerprint_comparison_tracks_live_policy_mutation(tmp_path: Path) -> None:
    paths = PaperDayPaths(state_dir=tmp_path / "state")
    original = fingerprint_for(tmp_path)
    mutated = fingerprint_for(
        tmp_path,
        RiskPolicy(max_defined_loss_per_position=Decimal("499")),
    )
    assert mutated != original
    open_full_session(paths, configuration_fingerprint=original)

    assert (
        entry_gate_preflight(paths, expected_configuration_fingerprint=original)(
            armed=True
        )
        is None
    )
    refusal = entry_gate_preflight(
        paths,
        expected_configuration_fingerprint=mutated,
    )(armed=True)
    assert refusal is not None
    assert "ENTRY_REFUSED_BY_FINGERPRINT" in refusal


def test_cli_preflight_helper_passes_expected_fingerprint(
    tmp_path: Path, monkeypatch: Any
) -> None:
    captured: dict[str, Any] = {}

    def fake_entry_gate_preflight(
        paths: PaperDayPaths,
        *,
        expected_configuration_fingerprint: str | None = None,
    ) -> Any:
        captured["state_dir"] = paths.state_dir
        captured["expected"] = expected_configuration_fingerprint
        return lambda **_: None

    monkeypatch.setattr("engine.paperday.entry_gate_preflight", fake_entry_gate_preflight)
    config = config_for(tmp_path)

    preflight = _paper_day_preflight(
        config,
        expected_configuration_fingerprint="live-fingerprint",
    )

    assert preflight(armed=True) is None
    assert captured == {
        "state_dir": config.state_dir,
        "expected": "live-fingerprint",
    }


def test_stale_open_gate_refuses_unarmed_entry_consideration(tmp_path: Path) -> None:
    paths = PaperDayPaths(state_dir=tmp_path / "state")
    open_full_session(paths, configuration_fingerprint="live-fingerprint")
    lock = json.loads(paths.lock.read_text(encoding="utf-8"))
    lock["session_id"] = "replacement-session"
    paths.lock.write_text(json.dumps(lock), encoding="utf-8")

    refusal = entry_gate_preflight(
        paths, expected_configuration_fingerprint="live-fingerprint"
    )(armed=False)

    assert refusal is not None
    assert "stale entry authority" in refusal


def test_unknown_gate_schema_and_gate_value_refuse_unarmed_consideration(
    tmp_path: Path,
) -> None:
    paths = PaperDayPaths(state_dir=tmp_path / "state")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.gate.write_text(
        json.dumps(
            {
                "schema_version": 999,
                "entry_gate": "OPEN",
                "state": READY,
                "session_id": "session-1",
                "mandate": MANDATE_FULL,
            }
        ),
        encoding="utf-8",
    )
    refusal = entry_gate_preflight(paths)(armed=False)
    assert refusal is not None and "schema" in refusal

    paths.gate.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entry_gate": "UNKNOWN",
                "state": READY,
                "session_id": "session-1",
                "mandate": MANDATE_FULL,
            }
        ),
        encoding="utf-8",
    )
    refusal = entry_gate_preflight(paths)(armed=False)
    assert refusal is not None and "unknown" in refusal


def test_unreadable_gate_file_refuses_entry_consideration(tmp_path: Path) -> None:
    paths = PaperDayPaths(state_dir=tmp_path / "state")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.gate.write_text("{not-json", encoding="utf-8")

    refusal = entry_gate_preflight(paths)(armed=False)

    assert refusal is not None and "unreadable" in refusal
