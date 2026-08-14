"""Durable, shared pacing reservations for a broker-owning worker.

``PacedRequestBudget`` is deliberately lightweight and process-local because
the original scanner was one-shot.  A persistent worker needs stronger
semantics: reservations must survive a restart, management capacity must be
protected from discovery, and an unknown post-crash request must not be
treated as free capacity.  This SQLite ledger supplies those semantics while
leaving the existing token bucket available to manual commands.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .pacing import Priority, RequestKind

__all__ = [
    "DurablePacingLedger",
    "PacingLedger",
    "PacingReservation",
    "PacingSnapshot",
    "ReservationState",
]


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("pacing ledger timestamps must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return _utc(value).isoformat()


def _parse(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    return _utc(parsed)


class ReservationState(str):
    ACTIVE = "ACTIVE"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PacingReservation:
    reservation_id: str
    kind: RequestKind
    cost: int
    priority: Priority
    owner_id: str
    created_at: dt.datetime
    expires_at: dt.datetime
    state: str = ReservationState.ACTIVE
    request_key: str | None = None

    @property
    def active(self) -> bool:
        return self.state == ReservationState.ACTIVE


@dataclass(frozen=True)
class PacingSnapshot:
    kind: RequestKind
    limit: int
    window_seconds: float
    consumed: int
    outstanding: int
    available: int
    management_reserve: int
    penalty_factor: float
    paused_until: dt.datetime | None

    @property
    def discovery_available(self) -> int:
        return max(0, self.available - self.management_reserve)


class PacingLedger:
    """Atomic rolling-window budget shared by every phase of one session."""

    def __init__(
        self,
        path: Path,
        *,
        historical_per_window: int = 55,
        historical_window_seconds: float = 600.0,
        general_per_window: int = 40,
        general_window_seconds: float = 60.0,
        management_reserve_fraction: float = 0.25,
        reservation_ttl: dt.timedelta = dt.timedelta(minutes=5),
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        if not 0.0 < management_reserve_fraction < 1.0:
            raise ValueError("management_reserve_fraction must be in (0, 1)")
        if reservation_ttl.total_seconds() <= 0:
            raise ValueError("reservation_ttl must be positive")
        self.path = Path(path)
        if self.path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
            self.path = self.path / "pacing.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.reserve_fraction = management_reserve_fraction
        self.reservation_ttl = reservation_ttl
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._limits = {
            RequestKind.HISTORICAL: (int(historical_per_window), float(historical_window_seconds)),
            RequestKind.GENERAL: (int(general_per_window), float(general_window_seconds)),
        }
        for kind, (limit, window) in self._limits.items():
            if limit <= 0 or window <= 0:
                raise ValueError(f"{kind.value} pacing limit/window must be positive")
        self._initialize()

    def _now(self) -> dt.datetime:
        return _utc(self.clock())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pacing_limits (
                    kind TEXT PRIMARY KEY,
                    limit_count INTEGER NOT NULL,
                    window_seconds REAL NOT NULL,
                    penalty_factor REAL NOT NULL DEFAULT 1.0,
                    paused_until TEXT
                );
                CREATE TABLE IF NOT EXISTS pacing_usage (
                    usage_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    cost INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    reservation_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pacing_usage_window
                    ON pacing_usage(kind, occurred_at);
                CREATE TABLE IF NOT EXISTS pacing_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    request_key TEXT UNIQUE,
                    kind TEXT NOT NULL,
                    cost INTEGER NOT NULL,
                    priority INTEGER NOT NULL,
                    owner_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    state TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pacing_reservations_expiry
                    ON pacing_reservations(state, expires_at);
                CREATE TABLE IF NOT EXISTS pacing_crash_expiry (
                    expiry_id TEXT PRIMARY KEY,
                    reservation_id TEXT NOT NULL,
                    expired_at TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    cost INTEGER NOT NULL
                );
                """
            )
            for kind, (limit, window) in self._limits.items():
                connection.execute(
                    """
                    INSERT INTO pacing_limits(kind, limit_count, window_seconds)
                    VALUES (?, ?, ?)
                    ON CONFLICT(kind) DO UPDATE SET
                        limit_count = excluded.limit_count,
                        window_seconds = excluded.window_seconds
                    """,
                    (kind.value, limit, window),
                )

    @staticmethod
    def _row_to_reservation(row: sqlite3.Row | None) -> PacingReservation | None:
        if row is None:
            return None
        return PacingReservation(
            reservation_id=str(row["reservation_id"]),
            request_key=row["request_key"],
            kind=RequestKind(str(row["kind"])),
            cost=int(row["cost"]),
            priority=Priority(int(row["priority"])),
            owner_id=str(row["owner_id"]),
            created_at=_parse(str(row["created_at"])),
            expires_at=_parse(str(row["expires_at"])),
            state=str(row["state"]),
        )

    def reap_expired(self, *, now: dt.datetime | None = None) -> tuple[str, ...]:
        """Expire abandoned reservations and leave a durable crash receipt."""
        current = _utc(now or self._now())
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pacing_reservations
                 WHERE state = ? AND expires_at <= ?
                """,
                (ReservationState.ACTIVE, _iso(current)),
            ).fetchall()
            expired: list[str] = []
            for row in rows:
                reservation_id = str(row["reservation_id"])
                connection.execute(
                    "UPDATE pacing_reservations SET state = ? WHERE reservation_id = ?",
                    (ReservationState.EXPIRED, reservation_id),
                )
                connection.execute(
                    """
                    INSERT INTO pacing_crash_expiry(
                        expiry_id, reservation_id, expired_at, owner_id, cost
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        reservation_id,
                        _iso(current),
                        row["owner_id"],
                        row["cost"],
                    ),
                )
                expired.append(reservation_id)
            return tuple(expired)

    def _usage_and_reserved(
        self,
        connection: sqlite3.Connection,
        kind: RequestKind,
        *,
        now: dt.datetime,
    ) -> tuple[int, int]:
        _, window = self._limits[kind]
        cutoff = _iso(now - dt.timedelta(seconds=window))
        consumed = connection.execute(
            """
            SELECT COALESCE(SUM(cost), 0) AS total
              FROM pacing_usage
             WHERE kind = ? AND occurred_at > ?
            """,
            (kind.value, cutoff),
        ).fetchone()["total"]
        reserved = connection.execute(
            """
            SELECT COALESCE(SUM(cost), 0) AS total
              FROM pacing_reservations
             WHERE kind = ? AND state = ? AND expires_at > ?
            """,
            (kind.value, ReservationState.ACTIVE, _iso(now)),
        ).fetchone()["total"]
        return int(consumed or 0), int(reserved or 0)

    def _limit_row(self, connection: sqlite3.Connection, kind: RequestKind) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM pacing_limits WHERE kind = ?", (kind.value,)
        ).fetchone()
        if row is None:  # pragma: no cover - initialize owns this invariant
            raise RuntimeError(f"pacing kind {kind.value} is not initialized")
        return row

    def _reserve_floor(self, kind: RequestKind, limit: int, priority: Priority) -> int:
        if priority <= Priority.WORKING_ORDERS:
            return 0
        return min(int(limit * self.reserve_fraction), max(0, limit - 1))

    def reserve(
        self,
        kind: RequestKind,
        *,
        cost: int = 1,
        priority: Priority = Priority.CANDIDATE_CONSTRUCTION,
        owner_id: str,
        request_key: str | None = None,
        ttl: dt.timedelta | None = None,
        now: dt.datetime | None = None,
    ) -> PacingReservation | None:
        """Atomically reserve broker capacity, or return ``None``.

        ``request_key`` makes retries idempotent.  An existing active or
        terminal reservation is returned rather than spending the budget a
        second time; callers must reconcile ``UNKNOWN`` before trying a new
        request key after a crash.
        """
        if cost <= 0:
            raise ValueError("pacing reservation cost must be positive")
        if not owner_id.strip():
            raise ValueError("pacing reservation owner_id must be non-empty")
        current = _utc(now or self._now())
        duration = ttl or self.reservation_ttl
        if duration.total_seconds() <= 0:
            raise ValueError("pacing reservation ttl must be positive")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._reap_expired_in_connection(connection, current)
            if request_key:
                existing = connection.execute(
                    "SELECT * FROM pacing_reservations WHERE request_key = ?",
                    (request_key,),
                ).fetchone()
                if existing is not None:
                    return self._row_to_reservation(existing)
            limit_row = self._limit_row(connection, kind)
            paused_until = (
                _parse(str(limit_row["paused_until"]))
                if limit_row["paused_until"]
                else None
            )
            if priority is Priority.DISCOVERY and paused_until and current < paused_until:
                return None
            consumed, outstanding = self._usage_and_reserved(
                connection, kind, now=current
            )
            limit = int(limit_row["limit_count"])
            floor = self._reserve_floor(kind, limit, priority)
            if consumed + outstanding + cost > limit - floor:
                return None
            reservation = PacingReservation(
                reservation_id=str(uuid4()),
                request_key=request_key,
                kind=kind,
                cost=cost,
                priority=priority,
                owner_id=owner_id,
                created_at=current,
                expires_at=current + duration,
            )
            connection.execute(
                """
                INSERT INTO pacing_reservations(
                    reservation_id, request_key, kind, cost, priority, owner_id,
                    created_at, expires_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reservation.reservation_id,
                    reservation.request_key,
                    reservation.kind.value,
                    reservation.cost,
                    int(reservation.priority),
                    reservation.owner_id,
                    _iso(reservation.created_at),
                    _iso(reservation.expires_at),
                    reservation.state,
                ),
            )
            return reservation

    def _reap_expired_in_connection(
        self, connection: sqlite3.Connection, now: dt.datetime
    ) -> None:
        rows = connection.execute(
            """
            SELECT * FROM pacing_reservations
             WHERE state = ? AND expires_at <= ?
            """,
            (ReservationState.ACTIVE, _iso(now)),
        ).fetchall()
        for row in rows:
            reservation_id = str(row["reservation_id"])
            connection.execute(
                "UPDATE pacing_reservations SET state = ? WHERE reservation_id = ?",
                (ReservationState.EXPIRED, reservation_id),
            )
            connection.execute(
                """
                INSERT INTO pacing_crash_expiry(
                    expiry_id, reservation_id, expired_at, owner_id, cost
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    reservation_id,
                    _iso(now),
                    row["owner_id"],
                    row["cost"],
                ),
            )

    def get(self, reservation_id: str) -> PacingReservation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pacing_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
        return self._row_to_reservation(row)

    def commit(
        self,
        reservation_id: str,
        *,
        actual_cost: int | None = None,
        now: dt.datetime | None = None,
    ) -> PacingReservation:
        current = _utc(now or self._now())
        with self._connect() as connection:
            self._reap_expired_in_connection(connection, current)
            row = connection.execute(
                "SELECT * FROM pacing_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            reservation = self._row_to_reservation(row)
            if reservation is None:
                raise KeyError(f"unknown pacing reservation {reservation_id}")
            if reservation.state == ReservationState.COMMITTED:
                return reservation
            if reservation.state != ReservationState.ACTIVE:
                raise RuntimeError(
                    f"cannot commit pacing reservation {reservation_id} in {reservation.state}"
                )
            cost = actual_cost if actual_cost is not None else reservation.cost
            if cost <= 0 or cost > reservation.cost:
                raise ValueError("actual_cost must be positive and no greater than reserved cost")
            connection.execute(
                """
                INSERT INTO pacing_usage(
                    usage_id, kind, cost, occurred_at, priority, reservation_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    reservation.kind.value,
                    cost,
                    _iso(current),
                    int(reservation.priority),
                    reservation.reservation_id,
                ),
            )
            connection.execute(
                "UPDATE pacing_reservations SET state = ?, cost = ? WHERE reservation_id = ?",
                (ReservationState.COMMITTED, cost, reservation_id),
            )
            return PacingReservation(
                **{**reservation.__dict__, "state": ReservationState.COMMITTED, "cost": cost}
            )

    def mark_unknown(
        self, reservation_id: str, *, now: dt.datetime | None = None
    ) -> PacingReservation:
        """Retain capacity after a crash where broker effect is ambiguous."""
        current = _utc(now or self._now())
        with self._connect() as connection:
            self._reap_expired_in_connection(connection, current)
            row = connection.execute(
                "SELECT * FROM pacing_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            reservation = self._row_to_reservation(row)
            if reservation is None:
                raise KeyError(f"unknown pacing reservation {reservation_id}")
            if reservation.state == ReservationState.UNKNOWN:
                return reservation
            if reservation.state != ReservationState.ACTIVE:
                raise RuntimeError(
                    f"cannot mark reservation {reservation_id} unknown in {reservation.state}"
                )
            connection.execute(
                """
                INSERT INTO pacing_usage(
                    usage_id, kind, cost, occurred_at, priority, reservation_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    reservation.kind.value,
                    reservation.cost,
                    _iso(current),
                    int(reservation.priority),
                    reservation.reservation_id,
                ),
            )
            connection.execute(
                "UPDATE pacing_reservations SET state = ? WHERE reservation_id = ?",
                (ReservationState.UNKNOWN, reservation_id),
            )
            return PacingReservation(
                **{**reservation.__dict__, "state": ReservationState.UNKNOWN}
            )

    def release(self, reservation_id: str) -> PacingReservation:
        current = self._now()
        with self._connect() as connection:
            self._reap_expired_in_connection(connection, current)
            row = connection.execute(
                "SELECT * FROM pacing_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            reservation = self._row_to_reservation(row)
            if reservation is None:
                raise KeyError(f"unknown pacing reservation {reservation_id}")
            if reservation.state == ReservationState.RELEASED:
                return reservation
            if reservation.state != ReservationState.ACTIVE:
                raise RuntimeError(
                    f"cannot release pacing reservation {reservation_id} in {reservation.state}"
                )
            connection.execute(
                "UPDATE pacing_reservations SET state = ? WHERE reservation_id = ?",
                (ReservationState.RELEASED, reservation_id),
            )
            return PacingReservation(
                **{**reservation.__dict__, "state": ReservationState.RELEASED}
            )

    def penalize(
        self,
        kind: RequestKind,
        *,
        pause_for: dt.timedelta | None = None,
        now: dt.datetime | None = None,
    ) -> dt.datetime:
        current = _utc(now or self._now())
        with self._connect() as connection:
            row = self._limit_row(connection, kind)
            factor = float(row["penalty_factor"]) * 2.0
            _, window = self._limits[kind]
            horizon = pause_for or dt.timedelta(seconds=window * factor)
            paused_until = current + horizon
            previous = _parse(str(row["paused_until"])) if row["paused_until"] else None
            if previous and previous > paused_until:
                paused_until = previous
            connection.execute(
                """
                UPDATE pacing_limits
                   SET penalty_factor = ?, paused_until = ?
                 WHERE kind = ?
                """,
                (factor, _iso(paused_until), kind.value),
            )
            return paused_until

    def snapshot(
        self, kind: RequestKind, *, now: dt.datetime | None = None
    ) -> PacingSnapshot:
        current = _utc(now or self._now())
        with self._connect() as connection:
            self._reap_expired_in_connection(connection, current)
            row = self._limit_row(connection, kind)
            consumed, outstanding = self._usage_and_reserved(
                connection, kind, now=current
            )
            limit = int(row["limit_count"])
            reserve = int(limit * self.reserve_fraction)
            paused_until = _parse(str(row["paused_until"])) if row["paused_until"] else None
            return PacingSnapshot(
                kind=kind,
                limit=limit,
                window_seconds=float(row["window_seconds"]),
                consumed=consumed,
                outstanding=outstanding,
                available=max(0, limit - consumed - outstanding),
                management_reserve=reserve,
                penalty_factor=float(row["penalty_factor"]),
                paused_until=paused_until,
            )

    def available(
        self,
        kind: RequestKind,
        *,
        priority: Priority = Priority.CANDIDATE_CONSTRUCTION,
        now: dt.datetime | None = None,
    ) -> int:
        snapshot = self.snapshot(kind, now=now)
        floor = self._reserve_floor(kind, snapshot.limit, priority)
        if priority is Priority.DISCOVERY and snapshot.paused_until:
            current = _utc(now or self._now())
            if current < snapshot.paused_until:
                return 0
        return max(0, snapshot.available - floor)


DurablePacingLedger = PacingLedger
