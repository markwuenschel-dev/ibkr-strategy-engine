"""Durable, immutable scan snapshots and independent claim ownership.

The live universe scanner predates the unattended cycle and persists one
mutable JSON book.  This module is the recovery-safe persistence boundary for
the next cycle: a scan is published once, the latest pointer is replaceable,
and logical ownership lives in a different store entirely.

There are three deliberately separate objects here:

* :class:`ScanBookSnapshot` is a frozen, manifest-bound description of one
  scan.  Its rows and diagnostic mappings are recursively frozen too, so a
  caller cannot mutate the object after it has been admitted for publication.
* :class:`ScanBookSnapshotStore` writes snapshot content under an immutable
  scan id and advances a small atomic latest pointer.  Publishing a newer
  scan never edits an older scan and never touches claims.
* :class:`ClaimLedger` is a versioned compare-and-set ledger keyed by the
  logical claim key (normally an underlying symbol).  Each mutation is an
  immutable event; a current pointer is only a rebuildable index.  A crash
  between those two publications therefore cannot erase ownership.

The scanner can wrap its existing ``ScanBook``/row records by passing their
JSON-shaped records to ``ScanBookSnapshot``.  No scheduler, broker, reviewer,
or strategy imports are required here.
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Self
from uuid import uuid4

from ..errors import EngineError

__all__ = [
    "CLAIM_SCHEMA",
    "LATEST_POINTER_SCHEMA",
    "SNAPSHOT_SCHEMA",
    "ClaimConflict",
    "ClaimCorrupt",
    "ClaimLedger",
    "ClaimRecord",
    "ClaimState",
    "CoverageAdmission",
    "PhaseCoverage",
    "ObservationAges",
    "ScanBookSnapshot",
    "ScanBookSnapshotStore",
    "SnapshotAdmission",
    "SnapshotAdmissionResult",
    "SnapshotCorrupt",
    "SnapshotError",
    "ImmutableSnapshotError",
]


SNAPSHOT_SCHEMA = "ibkr.scanbook.snapshot/1"
LATEST_POINTER_SCHEMA = "ibkr.scanbook.latest/1"
CLAIM_SCHEMA = "ibkr.scanbook.claim/1"
_CURRENT_POINTER_SCHEMA = "ibkr.scanbook.claim-current/1"
_DIGEST_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,199}$")


class SnapshotError(EngineError):
    """Base for deliberate snapshot persistence and admission failures."""


class ImmutableSnapshotError(SnapshotError):
    """A scan id was published before with different content."""


class SnapshotCorrupt(SnapshotError):
    """A snapshot or latest pointer cannot be trusted."""


class ClaimCorrupt(SnapshotError):
    """A claim event stream is malformed or has a version gap."""


class ClaimConflict(SnapshotError):
    """A compare-and-set observed a different owner or version."""

    def __init__(
        self,
        message: str,
        *,
        key: str,
        expected_version: int,
        actual_version: int,
        current: "ClaimRecord | None",
    ) -> None:
        super().__init__(message)
        self.key = key
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.current = current


def _utc(value: dt.datetime, *, name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(dt.UTC)


def _digest(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.strip()):
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return value.strip().lower().removeprefix("sha256:")


def _identifier(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value.strip()):
        raise ValueError(f"{name} must be a non-empty safe identifier")
    return value.strip()


def _freeze(value: Any) -> Any:
    """Recursively turn JSON-shaped data into immutable values."""

    if isinstance(value, Mapping):
        frozen = {str(key): _freeze(item) for key, item in value.items()}
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON data may not contain NaN or infinity")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"value of type {type(value).__name__} is not JSON-shaped")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical_bytes(record: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                _thaw(record),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"record is not canonical JSON: {exc}") from exc


def _payload_digest(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(record)).hexdigest()


def _read_json(path: Path, *, error_type: type[SnapshotError], label: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        record = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise error_type(f"{label} is unreadable: {path}") from exc
    if not isinstance(record, dict):
        raise error_type(f"{label} is not a JSON object: {path}")
    return record


def _atomic_write_json(path: Path, record: Mapping[str, Any]) -> None:
    """Publish a replaceable JSON pointer with flush-before-replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_bytes(record)
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _publish_immutable(path: Path, payload: bytes, *, error_type: type[SnapshotError]) -> None:
    """Atomically publish *path* without allowing a different overwrite.

    A hard-link from a fully flushed temporary file gives a no-replace commit
    on both Windows and POSIX.  If a platform disallows hard links, Windows'
    no-replace ``rename`` is the safe fallback.  Existing identical content is
    idempotent; different content for the same identity is corruption.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = path.read_bytes()
            if existing != payload:
                raise error_type(f"immutable publication conflict at {path}")
        except OSError as exc:
            if os.name != "nt" or exc.errno not in {
                errno.EACCES,
                errno.EPERM,
                errno.ENOTSUP,
            }:
                raise
            # On Windows os.rename fails rather than replacing an existing
            # destination.  It is therefore a no-replace atomic publication.
            try:
                os.rename(temporary, path)
            except FileExistsError:
                existing = path.read_bytes()
                if existing != payload:
                    raise error_type(f"immutable publication conflict at {path}")
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


@dataclass(frozen=True)
class PhaseCoverage:
    """Coverage accounting for one scanner phase or shard."""

    expected: int
    completed: int
    deferred: int = 0
    unavailable: int = 0
    required: bool = True

    def __post_init__(self) -> None:
        for name in ("expected", "completed", "deferred", "unavailable"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"phase coverage {name} must be a non-negative integer")
        if self.completed + self.deferred + self.unavailable > self.expected:
            raise ValueError("phase coverage outcomes exceed expected work")

    @property
    def complete(self) -> bool:
        return (
            self.expected > 0
            and self.completed == self.expected
            and self.deferred == 0
            and self.unavailable == 0
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "expected": self.expected,
            "completed": self.completed,
            "deferred": self.deferred,
            "unavailable": self.unavailable,
            "required": self.required,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Self:
        return cls(
            expected=int(record["expected"]),
            completed=int(record["completed"]),
            deferred=int(record.get("deferred", 0)),
            unavailable=int(record.get("unavailable", 0)),
            required=bool(record.get("required", True)),
        )


@dataclass(frozen=True)
class ObservationAges:
    """Age, in seconds at publication, of the oldest and newest observation."""

    oldest_seconds: float
    newest_seconds: float

    def __post_init__(self) -> None:
        for name in ("oldest_seconds", "newest_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} must be numeric")
            if not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.oldest_seconds < self.newest_seconds:
            raise ValueError("oldest observation cannot be newer than newest observation")

    def to_record(self) -> dict[str, float]:
        return {
            "oldest_seconds": float(self.oldest_seconds),
            "newest_seconds": float(self.newest_seconds),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Self:
        return cls(
            oldest_seconds=float(record["oldest_seconds"]),
            newest_seconds=float(record["newest_seconds"]),
        )


class SnapshotAdmission(str, Enum):
    """Why a latest snapshot can or cannot feed an entry pass."""

    ACCEPTED = "ACCEPTED"
    MISSING = "MISSING"
    CORRUPT = "CORRUPT"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    FUTURE = "FUTURE"
    STALE = "STALE"
    MANIFEST_MISMATCH = "MANIFEST_MISMATCH"
    INCOMPLETE = "INCOMPLETE"
    POINTER_MISMATCH = "POINTER_MISMATCH"


class CoverageAdmission(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class SnapshotAdmissionResult:
    status: SnapshotAdmission
    snapshot: "ScanBookSnapshot | None" = None
    detail: str = ""

    @property
    def entry_admissible(self) -> bool:
        return self.status is SnapshotAdmission.ACCEPTED and self.snapshot is not None

    @property
    def diagnostic_only(self) -> bool:
        return self.snapshot is not None and not self.entry_admissible


@dataclass(frozen=True)
class ScanBookSnapshot:
    """An immutable, manifest-bound snapshot of one scan cycle.

    Rows are JSON-shaped records and must contain a unique ``symbol`` field.
    They intentionally remain opaque to this persistence layer: the current
    scanner's rows and a future indexed scanner can both be adapted without
    making the scheduler import strategy internals.
    """

    scan_id: str
    session_id: str
    session_date: dt.date
    generated_at: dt.datetime
    catalog_hash: str
    policy_hash: str
    calendar_hash: str
    config_hash: str
    expected_symbols: int
    evaluated_symbols: int
    deferred_symbols: int
    unavailable_symbols: int
    rows: tuple[Mapping[str, Any], ...]
    phase_coverage: Mapping[str, PhaseCoverage]
    observation_ages: ObservationAges
    pacing_snapshot: Mapping[str, Any]
    cycle_state: Mapping[str, Any]
    shard_state: Mapping[str, Any]
    tick_id: str | None = None
    attempt_id: str | None = None
    version: str = SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "scan_id", _identifier(self.scan_id, name="scan_id"))
        object.__setattr__(self, "session_id", _identifier(self.session_id, name="session_id"))
        if self.tick_id is not None:
            object.__setattr__(self, "tick_id", _identifier(self.tick_id, name="tick_id"))
        if self.attempt_id is not None:
            object.__setattr__(self, "attempt_id", _identifier(self.attempt_id, name="attempt_id"))
        if self.version != SNAPSHOT_SCHEMA:
            raise ValueError(f"unsupported snapshot version {self.version!r}")
        if not isinstance(self.session_date, dt.date) or isinstance(
            self.session_date, dt.datetime
        ):
            raise ValueError("session_date must be a date")
        object.__setattr__(self, "generated_at", _utc(self.generated_at, name="generated_at"))
        for name in ("catalog_hash", "policy_hash", "calendar_hash", "config_hash"):
            object.__setattr__(self, name, _digest(getattr(self, name), name=name))
        for name in (
            "expected_symbols",
            "evaluated_symbols",
            "deferred_symbols",
            "unavailable_symbols",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.expected_symbols <= 0:
            raise ValueError("expected_symbols must be positive")
        if self.evaluated_symbols + self.deferred_symbols + self.unavailable_symbols > self.expected_symbols:
            raise ValueError("snapshot coverage outcomes exceed expected symbols")

        frozen_rows = tuple(_freeze(row) for row in self.rows)
        symbols: set[str] = set()
        for row in frozen_rows:
            if not isinstance(row, Mapping):
                raise ValueError("snapshot rows must be mappings")
            raw_symbol = row.get("symbol")
            if not isinstance(raw_symbol, str) or not raw_symbol.strip():
                raise ValueError("every snapshot row must contain a symbol")
            symbol = raw_symbol.strip().upper()
            if symbol in symbols:
                raise ValueError(f"snapshot contains duplicate symbol {symbol}")
            symbols.add(symbol)
        if len(frozen_rows) > self.expected_symbols:
            raise ValueError("snapshot rows exceed expected_symbols")
        object.__setattr__(self, "rows", frozen_rows)

        phases: dict[str, PhaseCoverage] = {}
        for name, phase in self.phase_coverage.items():
            phase_name = str(name).strip()
            if not phase_name:
                raise ValueError("phase coverage names must be non-empty")
            phases[phase_name] = (
                phase if isinstance(phase, PhaseCoverage) else PhaseCoverage.from_record(phase)
            )
        if not any(phase.required for phase in phases.values()):
            raise ValueError("at least one required scan phase is necessary")
        object.__setattr__(self, "phase_coverage", MappingProxyType(phases))
        object.__setattr__(self, "pacing_snapshot", _freeze(self.pacing_snapshot))
        object.__setattr__(self, "cycle_state", _freeze(self.cycle_state))
        object.__setattr__(self, "shard_state", _freeze(self.shard_state))
        # Validate the complete record now, not only at file publication.
        _canonical_bytes(self.to_record())

    @property
    def coverage(self) -> CoverageAdmission:
        return (
            CoverageAdmission.COMPLETE
            if self.coverage_complete
            else CoverageAdmission.INCOMPLETE
        )

    @property
    def coverage_complete(self) -> bool:
        return (
            len(self.rows) == self.expected_symbols
            and self.evaluated_symbols == self.expected_symbols
            and self.deferred_symbols == 0
            and self.unavailable_symbols == 0
            and all(
                phase.complete
                for phase in self.phase_coverage.values()
                if phase.required
            )
        )

    def admit(
        self,
        *,
        session_date: dt.date,
        now: dt.datetime,
        max_age: dt.timedelta,
        catalog_hash: str,
        policy_hash: str,
        calendar_hash: str,
        config_hash: str,
    ) -> SnapshotAdmissionResult:
        """Evaluate the complete entry-admission contract.

        A partial book is returned with ``snapshot`` attached for diagnostics,
        but ``entry_admissible`` remains false.  The order is intentional:
        identity and manifest failures are reported before freshness, and a
        current but incomplete book is never mistaken for a stale one.
        """

        if not isinstance(session_date, dt.date) or isinstance(session_date, dt.datetime):
            raise ValueError("session_date must be a date")
        now_utc = _utc(now, name="now")
        if not isinstance(max_age, dt.timedelta) or max_age <= dt.timedelta(0):
            raise ValueError("max_age must be positive")
        if self.session_date != session_date:
            return SnapshotAdmissionResult(
                SnapshotAdmission.SESSION_MISMATCH,
                self,
                "snapshot session date does not match the requested session",
            )
        expected = {
            "catalog_hash": _digest(catalog_hash, name="catalog_hash"),
            "policy_hash": _digest(policy_hash, name="policy_hash"),
            "calendar_hash": _digest(calendar_hash, name="calendar_hash"),
            "config_hash": _digest(config_hash, name="config_hash"),
        }
        actual = {
            name: getattr(self, name)
            for name in ("catalog_hash", "policy_hash", "calendar_hash", "config_hash")
        }
        if actual != expected:
            return SnapshotAdmissionResult(
                SnapshotAdmission.MANIFEST_MISMATCH,
                self,
                "snapshot manifest differs from the active policy/catalog/calendar/config",
            )
        age = now_utc - self.generated_at
        if age < dt.timedelta(0):
            return SnapshotAdmissionResult(
                SnapshotAdmission.FUTURE,
                self,
                "snapshot generated in the future of the binding clock",
            )
        if age > max_age:
            return SnapshotAdmissionResult(
                SnapshotAdmission.STALE,
                self,
                f"snapshot age {age} exceeds {max_age}",
            )
        if not self.coverage_complete:
            return SnapshotAdmissionResult(
                SnapshotAdmission.INCOMPLETE,
                self,
                "incomplete or deferred coverage is diagnostic-only",
            )
        return SnapshotAdmissionResult(SnapshotAdmission.ACCEPTED, self, "accepted")

    def to_record(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "scan_id": self.scan_id,
            "session_id": self.session_id,
            "session_date": self.session_date.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "tick_id": self.tick_id,
            "attempt_id": self.attempt_id,
            "catalog_hash": self.catalog_hash,
            "policy_hash": self.policy_hash,
            "calendar_hash": self.calendar_hash,
            "config_hash": self.config_hash,
            "expected_symbols": self.expected_symbols,
            "evaluated_symbols": self.evaluated_symbols,
            "deferred_symbols": self.deferred_symbols,
            "unavailable_symbols": self.unavailable_symbols,
            "rows": [_thaw(row) for row in self.rows],
            "phase_coverage": {
                name: phase.to_record() for name, phase in self.phase_coverage.items()
            },
            "observation_ages": self.observation_ages.to_record(),
            "pacing_snapshot": _thaw(self.pacing_snapshot),
            "cycle_state": _thaw(self.cycle_state),
            "shard_state": _thaw(self.shard_state),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Self:
        try:
            return cls(
                version=str(record["version"]),
                scan_id=str(record["scan_id"]),
                session_id=str(record["session_id"]),
                session_date=dt.date.fromisoformat(str(record["session_date"])),
                generated_at=dt.datetime.fromisoformat(str(record["generated_at"])),
                tick_id=(str(record["tick_id"]) if record.get("tick_id") else None),
                attempt_id=(str(record["attempt_id"]) if record.get("attempt_id") else None),
                catalog_hash=str(record["catalog_hash"]),
                policy_hash=str(record["policy_hash"]),
                calendar_hash=str(record["calendar_hash"]),
                config_hash=str(record["config_hash"]),
                expected_symbols=int(record["expected_symbols"]),
                evaluated_symbols=int(record["evaluated_symbols"]),
                deferred_symbols=int(record["deferred_symbols"]),
                unavailable_symbols=int(record["unavailable_symbols"]),
                rows=tuple(record.get("rows", [])),
                phase_coverage={
                    str(name): PhaseCoverage.from_record(value)
                    for name, value in (record.get("phase_coverage") or {}).items()
                },
                observation_ages=ObservationAges.from_record(record["observation_ages"]),
                pacing_snapshot=record.get("pacing_snapshot") or {},
                cycle_state=record.get("cycle_state") or {},
                shard_state=record.get("shard_state") or {},
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SnapshotCorrupt("snapshot record violates the immutable schema") from exc


@dataclass(frozen=True)
class _LatestPointer:
    session_date: dt.date
    scan_id: str
    snapshot_sha256: str
    published_at: dt.datetime

    def to_record(self) -> dict[str, Any]:
        return {
            "version": LATEST_POINTER_SCHEMA,
            "session_date": self.session_date.isoformat(),
            "scan_id": self.scan_id,
            "snapshot_sha256": self.snapshot_sha256,
            "published_at": self.published_at.isoformat(),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Self:
        if record.get("version") != LATEST_POINTER_SCHEMA:
            raise SnapshotCorrupt("latest pointer has an unsupported version")
        try:
            return cls(
                session_date=dt.date.fromisoformat(str(record["session_date"])),
                scan_id=_identifier(str(record["scan_id"]), name="latest scan_id"),
                snapshot_sha256=_digest(str(record["snapshot_sha256"]), name="snapshot_sha256"),
                published_at=_utc(
                    dt.datetime.fromisoformat(str(record["published_at"])),
                    name="published_at",
                ),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SnapshotCorrupt("latest pointer is malformed") from exc


class ScanBookSnapshotStore:
    """Filesystem store for immutable snapshots and a replaceable latest pointer."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.snapshot_root = self.root / "scanbook" / "snapshots"
        self.pointer_root = self.root / "scanbook" / "latest"

    def snapshot_path(self, snapshot: ScanBookSnapshot | str, session_date: dt.date | None = None) -> Path:
        if isinstance(snapshot, ScanBookSnapshot):
            scan_id = snapshot.scan_id
            session = snapshot.session_date
        else:
            scan_id = _identifier(snapshot, name="scan_id")
            if session_date is None:
                raise ValueError("session_date is required when addressing a scan id")
            session = session_date
        return self.snapshot_root / session.isoformat() / f"{scan_id}.json"

    def latest_pointer_path(self, session_date: dt.date) -> Path:
        return self.pointer_root / f"{session_date.isoformat()}.json"

    def publish(self, snapshot: ScanBookSnapshot, *, make_latest: bool = True) -> Path:
        """Publish a snapshot once, optionally advancing its session pointer."""

        path = self.snapshot_path(snapshot)
        payload = _canonical_bytes(snapshot.to_record()) + b"\n"
        _publish_immutable(path, payload, error_type=ImmutableSnapshotError)
        if make_latest:
            pointer = _LatestPointer(
                session_date=snapshot.session_date,
                scan_id=snapshot.scan_id,
                snapshot_sha256=hashlib.sha256(payload).hexdigest(),
                published_at=dt.datetime.now(dt.UTC),
            )
            _atomic_write_json(self.latest_pointer_path(snapshot.session_date), pointer.to_record())
        return path

    def read_snapshot(self, scan_id: str, session_date: dt.date) -> ScanBookSnapshot:
        path = self.snapshot_path(scan_id, session_date)
        record = _read_json(path, error_type=SnapshotCorrupt, label="scan snapshot")
        snapshot = ScanBookSnapshot.from_record(record)
        if snapshot.scan_id != scan_id or snapshot.session_date != session_date:
            raise SnapshotCorrupt("snapshot identity does not match its path")
        return snapshot

    def read_latest(self, session_date: dt.date) -> ScanBookSnapshot | None:
        path = self.latest_pointer_path(session_date)
        if not path.exists():
            return None
        pointer = _LatestPointer.from_record(
            _read_json(path, error_type=SnapshotCorrupt, label="latest pointer")
        )
        if pointer.session_date != session_date:
            raise SnapshotCorrupt("latest pointer session date does not match its path")
        snapshot = self.read_snapshot(pointer.scan_id, session_date)
        payload = _canonical_bytes(snapshot.to_record()) + b"\n"
        if hashlib.sha256(payload).hexdigest() != pointer.snapshot_sha256:
            raise SnapshotCorrupt("latest pointer does not identify the published snapshot bytes")
        return snapshot

    def admit_latest(
        self,
        *,
        session_date: dt.date,
        now: dt.datetime,
        max_age: dt.timedelta,
        catalog_hash: str,
        policy_hash: str,
        calendar_hash: str,
        config_hash: str,
    ) -> SnapshotAdmissionResult:
        try:
            snapshot = self.read_latest(session_date)
        except SnapshotCorrupt as exc:
            return SnapshotAdmissionResult(SnapshotAdmission.CORRUPT, None, str(exc))
        if snapshot is None:
            return SnapshotAdmissionResult(
                SnapshotAdmission.MISSING,
                None,
                f"no latest snapshot exists for {session_date.isoformat()}",
            )
        return snapshot.admit(
            session_date=session_date,
            now=now,
            max_age=max_age,
            catalog_hash=catalog_hash,
            policy_hash=policy_hash,
            calendar_hash=calendar_hash,
            config_hash=config_hash,
        )

    def list_snapshots(self, session_date: dt.date) -> tuple[ScanBookSnapshot, ...]:
        directory = self.snapshot_root / session_date.isoformat()
        if not directory.exists():
            return ()
        snapshots: list[ScanBookSnapshot] = []
        for path in sorted(directory.glob("*.json")):
            record = _read_json(path, error_type=SnapshotCorrupt, label="scan snapshot")
            snapshot = ScanBookSnapshot.from_record(record)
            if snapshot.session_date != session_date:
                raise SnapshotCorrupt("snapshot session does not match its directory")
            snapshots.append(snapshot)
        return tuple(snapshots)


