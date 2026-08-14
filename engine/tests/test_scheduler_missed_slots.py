from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from engine.scheduler import (
    AuthorityDecision,
    FixedRateScheduler,
    ScheduledSlotStore,
    SlotRecoveryRequired,
    SlotStatus,
    TickContext,
    TickEvent,
    append_tick_event,
    find_unmatched_ticks,
    read_tick_events,
)

from scheduler_support import FakeClock, FakeEngine, always_open, identity, paths_for, write_lock
from engine.scheduler import SchedulerLoop

UTC = dt.timezone.utc
ANCHOR = dt.datetime(2026, 8, 13, 14, 0, tzinfo=UTC)


def test_fixed_rate_marks_missed_slots_without_burst_replay(tmp_path: Path) -> None:
    store = ScheduledSlotStore(tmp_path / "slots.jsonl")
    scheduler = FixedRateScheduler(
        store=store,
        job="management",
        cadence_seconds=60,
        anchor=ANCHOR,
        session_id="paperday-test",
        lease_nonce="nonce-1",
    )

    current = scheduler.poll(now=ANCHOR + dt.timedelta(seconds=185))

    assert current is not None
    assert current.slot_id.endswith("-00000003")
    latest_by_id = {slot.slot_id: slot for slot in store.records()}
    assert [latest_by_id[f"management-paperday-test-{index:08d}"].status for index in range(4)] == [
        SlotStatus.MISSED,
        SlotStatus.MISSED,
        SlotStatus.MISSED,
        SlotStatus.SCHEDULED,
    ]
    started = scheduler.start(current, attempt_id="attempt-1", tick_id="tick-1", at=ANCHOR)
    scheduler.complete(started, at=ANCHOR, detail="fixture")

    later = scheduler.poll(now=ANCHOR + dt.timedelta(seconds=305))

    assert later is not None
    assert later.slot_id.endswith("-00000005")
    assert later.status is SlotStatus.SCHEDULED
    assert [slot.status for slot in store.records()][-2:] == [
        SlotStatus.MISSED,
        SlotStatus.SCHEDULED,
    ]


def test_started_slot_requires_reconciliation_before_a_new_slot(tmp_path: Path) -> None:
    store = ScheduledSlotStore(tmp_path / "slots.jsonl")
    scheduler = FixedRateScheduler(
        store=store,
        job="entry",
        cadence_seconds=300,
        anchor=ANCHOR,
        session_id="paperday-test",
        lease_nonce="nonce-1",
    )
    slot = scheduler.poll(now=ANCHOR)
    assert slot is not None
    started = scheduler.start(slot, attempt_id="attempt-1", tick_id="tick-1", at=ANCHOR)

    with pytest.raises(SlotRecoveryRequired, match="reconcile"):
        scheduler.poll(now=ANCHOR + dt.timedelta(seconds=600))

    unresolved = scheduler.unresolved(started, at=ANCHOR, detail="broker outcome unknown")
    scheduler.reconciled(unresolved, at=ANCHOR, detail="broker matched no order")
    recovered = scheduler.poll(now=ANCHOR + dt.timedelta(seconds=600))
    assert recovered is not None
    assert recovered.status is SlotStatus.SCHEDULED


def test_discovery_backlog_is_not_timer_slot_replay(tmp_path: Path) -> None:
    from engine.scheduler import DiscoveryBacklog

    backlog = DiscoveryBacklog(tmp_path / "backlog.jsonl")
    backlog.enqueue(key="catalog-v1", scheduled_for=ANCHOR, reason="pacing")

    assert [item["key"] for item in backlog.pending()] == ["catalog-v1"]
    backlog.complete(key="catalog-v1", at=ANCHOR, detail="serviced")
    assert backlog.pending() == []


