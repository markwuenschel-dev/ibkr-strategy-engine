"""R8 adversarial fixtures for scheduler, broker, and entry recovery.

This file is intentionally test-only.  The first group exercises safety seams
that exist at the baseline.  The contract tests at the bottom are explicit
skips for recovery APIs that the unattended-worker lanes still have to expose;
they are preferable to a fake green test that silently tests a different path.
"""

from __future__ import annotations

import datetime as dt
import importlib
from pathlib import Path

import pytest

from engine.errors import RefusedError
from engine.options.orderstate import (
    BrokerOrderSnapshot,
    OrderLifecycleState,
    classify,
)
from engine.options.transmit import place_combo
from engine.scheduler import SchedulerIdentity, SchedulerLoop, TickOutcome
from scheduler_support import FakeClock, FakeEngine, always_open, paths_for
from test_options_transmit import RecordingIB, authorized, spread


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 14, 14, 0, tzinfo=UTC)


def _missing_contract(module_names: tuple[str, ...], symbols: tuple[str, ...]) -> None:
    """Skip with an actionable reason until a lane publishes its seam."""
    found: list[str] = []
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name or str(exc.name).startswith(module_name + "."):
                continue
            raise
        found.append(module_name)
        if all(hasattr(module, symbol) for symbol in symbols):
            return
    pytest.skip(
        "missing contract seam: "
        + ", ".join(f"{name}.{symbol}" for name in module_names for symbol in symbols)
        + (f" (searched {', '.join(found)})" if found else "")
    )


class TestBaselineRecoveryControls:
    def test_corrupt_session_lock_stops_before_broker_work(self, tmp_path: Path) -> None:
        """A torn authority record is not permission to run a pass."""
        paths = paths_for(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        lock = paths.root / "session.lock"
        lock.write_text('{"session_id": "paperday-20260814-', encoding="utf-8")
        clock = FakeClock(start=NOW)
        engine = FakeEngine()
        loop = SchedulerLoop(
            identity=SchedulerIdentity(session_id="paperday-20260814-r8", nonce="nonce-r8"),
            paths=paths,
            lock=lock,
            cadence_seconds=300.0,
            is_open=always_open,
            command=("options-cycle",),
            engine=engine,
            clock=clock,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        receipt = loop.tick(0)

        assert receipt.outcome is TickOutcome.STOPPED_LEASE_LOST
        assert engine.calls == [], "corrupt authority must fence broker work"

    def test_unknown_broker_result_is_uncertain_and_not_terminal(self) -> None:
        state = classify("", disconnected=True)
        snapshot = BrokerOrderSnapshot(
            state=state,
            observed_at=NOW,
            order_id=101,
            perm_id=9001,
            message="socket died after broker acceptance was possible",
        )

        assert state is OrderLifecycleState.UNKNOWN
        assert snapshot.is_uncertain
        assert not snapshot.is_terminal
        assert not snapshot.has_position

    def test_partial_broker_result_is_a_position_and_still_working(self) -> None:
        state = classify(
            "Submitted",
            filled=1,
            remaining=2,
            quantity=3,
        )
        snapshot = BrokerOrderSnapshot(
            state=state,
            observed_at=NOW,
            order_id=102,
            perm_id=9002,
            filled=1,
            remaining=2,
        )

        assert state is OrderLifecycleState.PARTIALLY_FILLED
        assert snapshot.has_position
        assert snapshot.is_working
        assert not snapshot.is_terminal

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "R2/R7 must make a production FULL opening caller provide a live "
            "session lease; baseline place_combo treats None as unfenced"
        ),
    )
    def test_an_authorized_open_without_a_lease_is_refused(self, tmp_path: Path) -> None:
        intent = spread()
        ib = RecordingIB()

        with pytest.raises(RefusedError, match="LEASE|lease|session"):
            place_combo(
                ib,
                intent,
                authorization=authorized(tmp_path, intent),
                account="DU1234567",
                session_lease=None,
            )
        assert ib.placed == []


class TestRecoveryContractSkips:
    def test_unmatched_tick_after_broker_activity_blocks_new_entry(self) -> None:
        _missing_contract(
            ("engine.autocycle", "engine.options.cycle", "engine.recovery"),
            ("recover_unmatched_tick",),
        )
        pytest.fail("the recovery contract module exists but its fixture adapter is not wired")

    @pytest.mark.parametrize(
        "crash_stage",
        (
            "after_handoff",
            "after_approval_consumption",
            "after_physical_send_intent",
            "after_broker_acceptance",
        ),
    )
    def test_entry_saga_crash_stage_requires_reconciliation(self, crash_stage: str) -> None:
        _missing_contract(
            ("engine.options.outbox", "engine.execution_outbox", "engine.autocycle"),
            ("reconcile_crash",),
        )
        pytest.fail(f"the execution-saga contract is not wired for {crash_stage}")

    def test_stale_paperday_and_reviewer_authority_cannot_arm(self) -> None:
        _missing_contract(
            ("engine.paperday", "engine.autocycle"),
            ("validate_entry_authority",),
        )
        pytest.fail("the authority fixture adapter is not wired")

    def test_review_only_has_no_transmission_chokepoint(self) -> None:
        _missing_contract(
            ("engine.options.runner", "engine.autocycle"),
            ("EntryMode", "run_once"),
        )
        from engine.options.runner import EntryMode

        if not hasattr(EntryMode, "REVIEW_ONLY"):
            pytest.skip("missing contract seam: EntryMode.REVIEW_ONLY")
        pytest.fail("the REVIEW_ONLY transmission fixture adapter is not wired")

    def test_fixed_rate_missed_slots_are_recorded_without_catchup(self) -> None:
        _missing_contract(
            ("engine.scheduler", "engine.autocycle"),
            ("FixedRateSlotLedger",),
        )
        pytest.fail("the fixed-rate slot fixture adapter is not wired")
