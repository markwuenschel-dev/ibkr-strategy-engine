"""Batch observation-cache interfaces and durable implementations.

The scanner's old JSONL files are intentionally kept as a migration format,
but a full-universe worker cannot repeatedly open one file per symbol and then
make fairness decisions from process-local state.  This module provides a
small raw-observation interface and a SQLite implementation with indexed
batch reads, atomic writes, per-record quarantine, and a durable refresh
queue.

Only slow observations and session metadata belong here.  Perishable quotes,
greeks, and live marks are rejected at the write boundary; caching them would
turn a performance optimization into a stale-data authorization path.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from types import MappingProxyType
from uuid import uuid4

from .freshness import FreshnessClass, ObservationEnvelope

__all__ = [
    "CacheWriteRefused",
    "FairRefreshQueue",
    "JSONLObservationCache",
    "JsonlObservationCache",
    "ObservationCache",
    "ObservationUpdate",
    "RawObservation",
    "RefreshState",
    "SQLiteObservationCache",
]


def _utc(now: dt.datetime | None = None) -> dt.datetime:
    value = now or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        raise ValueError("cache timestamps must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime | None) -> str | None:
    return value.astimezone(dt.timezone.utc).isoformat() if value else None


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("persisted cache timestamp is naive")
    return parsed.astimezone(dt.timezone.utc)


def _normal_symbol(symbol: str) -> str:
    raw = getattr(symbol, "symbol", symbol)
    normalized = str(raw).strip().upper()
    if not normalized:
        raise ValueError("an observation must name a symbol")
    return normalized


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


class CacheWriteRefused(ValueError):
    """The cache rejected data that is unsafe or not serializable."""


@dataclass(frozen=True)
class RawObservation:
    """One raw observation with the provenance required for reuse."""

    symbol: str
    key: str
    payload: Mapping[str, Any]
    envelope: ObservationEnvelope
    catalog_version: str = "unknown"

    def __post_init__(self) -> None:
        symbol = _normal_symbol(self.symbol)
        if not self.key.strip():
            raise ValueError("an observation must name its observation key")
        if self.envelope.symbol.strip().upper() != symbol:
            raise ValueError("observation and envelope symbols do not match")
        if self.envelope.freshness_class is FreshnessClass.PERISHABLE:
            raise CacheWriteRefused(
                f"{symbol}/{self.key}: perishable observations are never cacheable"
            )
        if not isinstance(self.payload, Mapping):
            raise TypeError("raw observation payload must be a mapping")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "key", self.key.strip())
        object.__setattr__(self, "payload", _freeze(self.payload))

    @property
    def configuration_version(self) -> str:
        return self.envelope.configuration_version

    @property
    def observed_at(self) -> dt.datetime:
        return self.envelope.observed_at

    def fresh(self, *, now: dt.datetime, session_date: dt.date) -> bool:
        return self.envelope.fresh(now=now, session_date=session_date)

    def to_record(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "key": self.key,
            "payload": _jsonable(self.payload),
            "envelope": self.envelope.to_record(),
            "catalog_version": self.catalog_version,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "RawObservation":
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("raw observation payload is not an object")
        envelope = record.get("envelope")
        if not isinstance(envelope, Mapping):
            raise ValueError("raw observation envelope is not an object")
        return cls(
            symbol=str(record["symbol"]),
            key=str(record["key"]),
            payload=payload,
            envelope=ObservationEnvelope.from_record(envelope),
            catalog_version=str(record.get("catalog_version", "unknown")),
        )


# An update is a complete raw observation.  The alias keeps call sites
# readable without introducing a second record type that could drift from the
# read path.
ObservationUpdate = RawObservation


class ObservationCache(Protocol):
    """Storage contract used by breadth and refresh planners."""

    def read_many(
        self,
        symbols: Iterable[str],
        *,
        now: dt.datetime | None = None,
        session_date: dt.date | None = None,
        catalog_version: str | None = None,
        configuration_version: str | None = None,
        include_expired: bool = False,
    ) -> Mapping[str, tuple[RawObservation, ...]]: ...

    def due(
        self,
        *,
        now: dt.datetime,
        limit: int,
        catalog_version: str = "unknown",
        configuration_version: str = "unknown",
    ) -> tuple["RefreshState", ...]: ...

    def write_batch(self, updates: Iterable[RawObservation]) -> None: ...


@dataclass(frozen=True)
class RefreshState:
    """Durable fairness state for one catalog symbol."""

    symbol: str
    catalog_version: str
    configuration_version: str
    last_phase_one_at: dt.datetime | None = None
    last_phase_two_at: dt.datetime | None = None
    next_due_at: dt.datetime | None = None
    starvation_since: dt.datetime | None = None
    previous_rank: float | None = None
    deferred_reason: str | None = None
    estimated_request_cost: int = 1
    claimed_until: dt.datetime | None = None
    claim_owner: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _normal_symbol(self.symbol))
        if self.estimated_request_cost <= 0:
            raise ValueError("estimated_request_cost must be positive")

    @property
    def never_seen(self) -> bool:
        return self.last_phase_one_at is None

    def starvation_age_seconds(self, *, now: dt.datetime) -> float:
        if self.starvation_since is None:
            return 0.0
        return max(0.0, (_utc(now) - self.starvation_since).total_seconds())


def _sqlite_path(path: Path) -> Path:
    path = Path(path)
    if path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
        return path
    return path / "observations.sqlite3"


class FairRefreshQueue:
    """SQLite-backed fair refresh queue.

    Selection gives never-seen and oldest symbols an exploration lane before
    using the remaining capacity for the highest-ranked symbols.  Observing a
    symbol advances its due time, so an attractive symbol cannot permanently
    starve the rest of the catalog.
    """

    def __init__(self, path: Path) -> None:
        self.path = _sqlite_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS refresh_queue (
                    symbol TEXT PRIMARY KEY,
                    catalog_version TEXT NOT NULL,
                    configuration_version TEXT NOT NULL,
                    last_phase_one_at TEXT,
                    last_phase_two_at TEXT,
                    next_due_at TEXT,
                    starvation_since TEXT NOT NULL,
                    previous_rank REAL,
                    deferred_reason TEXT,
                    estimated_request_cost INTEGER NOT NULL,
                    claimed_until TEXT,
                    claim_owner TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_refresh_due
                    ON refresh_queue(next_due_at, starvation_since, previous_rank);
                """
            )

    @staticmethod
    def _row_to_state(row: sqlite3.Row) -> RefreshState:
        return RefreshState(
            symbol=str(row["symbol"]),
            catalog_version=str(row["catalog_version"]),
            configuration_version=str(row["configuration_version"]),
            last_phase_one_at=_parse_time(row["last_phase_one_at"]),
            last_phase_two_at=_parse_time(row["last_phase_two_at"]),
            next_due_at=_parse_time(row["next_due_at"]),
            starvation_since=_parse_time(row["starvation_since"]),
            previous_rank=(
                float(row["previous_rank"]) if row["previous_rank"] is not None else None
            ),
            deferred_reason=row["deferred_reason"],
            estimated_request_cost=int(row["estimated_request_cost"]),
            claimed_until=_parse_time(row["claimed_until"]),
            claim_owner=row["claim_owner"],
        )

    def seed(
        self,
        symbols: Iterable[str],
        *,
        catalog_version: str,
        configuration_version: str,
        now: dt.datetime,
        interval: dt.timedelta = dt.timedelta(minutes=30),
        estimated_request_cost: int = 1,
    ) -> None:
        now = _utc(now)
        if interval.total_seconds() <= 0:
            raise ValueError("refresh interval must be positive")
        if estimated_request_cost <= 0:
            raise ValueError("estimated_request_cost must be positive")
        symbols = tuple(dict.fromkeys(_normal_symbol(symbol) for symbol in symbols))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for symbol in symbols:
                old = connection.execute(
                    "SELECT * FROM refresh_queue WHERE symbol = ?", (symbol,)
                ).fetchone()
                if old is None:
                    connection.execute(
                        """
                        INSERT INTO refresh_queue(
                            symbol, catalog_version, configuration_version,
                            last_phase_one_at, last_phase_two_at, next_due_at,
                            starvation_since, previous_rank, deferred_reason,
                            estimated_request_cost, claimed_until, claim_owner,
                            updated_at
                        ) VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, ?, NULL, NULL, ?)
                        """,
                        (
                            symbol,
                            catalog_version,
                            configuration_version,
                            _iso(now),
                            _iso(now),
                            estimated_request_cost,
                            _iso(now),
                        ),
                    )
                    continue
                changed_manifest = (
                    old["catalog_version"] != catalog_version
                    or old["configuration_version"] != configuration_version
                )
                if changed_manifest:
                    connection.execute(
                        """
                        UPDATE refresh_queue
                           SET catalog_version = ?, configuration_version = ?,
                               last_phase_one_at = NULL, last_phase_two_at = NULL,
                               next_due_at = ?, starvation_since = ?,
                               previous_rank = NULL, deferred_reason = NULL,
                               estimated_request_cost = ?, claimed_until = NULL,
                               claim_owner = NULL, updated_at = ?
                         WHERE symbol = ?
                        """,
                        (
                            catalog_version,
                            configuration_version,
                            _iso(now),
                            _iso(now),
                            estimated_request_cost,
                            _iso(now),
                            symbol,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE refresh_queue
                           SET estimated_request_cost = ?, updated_at = ?
                         WHERE symbol = ?
                        """,
                        (estimated_request_cost, _iso(now), symbol),
                    )

    def get(self, symbol: str) -> RefreshState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM refresh_queue WHERE symbol = ?", (_normal_symbol(symbol),)
            ).fetchone()
        return self._row_to_state(row) if row is not None else None

    def select_due(
        self,
        *,
        now: dt.datetime,
        limit: int,
        catalog_version: str,
        configuration_version: str,
        claim_owner: str | None = None,
        claim_ttl: dt.timedelta = dt.timedelta(minutes=10),
    ) -> tuple[RefreshState, ...]:
        """Select due work with an exploration quota and optional leases."""
        now = _utc(now)
        if limit <= 0:
            return ()
        if claim_owner is not None and claim_ttl.total_seconds() <= 0:
            raise ValueError("claim_ttl must be positive")
        with self._connect() as connection:
            if claim_owner is not None:
                connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM refresh_queue
                 WHERE catalog_version = ?
                   AND configuration_version = ?
                   AND (next_due_at IS NULL OR next_due_at <= ?)
                   AND (claimed_until IS NULL OR claimed_until <= ?)
                 ORDER BY
                   CASE WHEN last_phase_one_at IS NULL THEN 0 ELSE 1 END,
                   starvation_since ASC,
                   next_due_at ASC,
                   previous_rank DESC,
                   symbol ASC
                """,
                (catalog_version, configuration_version, _iso(now), _iso(now)),
            ).fetchall()
            states = [self._row_to_state(row) for row in rows]
            if not states:
                return ()

            # Keep an explicit exploration lane.  It is tempting to sort only
            # by IV/rank, but that makes a permanently attractive symbol a
            # starvation bug at scale.
            explore_count = max(1, limit // 4)
            unseen = [state for state in states if state.never_seen]
            aged = [state for state in states if not state.never_seen]
            aged.sort(
                key=lambda state: (
                    state.starvation_since or dt.datetime.max.replace(tzinfo=dt.timezone.utc),
                    state.next_due_at or dt.datetime.max.replace(tzinfo=dt.timezone.utc),
                )
            )
            exploration = (unseen + aged)[:explore_count]
            selected_symbols = {state.symbol for state in exploration}
            exploitation = sorted(
                (state for state in states if state.symbol not in selected_symbols),
                key=lambda state: (
                    -(state.previous_rank if state.previous_rank is not None else float("-inf")),
                    state.starvation_since or dt.datetime.max.replace(tzinfo=dt.timezone.utc),
                    state.symbol,
                ),
            )
            selected = (exploration + exploitation)[:limit]
            if claim_owner is not None:
                until = now + claim_ttl
                for state in selected:
                    connection.execute(
                        """
                        UPDATE refresh_queue
                           SET claimed_until = ?, claim_owner = ?, updated_at = ?
                         WHERE symbol = ?
                        """,
                        (_iso(until), claim_owner, _iso(now), state.symbol),
                    )
                selected = [
                    RefreshState(
                        **{
                            **state.__dict__,
                            "claimed_until": until,
                            "claim_owner": claim_owner,
                        }
                    )
                    for state in selected
                ]
            return tuple(selected)

    def mark_phase_one(
        self,
        symbol: str,
        *,
        observed_at: dt.datetime,
        next_due_at: dt.datetime,
        previous_rank: float | None = None,
    ) -> None:
        observed_at = _utc(observed_at)
        next_due_at = _utc(next_due_at)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE refresh_queue
                   SET last_phase_one_at = ?, next_due_at = ?,
                       starvation_since = ?, previous_rank = ?,
                       deferred_reason = NULL, claimed_until = NULL,
                       claim_owner = NULL, updated_at = ?
                 WHERE symbol = ?
                """,
                (
                    _iso(observed_at),
                    _iso(next_due_at),
                    _iso(next_due_at),
                    previous_rank,
                    _iso(observed_at),
                    _normal_symbol(symbol),
                ),
            )

    def mark_phase_two(
        self,
        symbol: str,
        *,
        observed_at: dt.datetime,
        previous_rank: float | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE refresh_queue
                   SET last_phase_two_at = ?, previous_rank = COALESCE(?, previous_rank),
                       claimed_until = NULL, claim_owner = NULL, updated_at = ?
                 WHERE symbol = ?
                """,
                (
                    _iso(_utc(observed_at)),
                    previous_rank,
                    _iso(_utc(observed_at)),
                    _normal_symbol(symbol),
                ),
            )

    def phase_two_order(
        self,
        *,
        symbols: Iterable[str],
        catalog_version: str,
        configuration_version: str,
    ) -> tuple[str, ...]:
        """Return a starvation-safe order for the deep ring.

        Deep work is deliberately ordered by the oldest (or never-recorded)
        ``last_phase_two_at`` before rank.  Rank remains a deterministic tie
        breaker, but it cannot repeatedly occupy every deep slot while an
        otherwise eligible symbol waits forever.  The caller records the
        attempt with :meth:`mark_phase_two` after the probe, including a
        pacing deferral, so the ring advances durably across restarts.
        """

        normalized = tuple(dict.fromkeys(_normal_symbol(symbol) for symbol in symbols))
        if not normalized:
            return ()
        placeholders = ", ".join("?" for _ in normalized)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM refresh_queue
                 WHERE catalog_version = ?
                   AND configuration_version = ?
                   AND symbol IN ({placeholders})
                """,
                (catalog_version, configuration_version, *normalized),
            ).fetchall()
        states = [self._row_to_state(row) for row in rows]
        by_symbol = {state.symbol: state for state in states}
        missing = [symbol for symbol in normalized if symbol not in by_symbol]
        states.sort(
            key=lambda state: (
                state.last_phase_two_at is not None,
                state.last_phase_two_at
                or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                -(state.previous_rank if state.previous_rank is not None else float("-inf")),
                state.symbol,
            )
        )
        return tuple(state.symbol for state in states) + tuple(missing)

    def defer(
        self,
        symbol: str,
        *,
        until: dt.datetime,
        reason: str,
        now: dt.datetime | None = None,
    ) -> None:
        if not reason.strip():
            raise ValueError("a deferred refresh requires a reason")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE refresh_queue
                   SET next_due_at = ?, deferred_reason = ?,
                       claimed_until = NULL, claim_owner = NULL, updated_at = ?
                 WHERE symbol = ?
                """,
                (
                    _iso(_utc(until)),
                    reason.strip(),
                    _iso(_utc(now)),
                    _normal_symbol(symbol),
                ),
            )

    def release_claims(self, *, owner: str) -> int:
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE refresh_queue
                   SET claimed_until = NULL, claim_owner = NULL,
                       updated_at = ?
                 WHERE claim_owner = ?
                """,
                (_iso(_utc()), owner),
            )
            return int(result.rowcount)

    def due(self, **kwargs: Any) -> tuple[RefreshState, ...]:
        return self.select_due(**kwargs)


class SQLiteObservationCache:
    """SQLite raw-observation store with atomic writes and quarantine."""

    def __init__(self, path: Path) -> None:
        self.path = _sqlite_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.refresh_queue = FairRefreshQueue(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS observations (
                    symbol TEXT NOT NULL,
                    observation_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    catalog_version TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    PRIMARY KEY(symbol, observation_key)
                );
                """
            )
            # SQLite cannot index a JSON field portably across old state files;
            # add a real expiry column if the table was created by an earlier
            # development version.
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(observations)")
            }
            if "expires_at" not in columns:
                connection.execute("ALTER TABLE observations ADD COLUMN expires_at TEXT")
                connection.execute(
                    "UPDATE observations SET expires_at = json_extract(envelope_json, '$.expires_at')"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_observations_expiry "
                "ON observations(expires_at)"
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cache_quarantine (
                    quarantine_id TEXT PRIMARY KEY,
                    symbol TEXT,
                    observation_key TEXT,
                    raw_record TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    quarantined_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_observations_symbol
                    ON observations(symbol);
                """
            )

    def seed_refresh(
        self,
        symbols: Iterable[str],
        *,
        catalog_version: str,
        configuration_version: str,
        now: dt.datetime,
        interval: dt.timedelta = dt.timedelta(minutes=30),
        estimated_request_cost: int = 1,
    ) -> None:
        self.refresh_queue.seed(
            symbols,
            catalog_version=catalog_version,
            configuration_version=configuration_version,
            now=now,
            interval=interval,
            estimated_request_cost=estimated_request_cost,
        )

    def read_many(
        self,
        symbols: Iterable[str],
        *,
        now: dt.datetime | None = None,
        session_date: dt.date | None = None,
        catalog_version: str | None = None,
        configuration_version: str | None = None,
        include_expired: bool = False,
    ) -> Mapping[str, tuple[RawObservation, ...]]:
        normalized = tuple(dict.fromkeys(_normal_symbol(symbol) for symbol in symbols))
        result: dict[str, list[RawObservation]] = {symbol: [] for symbol in normalized}
        if not normalized:
            return {symbol: tuple(items) for symbol, items in result.items()}
        current = _utc(now)
        date = session_date or current.date()
        placeholders = ",".join("?" for _ in normalized)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM observations WHERE symbol IN ({placeholders})",
                normalized,
            ).fetchall()
            for row in rows:
                try:
                    raw = {
                        "symbol": row["symbol"],
                        "key": row["observation_key"],
                        "payload": json.loads(row["payload_json"]),
                        "envelope": json.loads(row["envelope_json"]),
                        "catalog_version": row["catalog_version"],
                    }
                    observation = RawObservation.from_record(raw)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._quarantine_row(
                        connection,
                        row,
                        raw_record=(
                            f"payload={row['payload_json']!r}; "
                            f"envelope={row['envelope_json']!r}"
                        ),
                        reason=f"corrupt observation: {exc}",
                    )
                    continue
                if catalog_version is not None and observation.catalog_version != catalog_version:
                    continue
                if (
                    configuration_version is not None
                    and observation.configuration_version != configuration_version
                ):
                    continue
                if not include_expired and not observation.fresh(
                    now=current, session_date=date
                ):
                    continue
                result[observation.symbol].append(observation)
        return {
            symbol: tuple(sorted(items, key=lambda item: (item.key, item.observed_at)))
            for symbol, items in result.items()
        }

    def _quarantine_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        raw_record: str,
        reason: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO cache_quarantine(
                quarantine_id, symbol, observation_key, raw_record, reason, quarantined_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                row["symbol"],
                row["observation_key"],
                raw_record,
                reason,
                _iso(_utc()),
            ),
        )
        connection.execute(
            "DELETE FROM observations WHERE symbol = ? AND observation_key = ?",
            (row["symbol"], row["observation_key"]),
        )

    def write_batch(self, updates: Iterable[RawObservation]) -> None:
        updates = tuple(updates)
        # Validate the entire batch before opening the transaction.  A single
        # quote or malformed payload must not result in a partially published
        # cache batch.
        for update in updates:
            if not isinstance(update, RawObservation):
                raise TypeError("write_batch accepts RawObservation values")
            json.dumps(update.to_record(), sort_keys=True)
        with self._connect() as connection:
            now = _iso(_utc())
            for update in updates:
                payload_json = json.dumps(_jsonable(update.payload), sort_keys=True)
                envelope_json = json.dumps(update.envelope.to_record(), sort_keys=True)
                connection.execute(
                    """
                    INSERT INTO observations(
                        symbol, observation_key, payload_json, envelope_json,
                        catalog_version, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, observation_key) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        envelope_json = excluded.envelope_json,
                        catalog_version = excluded.catalog_version,
                        updated_at = excluded.updated_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        update.symbol,
                        update.key,
                        payload_json,
                        envelope_json,
                        update.catalog_version,
                        now,
                        _iso(update.envelope.expires_at),
                    ),
                )

    def due(
        self,
        *,
        now: dt.datetime,
        limit: int,
        catalog_version: str = "unknown",
        configuration_version: str = "unknown",
    ) -> tuple[RefreshState, ...]:
        return self.refresh_queue.select_due(
            now=now,
            limit=limit,
            catalog_version=catalog_version,
            configuration_version=configuration_version,
        )