def test_lifecycle_events_pair_a_started_tick_with_a_terminal_receipt(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    write_lock(paths)
    clock = FakeClock(start=ANCHOR)
    loop = SchedulerLoop(
        identity=identity(),
        paths=paths,
        lock=paths.root / "session.lock",
        cadence_seconds=60,
        is_open=always_open,
        command=("options-cycle", "--mode=SHADOW"),
        engine=FakeEngine(),
        clock=clock,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        lifecycle_receipts=True,
        policy_hash="a" * 64,
        catalog_hash="b" * 64,
    )

    loop.run(max_ticks=1)

    events = read_tick_events(paths)
    assert [event["event"] for event in events] == ["TICK_STARTED", "TICK_FINISHED"]
    assert events[0]["session_id"] == identity().session_id
    assert events[0]["lease_nonce"] == identity().nonce
    assert events[0]["policy_hash"] == "a" * 64
    assert find_unmatched_ticks(paths, session_id=identity().session_id) == []


def test_unmatched_tick_blocks_startup_and_does_not_replay_the_worker(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    write_lock(paths)
    context = TickContext(
        session_id=identity().session_id,
        lease_nonce=identity().nonce,
        tick_id="old-tick",
        attempt_id="old-attempt",
        policy_hash="a" * 64,
        catalog_hash="b" * 64,
        scheduled_for=ANCHOR,
    )
    append_tick_event(paths, TickEvent.TICK_STARTED, context, at=ANCHOR)
    engine = FakeEngine()
    clock = FakeClock(start=ANCHOR)
    loop = SchedulerLoop(
        identity=identity(),
        paths=paths,
        lock=paths.root / "session.lock",
        cadence_seconds=60,
        is_open=always_open,
        command=("options-cycle",),
        engine=engine,
        clock=clock,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        lifecycle_receipts=True,
    )

    receipts = loop.run(max_ticks=1)

    assert engine.calls == []
    assert receipts[-1].outcome.value == "STOPPED_RECOVERY_REQUIRED"
    assert receipts[-1].failure_code == "FAIL-UNMATCHED-TICK"
    assert any(event["event"] == "RECOVERY_REQUIRED" for event in read_tick_events(paths))


def test_unmatched_tick_from_an_older_fencing_nonce_still_blocks_the_session(
    tmp_path: Path,
) -> None:
    paths = paths_for(tmp_path)
    write_lock(paths)
    old_context = TickContext(
        session_id=identity().session_id,
        lease_nonce="old-nonce",
        tick_id="old-nonce-tick",
        attempt_id="old-nonce-attempt",
        scheduled_for=ANCHOR,
    )
    append_tick_event(paths, TickEvent.TICK_STARTED, old_context, at=ANCHOR)
    current_identity = identity(nonce="new-nonce")
    engine = FakeEngine()
    clock = FakeClock(start=ANCHOR)
    loop = SchedulerLoop(
        identity=current_identity,
        paths=paths,
        lock=paths.root / "session.lock",
        cadence_seconds=60,
        is_open=always_open,
        command=("options-cycle",),
        engine=engine,
        clock=clock,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        lifecycle_receipts=True,
    )

    receipts = loop.run(max_ticks=1)

    assert engine.calls == []
    assert receipts[-1].failure_code == "FAIL-UNMATCHED-TICK"


def test_authority_hook_fails_closed_before_ticking_the_worker(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    write_lock(paths)
    engine = FakeEngine()
    clock = FakeClock(start=ANCHOR)
    loop = SchedulerLoop(
        identity=identity(),
        paths=paths,
        lock=paths.root / "session.lock",
        cadence_seconds=60,
        is_open=always_open,
        command=("options-cycle",),
        engine=engine,
        clock=clock,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        authority_check=lambda: AuthorityDecision(
            allowed=False,
            failure_code="FAIL-STALE-PAPERDAY-AUTHORITY",
            detail="old heartbeat",
        ),
    )

    receipt = loop.tick(0)

    assert engine.calls == []
    assert receipt.failure_code == "FAIL-STALE-PAPERDAY-AUTHORITY"
    assert receipt.outcome.value == "STOPPED_AUTHORITY_INVALID"
