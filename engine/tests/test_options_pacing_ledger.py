"""Durable pacing reservations and management protection."""

from __future__ import annotations

import datetime as dt

from engine.options.pacing import Priority, RequestKind
from engine.options.pacing_ledger import PacingLedger, ReservationState


class Clock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 8, 14, 14, 0, tzinfo=dt.timezone.utc)

    def __call__(self) -> dt.datetime:
        return self.now


def test_discovery_cannot_spend_the_management_reserve(tmp_path) -> None:
    clock = Clock()
    ledger = PacingLedger(
        tmp_path / "pacing.sqlite3",
        historical_per_window=8,
        general_per_window=8,
        clock=clock,
    )
    reservations = [
        ledger.reserve(
            RequestKind.GENERAL,
            owner_id="cycle",
            priority=Priority.DISCOVERY,
            request_key=f"d{i}",
        )
        for i in range(8)
    ]
    assert sum(item is not None for item in reservations) == 6
    management = ledger.reserve(
        RequestKind.GENERAL,
        owner_id="cycle",
        priority=Priority.EXITS_MANAGEMENT,
        request_key="exit-1",
    )
    assert management is not None
    assert ledger.snapshot(RequestKind.GENERAL, now=clock.now).outstanding == 7


def test_expired_reservation_is_recorded_as_crash_expiry_and_reusable(tmp_path) -> None:
    clock = Clock()
    ledger = PacingLedger(
        tmp_path / "pacing.sqlite3",
        general_per_window=2,
        reservation_ttl=dt.timedelta(seconds=30),
        clock=clock,
    )
    first = ledger.reserve(
        RequestKind.GENERAL,
        owner_id="dead-cycle",
        request_key="dead-1",
    )
    assert first is not None
    clock.now += dt.timedelta(seconds=31)
    assert ledger.reap_expired() == (first.reservation_id,)
    assert ledger.get(first.reservation_id).state == ReservationState.EXPIRED
    replacement = ledger.reserve(
        RequestKind.GENERAL,
        owner_id="new-cycle",
        request_key="new-1",
    )
    assert replacement is not None


def test_unknown_outcome_consumes_capacity_until_reconciliation(tmp_path) -> None:
    clock = Clock()
    ledger = PacingLedger(tmp_path / "pacing.sqlite3", general_per_window=2, clock=clock)
    reservation = ledger.reserve(
        RequestKind.GENERAL,
        owner_id="cycle",
        priority=Priority.AUTHORIZATION,
        request_key="send-1",
    )
    assert reservation is not None
    unknown = ledger.mark_unknown(reservation.reservation_id)
    assert unknown.state == ReservationState.UNKNOWN
    assert ledger.snapshot(RequestKind.GENERAL, now=clock.now).consumed == 1


def test_penalty_pauses_discovery_but_not_exit_reservations(tmp_path) -> None:
    clock = Clock()
    ledger = PacingLedger(tmp_path / "pacing.sqlite3", general_per_window=4, clock=clock)
    paused = ledger.penalize(RequestKind.GENERAL, now=clock.now)
    assert paused > clock.now
    assert ledger.reserve(
        RequestKind.GENERAL,
        owner_id="cycle",
        priority=Priority.DISCOVERY,
        request_key="paused-discovery",
    ) is None
    assert ledger.reserve(
        RequestKind.GENERAL,
        owner_id="cycle",
        priority=Priority.EXITS_MANAGEMENT,
        request_key="exit",
    ) is not None


def test_rolling_window_releases_committed_capacity(tmp_path) -> None:
    clock = Clock()
    ledger = PacingLedger(
        tmp_path / "pacing.sqlite3",
        general_per_window=2,
        general_window_seconds=60,
        clock=clock,
    )
    reservation = ledger.reserve(
        RequestKind.GENERAL,
        owner_id="cycle",
        priority=Priority.EXITS_MANAGEMENT,
        request_key="old-send",
    )
    assert reservation is not None
    ledger.commit(reservation.reservation_id)
    clock.now += dt.timedelta(seconds=61)
    assert ledger.snapshot(RequestKind.GENERAL, now=clock.now).consumed == 0
