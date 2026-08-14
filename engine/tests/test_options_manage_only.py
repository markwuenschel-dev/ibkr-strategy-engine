"""The runner's explicit entry mandate boundary.

These tests pin the unattended-management contract at the runner seam. The
default pass may reconcile and manage an existing position, but it must not
reach any entry work. Opening is available only through an explicit FULL
mandate, where the existing preflight and session-lease fences remain in force.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import datetime as dt

import pytest

from engine.options.lifecycle import ManagementAction
from engine.options.policy import RiskPolicy
from engine.options.runner import EntryMode, run_once
from engine.options.selection import Bias
from test_options_runner import (
    FakeBroker,
    FakeIB,
    FakeMarketDataPort,
    FakePortfolioPort,
    NOW,
    TODAY,
    gate_for,
    seed_open_position,
    store_for,
)
from engine.options.transmit import SESSION_LEASE_LOST


def run_manage_only(
    broker: Any,
    gate: Any,
    store: Any,
    *,
    armed: bool,
    entry_mode: EntryMode = EntryMode.MANAGE_ONLY,
    **overrides: Any,
) -> Any:
    return run_once(
        broker,
        gate=gate,
        journal=gate.journal,
        store=store,
        policy=overrides.pop("policy", None) or RiskPolicy(),
        armed=armed,
        symbol="SPY",
        bias=Bias.BULLISH,
        market_data=overrides.pop("market_data", None) or FakeMarketDataPort(),
        portfolio=overrides.pop("portfolio", None) or FakePortfolioPort(),
        now=overrides.pop("now", NOW),
        today=overrides.pop("today", TODAY),
        account="DU1234567",
        entry_mode=entry_mode,
        **overrides,
    )


class TestManageOnlyBoundary:
    def test_default_mandate_reports_machine_readable_refusal_and_skips_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ib = FakeIB()
        store = store_for(tmp_path)

        def fail_if_entry_pipeline_is_reached(**_: Any) -> Any:
            raise AssertionError("MANAGE_ONLY reached candidate construction")

        monkeypatch.setattr(
            "engine.options.runner._build_candidate", fail_if_entry_pipeline_is_reached
        )

        report = run_manage_only(
            FakeBroker(ib=ib), gate_for(tmp_path), store, armed=True
        )

        assert report.entry_mode is EntryMode.MANAGE_ONLY
        assert report.entry_refusal_code == "RUNNER_MANAGE_ONLY"
        assert "RUNNER_MANAGE_ONLY" in report.refusal_codes
        assert report.candidate is None
        assert report.iv_rank is None
        assert report.risk is None
        assert report.governor is None
        assert report.transmissions == []
        assert ib.placed == []
        record = report.to_record()
        assert record["entry_mode"] == "MANAGE_ONLY"
        assert record["entry_refusal_code"] == "RUNNER_MANAGE_ONLY"

    def test_ten_management_ticks_never_enter_entry_pipeline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scheduler's repeated management cadence cannot accumulate entry work."""

        ib = FakeIB()
        store = store_for(tmp_path)
        gate = gate_for(tmp_path)
        calls = 0

        def fail_if_entry_pipeline_is_reached(**_: Any) -> Any:
            nonlocal calls
            calls += 1
            raise AssertionError("MANAGE_ONLY reached candidate construction")

        monkeypatch.setattr(
            "engine.options.runner._build_candidate", fail_if_entry_pipeline_is_reached
        )

        reports = [
            run_manage_only(
                FakeBroker(ib=ib),
                gate,
                store,
                armed=True,
                now=NOW + dt.timedelta(minutes=tick),
            )
            for tick in range(10)
        ]

        assert calls == 0
        assert len(reports) == 10
        assert all(report.entry_mode is EntryMode.MANAGE_ONLY for report in reports)
        assert all(report.entry_refusal_code == "RUNNER_MANAGE_ONLY" for report in reports)
        assert all(report.candidate is None for report in reports)
        assert all(report.iv_rank is None for report in reports)
        assert all(report.risk is None for report in reports)
        assert all(report.governor is None for report in reports)
        assert all(report.transmissions == [] for report in reports)
        assert ib.placed == []

    def test_manage_only_can_transmit_an_eligible_exit(self, tmp_path: Path) -> None:
        ib = FakeIB()
        store = store_for(tmp_path)
        seed_open_position(store, dte=10)

        report = run_manage_only(
            FakeBroker(ib=ib, positions=()), gate_for(tmp_path), store, armed=True
        )

        assert report.entry_mode is EntryMode.MANAGE_ONLY
        assert report.decisions[0].action is ManagementAction.CLOSE_DTE
        assert [transmission.action.value for transmission in report.transmissions] == [
            "CLOSE"
        ]
        assert len(ib.placed) == 1
        assert report.entry_refusal_code == "RUNNER_MANAGE_ONLY"


class TestFullModePreservesEntryFences:
    def test_full_mode_still_honors_entry_preflight(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def preflight(**_: Any) -> str:
            calls.append("preflight")
            return "window closed"

        report = run_manage_only(
            FakeBroker(),
            gate_for(tmp_path),
            store_for(tmp_path),
            armed=True,
            entry_mode=EntryMode.FULL,
            entry_preflight=preflight,
        )

        assert calls == ["preflight"]
        assert report.entry_mode is EntryMode.FULL
        assert "OPTIONS_ENTRY_PREFLIGHT_REFUSED" in report.refusal_codes

    def test_full_mode_still_honors_the_final_session_lease_fence(
        self, tmp_path: Path
    ) -> None:
        report = run_manage_only(
            FakeBroker(),
            gate_for(tmp_path),
            store_for(tmp_path),
            armed=True,
            entry_mode=EntryMode.FULL,
            session_lease=lambda: "session replaced",
        )

        assert report.entry_mode is EntryMode.FULL
        assert SESSION_LEASE_LOST in report.refusal_codes
        assert report.transmissions == []
