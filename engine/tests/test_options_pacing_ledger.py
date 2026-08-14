"""Durable pacing reservations and management protection."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

from engine.options.pacing import (
    PacedRequestBudget,
    Priority,
    RequestKind,
    SharedPacingBudget,
)
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


def test_policy_discovery_fraction_caps_breadth_without_reducing_management_floor(tmp_path) -> None:
    clock = Clock()
    ledger = PacingLedger(
        tmp_path / "pacing.sqlite3",
        general_per_window=10,
        management_reserve_fraction=0.20,
        discovery_fraction=0.50,
        minimum_management_requests=1,
        clock=clock,
    )

    reservations = [
        ledger.reserve(
            RequestKind.GENERAL,
            owner_id="cycle",
            priority=Priority.DISCOVERY,
            request_key=f"d{i}",
        )
        for i in range(10)
    ]

    assert sum(item is not None for item in reservations) == 5
    snapshot = ledger.snapshot(RequestKind.GENERAL, now=clock.now)
    assert snapshot.management_reserve == 2
    assert snapshot.discovery_reserve == 5
    assert snapshot.discovery_available == 0


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


def test_shared_budget_commits_every_adapter_request_to_the_durable_ledger(tmp_path) -> None:
    clock = Clock()
    ledger = PacingLedger(tmp_path / "pacing.sqlite3", general_per_window=4, clock=clock)
    local = PacedRequestBudget(clock=lambda: 0.0, sleeper=lambda _seconds: None)
    shared = SharedPacingBudget(
        local,
        ledger,
        owner_id="session:nonce",
        clock=clock,
    )

    shared.acquire(RequestKind.GENERAL, priority=Priority.AUTHORIZATION)

    snapshot = ledger.snapshot(RequestKind.GENERAL, now=clock.now)
    assert snapshot.consumed == 1
    assert snapshot.outstanding == 0


def test_shared_budget_penalty_updates_both_connection_and_restart_authorities(tmp_path) -> None:
    clock = Clock()
    ledger = PacingLedger(tmp_path / "pacing.sqlite3", general_per_window=4, clock=clock)
    local = PacedRequestBudget(clock=lambda: 0.0, sleeper=lambda _seconds: None)
    shared = SharedPacingBudget(local, ledger, owner_id="session:nonce", clock=clock)

    shared.penalize(RequestKind.GENERAL)

    assert ledger.snapshot(RequestKind.GENERAL, now=clock.now).paused_until is not None


def test_worker_read_adapters_consume_the_shared_general_budget() -> None:
    """Reconciliation and account sizing may not spend outside the ledger."""
    from engine.options.adapters import IBKRPortfolioStateAdapter, read_open_orders

    class RecordingBudget:
        def __init__(self) -> None:
            self.requests = []

        def acquire(self, kind, *, priority) -> None:
            self.requests.append((kind, priority))

    class FakeIB:
        def openTrades(self):
            return []

    class FakeBroker:
        def __init__(self) -> None:
            self.ib = FakeIB()

        def account_summary(self):
            return [
                ("NetLiquidation", "100000", "USD"),
                ("FullInitMarginReq", "0", "USD"),
            ]

    budget = RecordingBudget()
    broker = FakeBroker()
    assert read_open_orders(
        broker.ib,
        budget=budget,
        budget_priority=Priority.WORKING_ORDERS,
    ) == ()
    snapshot = IBKRPortfolioStateAdapter(
        broker,
        budget=budget,
        budget_priority=Priority.AUTHORIZATION,
    ).snapshot(as_of=dt.datetime(2026, 8, 14, 14, 0, tzinfo=dt.timezone.utc))

    assert snapshot.net_liquidation == 100000
    assert budget.requests == [
        (RequestKind.GENERAL, Priority.WORKING_ORDERS),
        (RequestKind.GENERAL, Priority.AUTHORIZATION),
    ]


def test_underlying_reference_quote_uses_the_shared_general_budget() -> None:
    from engine.options.runner import _underlying_reference_price

    class RecordingBudget:
        def __init__(self) -> None:
            self.requests = []

        def acquire(self, kind, *, priority) -> None:
            self.requests.append((kind, priority))

    class Broker:
        def quote(self, _symbol):
            return SimpleNamespace(price="100")

    budget = RecordingBudget()
    assert _underlying_reference_price(Broker(), "SPY", request_budget=budget) == Decimal("100")
    assert budget.requests == [(RequestKind.GENERAL, Priority.CANDIDATE_CONSTRUCTION)]