class JsonlObservationCache:
    """Migration implementation for raw JSONL observations.

    It reads the generic format emitted by this class and the legacy IVStore
    ``meta``/``on`` format.  It is intentionally less capable than SQLite;
    production workers should use :class:`SQLiteObservationCache` once the
    artifact has been migrated.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.refresh_queue = FairRefreshQueue(self.root / "refresh.sqlite3")

    def _path(self, symbol: str) -> Path:
        clean = _normal_symbol(symbol)
        clean = "".join(c for c in clean if c.isalnum() or c in "._-")
        if not clean:
            raise ValueError("not a cacheable symbol")
        return self.root / f"{clean}.jsonl"

    def read_many(self, symbols: Iterable[str], **kwargs: Any) -> Mapping[str, tuple[RawObservation, ...]]:
        now = _utc(kwargs.pop("now", None))
        session_date = kwargs.pop("session_date", None) or now.date()
        catalog_version = kwargs.pop("catalog_version", None)
        configuration_version = kwargs.pop("configuration_version", None)
        include_expired = bool(kwargs.pop("include_expired", False))
        if kwargs:
            raise TypeError(f"unexpected cache arguments: {', '.join(kwargs)}")
        output: dict[str, list[RawObservation]] = {}
        for symbol in dict.fromkeys(_normal_symbol(item) for item in symbols):
            output[symbol] = []
            path = self._path(symbol)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            legacy_envelope: ObservationEnvelope | None = None
            legacy_catalog = "unknown"
            for line in lines:
                try:
                    record = json.loads(line)
                    if not isinstance(record, Mapping):
                        continue
                    if isinstance(record.get("meta"), Mapping):
                        raw = record["meta"].get("envelope")
                        if isinstance(raw, Mapping):
                            with contextlib.suppress(KeyError, TypeError, ValueError):
                                legacy_envelope = ObservationEnvelope.from_record(raw)
                        legacy_catalog = str(record["meta"].get("catalog_version", "unknown"))
                        continue
                    if "observation" in record:
                        observation = RawObservation.from_record(record["observation"])
                    elif legacy_envelope is not None and "on" in record and "iv" in record:
                        # Legacy IVStore bars are raw inputs, not a derived rank.
                        observation = RawObservation(
                            symbol=symbol,
                            key="iv-history",
                            payload={"on": str(record["on"]), "iv": str(record["iv"])},
                            envelope=legacy_envelope,
                            catalog_version=legacy_catalog,
                        )
                    else:
                        continue
                    if catalog_version is not None and observation.catalog_version != catalog_version:
                        continue
                    if (
                        configuration_version is not None
                        and observation.configuration_version != configuration_version
                    ):
                        continue
                    if include_expired or observation.fresh(now=now, session_date=session_date):
                        output[symbol].append(observation)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
        return {symbol: tuple(items) for symbol, items in output.items()}

    def write_batch(self, updates: Iterable[RawObservation]) -> None:
        grouped: dict[str, list[RawObservation]] = defaultdict(list)
        for update in updates:
            if not isinstance(update, RawObservation):
                raise TypeError("write_batch accepts RawObservation values")
            json.dumps(update.to_record(), sort_keys=True)
            grouped[update.symbol].append(update)
        for symbol, items in grouped.items():
            path = self._path(symbol)
            lines = [
                json.dumps({"observation": item.to_record()}, sort_keys=True)
                for item in items
            ]
            handle, temporary = tempfile.mkstemp(
                dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write("\n".join(lines) + "\n")
                os.replace(temporary, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise

    def due(self, **kwargs: Any) -> tuple[RefreshState, ...]:
        return self.refresh_queue.select_due(**kwargs)


# Both spellings occur in operator notes; keep the migration adapter easy to
# discover without creating a second implementation.
JSONLObservationCache = JsonlObservationCache