class ClaimState(str, Enum):
    CLAIMED = "CLAIMED"
    RELEASED = "RELEASED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class ClaimRecord:
    """One immutable version in the claim event stream."""

    key: str
    version: int
    mutation_id: str
    claim_id: str
    scan_id: str
    symbol: str
    owner_id: str
    state: ClaimState
    changed_at: dt.datetime
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _identifier(self.key, name="claim key"))
        object.__setattr__(self, "mutation_id", _identifier(self.mutation_id, name="mutation_id"))
        object.__setattr__(self, "claim_id", _identifier(self.claim_id, name="claim_id"))
        object.__setattr__(self, "scan_id", _identifier(self.scan_id, name="claim scan_id"))
        object.__setattr__(self, "owner_id", _identifier(self.owner_id, name="claim owner_id"))
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("claim symbol must be non-empty")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version <= 0:
            raise ValueError("claim version must be positive")
        object.__setattr__(self, "changed_at", _utc(self.changed_at, name="changed_at"))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    def to_record(self) -> dict[str, Any]:
        return {
            "version": CLAIM_SCHEMA,
            "key": self.key,
            "record_version": self.version,
            "mutation_id": self.mutation_id,
            "claim_id": self.claim_id,
            "scan_id": self.scan_id,
            "symbol": self.symbol,
            "owner_id": self.owner_id,
            "state": self.state.value,
            "changed_at": self.changed_at.isoformat(),
            "metadata": _thaw(self.metadata),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Self:
        if record.get("version") != CLAIM_SCHEMA:
            raise ClaimCorrupt("claim event has an unsupported version")
        try:
            return cls(
                key=str(record["key"]),
                version=int(record["record_version"]),
                mutation_id=str(record["mutation_id"]),
                claim_id=str(record["claim_id"]),
                scan_id=str(record["scan_id"]),
                symbol=str(record["symbol"]),
                owner_id=str(record["owner_id"]),
                state=ClaimState(str(record["state"])),
                changed_at=dt.datetime.fromisoformat(str(record["changed_at"])),
                metadata=record.get("metadata") or {},
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ClaimCorrupt("claim event is malformed") from exc


@contextmanager
def _claim_lock(path: Path) -> Iterator[None]:
    """Hold a cross-process lock for one claim key."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - exercised on POSIX CI, not Windows runtime
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised on POSIX CI, not Windows runtime
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class ClaimLedger:
    """A durable per-key CAS ledger independent of scan snapshot files."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root) / "claims"
        self.events_root = self.root / "events"
        self.current_root = self.root / "current"
        self.locks_root = self.root / "locks"

    @staticmethod
    def normalize_key(key: str) -> str:
        value = str(key).strip().upper()
        if not value or not _SAFE_ID_RE.fullmatch(value):
            raise ValueError("claim key must be a safe non-empty identifier")
        return value

    def _key_hash(self, key: str) -> str:
        return hashlib.sha256(self.normalize_key(key).encode("utf-8")).hexdigest()

    def _event_dir(self, key: str) -> Path:
        return self.events_root / self._key_hash(key)

    def _current_path(self, key: str) -> Path:
        return self.current_root / f"{self._key_hash(key)}.json"

    def _lock_path(self, key: str) -> Path:
        return self.locks_root / f"{self._key_hash(key)}.lock"

    def _history_unlocked(self, key: str) -> tuple[ClaimRecord, ...]:
        normalized = self.normalize_key(key)
        directory = self._event_dir(normalized)
        if not directory.exists():
            return ()
        records: list[ClaimRecord] = []
        for path in sorted(directory.glob("*.json")):
            record = ClaimRecord.from_record(
                _read_json(path, error_type=ClaimCorrupt, label="claim event")
            )
            if record.key != normalized:
                raise ClaimCorrupt("claim event key does not match its directory")
            records.append(record)
        records.sort(key=lambda record: record.version)
        for expected, record in enumerate(records, start=1):
            if record.version != expected:
                raise ClaimCorrupt(
                    f"claim {normalized} has a version gap at {expected}"
                )
        return tuple(records)

    def history(self, key: str) -> tuple[ClaimRecord, ...]:
        return self._history_unlocked(key)

    def read(self, key: str) -> ClaimRecord | None:
        records = self._history_unlocked(key)
        if not records:
            pointer = self._current_path(key)
            if pointer.exists():
                raise ClaimCorrupt("claim current pointer exists without an event stream")
            return None
        latest = records[-1]
        pointer_path = self._current_path(key)
        if pointer_path.exists():
            pointer = _read_json(
                pointer_path, error_type=ClaimCorrupt, label="claim current pointer"
            )
            if pointer.get("version") != _CURRENT_POINTER_SCHEMA:
                raise ClaimCorrupt("claim current pointer has an unsupported version")
            if str(pointer.get("key")) != self.normalize_key(key):
                raise ClaimCorrupt("claim current pointer key mismatch")
            if int(pointer.get("record_version", -1)) != latest.version:
                raise ClaimCorrupt("claim current pointer is behind the event stream")
            if str(pointer.get("record_sha256")) != _payload_digest(latest.to_record()):
                raise ClaimCorrupt("claim current pointer digest mismatch")
        # If the process died after the immutable event and before the index,
        # the event is still the source of truth.  repair_current() is explicit
        # so a read remains side-effect free.
        return latest

    def repair_current(self, key: str) -> ClaimRecord | None:
        normalized = self.normalize_key(key)
        with _claim_lock(self._lock_path(normalized)):
            records = self._history_unlocked(normalized)
            if not records:
                return None
            self._publish_current(normalized, records[-1])
            return records[-1]

    def _publish_current(self, key: str, record: ClaimRecord) -> None:
        _atomic_write_json(
            self._current_path(key),
            {
                "version": _CURRENT_POINTER_SCHEMA,
                "key": key,
                "record_version": record.version,
                "record_sha256": _payload_digest(record.to_record()),
                "updated_at": dt.datetime.now(dt.UTC).isoformat(),
            },
        )

    def _commit_unlocked(
        self,
        *,
        key: str,
        current: ClaimRecord | None,
        state: ClaimState,
        owner_id: str,
        claim_id: str,
        scan_id: str,
        symbol: str,
        at: dt.datetime,
        metadata: Mapping[str, Any] | None,
    ) -> ClaimRecord:
        record = ClaimRecord(
            key=key,
            version=(current.version + 1 if current else 1),
            mutation_id=uuid4().hex,
            claim_id=claim_id,
            scan_id=scan_id,
            symbol=symbol.strip().upper(),
            owner_id=owner_id,
            state=state,
            changed_at=at,
            metadata=metadata or {},
        )
        event_path = self._event_dir(key) / f"{record.version:020d}-{record.mutation_id}.json"
        payload = _canonical_bytes(record.to_record()) + b"\n"
        _publish_immutable(event_path, payload, error_type=ClaimCorrupt)
        self._publish_current(key, record)
        return record

    def compare_and_set(
        self,
        key: str,
        *,
        expected_version: int,
        state: ClaimState,
        owner_id: str,
        scan_id: str,
        symbol: str,
        at: dt.datetime,
        claim_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ClaimRecord:
        """Commit one version only if the on-disk version still matches."""

        normalized = self.normalize_key(key)
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if not isinstance(state, ClaimState):
            state = ClaimState(state)
        with _claim_lock(self._lock_path(normalized)):
            current = self.read(normalized)
            actual = current.version if current else 0
            if actual != expected_version:
                raise ClaimConflict(
                    f"claim {normalized} changed from version {expected_version} to {actual}",
                    key=normalized,
                    expected_version=expected_version,
                    actual_version=actual,
                    current=current,
                )
            if state is ClaimState.CLAIMED and current is not None and current.state is ClaimState.CLAIMED:
                raise ClaimConflict(
                    f"claim {normalized} is already owned by {current.owner_id}",
                    key=normalized,
                    expected_version=expected_version,
                    actual_version=actual,
                    current=current,
                )
            if state is not ClaimState.CLAIMED and current is None:
                raise ClaimConflict(
                    f"claim {normalized} has no active ownership to transition",
                    key=normalized,
                    expected_version=expected_version,
                    actual_version=actual,
                    current=None,
                )
            if state is not ClaimState.CLAIMED and current is not None and current.owner_id != owner_id:
                raise ClaimConflict(
                    f"claim {normalized} is owned by {current.owner_id}, not {owner_id}",
                    key=normalized,
                    expected_version=expected_version,
                    actual_version=actual,
                    current=current,
                )
            return self._commit_unlocked(
                key=normalized,
                current=current,
                state=state,
                owner_id=owner_id,
                claim_id=claim_id or (current.claim_id if current else uuid4().hex),
                scan_id=scan_id,
                symbol=symbol,
                at=at,
                metadata=metadata,
            )

    def claim(
        self,
        scan_id: str,
        symbol: str,
        owner_id: str,
        *,
        at: dt.datetime,
        expected_version: int | None = None,
        claim_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        key: str | None = None,
    ) -> ClaimRecord:
        """Claim a symbol, idempotently for the same owner, through CAS."""

        normalized = self.normalize_key(key or symbol)
        scan_id = _identifier(scan_id, name="scan_id")
        owner_id = _identifier(owner_id, name="owner_id")
        with _claim_lock(self._lock_path(normalized)):
            current = self.read(normalized)
            actual = current.version if current else 0
            if expected_version is not None and expected_version != actual:
                raise ClaimConflict(
                    f"claim {normalized} changed from version {expected_version} to {actual}",
                    key=normalized,
                    expected_version=expected_version,
                    actual_version=actual,
                    current=current,
                )
            if current is not None and current.state is ClaimState.CLAIMED:
                if current.owner_id == owner_id:
                    return current
                raise ClaimConflict(
                    f"claim {normalized} is already owned by {current.owner_id}",
                    key=normalized,
                    expected_version=actual,
                    actual_version=actual,
                    current=current,
                )
            return self._commit_unlocked(
                key=normalized,
                current=current,
                state=ClaimState.CLAIMED,
                owner_id=owner_id,
                claim_id=claim_id or uuid4().hex,
                scan_id=scan_id,
                symbol=symbol,
                at=at,
                metadata=metadata,
            )

    def release(
        self,
        key: str,
        *,
        owner_id: str,
        at: dt.datetime,
        expected_version: int,
        scan_id: str,
        symbol: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ClaimRecord:
        return self.compare_and_set(
            key,
            expected_version=expected_version,
            state=ClaimState.RELEASED,
            owner_id=owner_id,
            scan_id=scan_id,
            symbol=symbol,
            at=at,
            metadata=metadata,
        )

    def supersede(
        self,
        key: str,
        *,
        owner_id: str,
        at: dt.datetime,
        expected_version: int,
        scan_id: str,
        symbol: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ClaimRecord:
        return self.compare_and_set(
            key,
            expected_version=expected_version,
            state=ClaimState.SUPERSEDED,
            owner_id=owner_id,
            scan_id=scan_id,
            symbol=symbol,
            at=at,
            metadata=metadata,
        )

    def active(self) -> tuple[ClaimRecord, ...]:
        records: list[ClaimRecord] = []
        if not self.events_root.exists():
            return ()
        for directory in sorted(self.events_root.iterdir()):
            if not directory.is_dir():
                continue
            events = sorted(directory.glob("*.json"))
            if not events:
                continue
            latest = self._history_unlocked(
                ClaimRecord.from_record(
                    _read_json(events[-1], error_type=ClaimCorrupt, label="claim event")
                ).key
            )[-1]
            if latest.state is ClaimState.CLAIMED:
                records.append(latest)
        return tuple(sorted(records, key=lambda record: record.key))
