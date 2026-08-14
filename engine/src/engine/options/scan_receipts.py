"""Durable scan lifecycle receipts and unmatched-scan recovery inspection.

Receipts are individual immutable JSON records rather than an in-place status
file.  A scan therefore has a durable ``SCAN_STARTED`` before work begins and
exactly one terminal ``SCAN_COMPLETED`` or ``SCAN_ABORTED`` record.  Reading an
unmatched start is an explicit recovery condition; absence of a terminal
record is never interpreted as success.

The store also reads an optional legacy JSONL file.  New writers use one file
per receipt so a crash cannot tear a shared append stream, while migrations can
continue to inspect the existing line-oriented evidence.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Self
from uuid import uuid4

from ..errors import EngineError

__all__ = [
    "SCAN_RECEIPT_SCHEMA",
    "ReceiptError",
    "ReceiptCorrupt",
    "ReceiptStateError",
    "ScanReceipt",
    "ScanReceiptKind",
    "ScanReceiptStore",
    "ScanRecoveryState",
]


SCAN_RECEIPT_SCHEMA = "ibkr.scan.receipt/1"


class ReceiptError(EngineError):
    """Base for deliberate receipt persistence failures."""


class ReceiptCorrupt(ReceiptError):
    """A receipt record cannot be trusted."""


class ReceiptStateError(ReceiptError):
    """A receipt would violate the scan lifecycle state machine."""


class ScanReceiptKind(str, Enum):
    SCAN_STARTED = "SCAN_STARTED"
    SCAN_SHARD_COMPLETED = "SCAN_SHARD_COMPLETED"
    SCAN_COMPLETED = "SCAN_COMPLETED"
    SCAN_ABORTED = "SCAN_ABORTED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RECOVERY_CLEARED = "RECOVERY_CLEARED"


_TERMINAL = frozenset({ScanReceiptKind.SCAN_COMPLETED, ScanReceiptKind.SCAN_ABORTED})
_SAFE_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:@+-")


def _utc(value: dt.datetime, *, name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(dt.UTC)


def _identifier(value: str, *, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > 200 or any(char not in _SAFE_ID_CHARS for char in value):
        raise ValueError(f"{name} must be a non-empty safe identifier")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("receipt payload may not contain NaN or infinity")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"receipt payload value {type(value).__name__} is not JSON-shaped")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical_bytes(record: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            _thaw(record),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"receipt is not canonical JSON: {exc}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptCorrupt(f"receipt is unreadable: {path}") from exc
    if not isinstance(record, dict):
        raise ReceiptCorrupt(f"receipt is not a JSON object: {path}")
    return record


def _publish_immutable(path: Path, payload: bytes) -> None:
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
            if path.read_bytes() != payload:
                raise ReceiptCorrupt(f"receipt id already contains different content: {path}")
        except OSError:
            if os.name != "nt":
                raise
            # Windows rename fails instead of replacing an existing path.
            try:
                os.rename(temporary, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise ReceiptCorrupt(
                        f"receipt id already contains different content: {path}"
                    )
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


@dataclass(frozen=True)
class ScanReceipt:
    """One immutable lifecycle event."""

    receipt_id: str
    kind: ScanReceiptKind
    session_id: str
    scan_id: str
    recorded_at: dt.datetime
    tick_id: str | None = None
    attempt_id: str | None = None
    shard_id: str | None = None
    payload: Mapping[str, Any] = MappingProxyType({})
    version: str = SCAN_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_id", _identifier(self.receipt_id, name="receipt_id"))
        object.__setattr__(self, "session_id", _identifier(self.session_id, name="session_id"))
        object.__setattr__(self, "scan_id", _identifier(self.scan_id, name="scan_id"))
        if self.tick_id is not None:
            object.__setattr__(self, "tick_id", _identifier(self.tick_id, name="tick_id"))
        if self.attempt_id is not None:
            object.__setattr__(self, "attempt_id", _identifier(self.attempt_id, name="attempt_id"))
        if self.shard_id is not None:
            object.__setattr__(self, "shard_id", _identifier(self.shard_id, name="shard_id"))
        if not isinstance(self.kind, ScanReceiptKind):
            object.__setattr__(self, "kind", ScanReceiptKind(self.kind))
        if self.version != SCAN_RECEIPT_SCHEMA:
            raise ValueError(f"unsupported scan receipt version {self.version!r}")
        object.__setattr__(self, "recorded_at", _utc(self.recorded_at, name="recorded_at"))
        object.__setattr__(self, "payload", _freeze(self.payload))
        _canonical_bytes(self.to_record())

    def to_record(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "receipt_id": self.receipt_id,
            "kind": self.kind.value,
            "session_id": self.session_id,
            "scan_id": self.scan_id,
            "recorded_at": self.recorded_at.isoformat(),
            "tick_id": self.tick_id,
            "attempt_id": self.attempt_id,
            "shard_id": self.shard_id,
            "payload": _thaw(self.payload),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Self:
        try:
            return cls(
                version=str(record["version"]),
                receipt_id=str(record["receipt_id"]),
                kind=ScanReceiptKind(str(record["kind"])),
                session_id=str(record["session_id"]),
                scan_id=str(record["scan_id"]),
                recorded_at=dt.datetime.fromisoformat(str(record["recorded_at"])),
                tick_id=(str(record["tick_id"]) if record.get("tick_id") else None),
                attempt_id=(str(record["attempt_id"]) if record.get("attempt_id") else None),
                shard_id=(str(record["shard_id"]) if record.get("shard_id") else None),
                payload=record.get("payload") or {},
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ReceiptCorrupt("scan receipt violates its schema") from exc


@dataclass(frozen=True)
class ScanRecoveryState:
    """Derived state for one scan, including whether recovery is required."""

    scan_id: str
    session_id: str
    receipt_ids: tuple[str, ...]
    shard_ids: tuple[str, ...]
    terminal_kind: ScanReceiptKind | None
    recovery_required: bool

    @property
    def unmatched(self) -> bool:
        return self.recovery_required

    @property
    def complete(self) -> bool:
        return self.terminal_kind is ScanReceiptKind.SCAN_COMPLETED


class ScanReceiptStore:
    """Filesystem-backed scan receipt journal with recovery inspection."""

    def __init__(self, root: Path, *, legacy_jsonl: Path | None = None) -> None:
        self.root = Path(root) / "scan-receipts"
        self.receipts_root = self.root / "records"
        self.legacy_jsonl = legacy_jsonl or (self.root / "scan-receipts.jsonl")

    def _path(self, receipt_id: str) -> Path:
        return self.receipts_root / f"{_identifier(receipt_id, name='receipt_id')}.json"

    def _all(self) -> tuple[ScanReceipt, ...]:
        records: list[ScanReceipt] = []
        if self.receipts_root.exists():
            for path in sorted(self.receipts_root.glob("*.json")):
                receipt = ScanReceipt.from_record(_read_json(path))
                if receipt.receipt_id != path.stem:
                    raise ReceiptCorrupt("receipt id does not match its filename")
                records.append(receipt)
        # Read, but never append to, the old line-oriented journal.  A bad line
        # fails closed instead of making an incomplete scan look complete.
        if self.legacy_jsonl.exists():
            try:
                lines = self.legacy_jsonl.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as exc:
                raise ReceiptCorrupt("legacy scan receipt journal is unreadable") from exc
            for number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    receipt = ScanReceipt.from_record(json.loads(line))
                except (json.JSONDecodeError, ReceiptCorrupt) as exc:
                    raise ReceiptCorrupt(
                        f"legacy scan receipt line {number} is malformed"
                    ) from exc
                records.append(receipt)
        by_id: dict[str, ScanReceipt] = {}
        for receipt in records:
            previous = by_id.get(receipt.receipt_id)
            if previous is not None and previous != receipt:
                raise ReceiptCorrupt(
                    f"receipt id {receipt.receipt_id} has conflicting publications"
                )
            by_id[receipt.receipt_id] = receipt
        return tuple(sorted(by_id.values(), key=lambda item: (item.recorded_at, item.receipt_id)))

    def read(self, scan_id: str) -> tuple[ScanReceipt, ...]:
        scan_id = _identifier(scan_id, name="scan_id")
        return tuple(receipt for receipt in self._all() if receipt.scan_id == scan_id)

    def append(
        self,
        kind: ScanReceiptKind,
        *,
        session_id: str,
        scan_id: str,
        recorded_at: dt.datetime,
        tick_id: str | None = None,
        attempt_id: str | None = None,
        shard_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        receipt_id: str | None = None,
    ) -> ScanReceipt:
        """Append one receipt after validating the scan lifecycle."""

        if not isinstance(kind, ScanReceiptKind):
            kind = ScanReceiptKind(kind)
        scan_id = _identifier(scan_id, name="scan_id")
        session_id = _identifier(session_id, name="session_id")
        existing = self.read(scan_id)
        self._validate_transition(existing, kind, session_id=session_id, shard_id=shard_id)
        receipt = ScanReceipt(
            receipt_id=receipt_id or uuid4().hex,
            kind=kind,
            session_id=session_id,
            scan_id=scan_id,
            recorded_at=recorded_at,
            tick_id=tick_id,
            attempt_id=attempt_id,
            shard_id=shard_id,
            payload=payload or {},
        )
        path = self._path(receipt.receipt_id)
        payload_bytes = _canonical_bytes(receipt.to_record()) + b"\n"
        _publish_immutable(path, payload_bytes)
        return receipt

    @staticmethod
    def _validate_transition(
        existing: tuple[ScanReceipt, ...],
        kind: ScanReceiptKind,
        *,
        session_id: str,
        shard_id: str | None,
    ) -> None:
        if not existing:
            if kind is not ScanReceiptKind.SCAN_STARTED:
                raise ReceiptStateError(
                    f"{kind.value} cannot be recorded before SCAN_STARTED"
                )
            return
        if any(item.session_id != session_id for item in existing):
            raise ReceiptStateError("one scan id cannot span multiple session authorities")
        terminal = [item for item in existing if item.kind in _TERMINAL]
        if terminal:
            if kind is terminal[-1].kind:
                # The caller may use an explicit receipt id to replay an
                # already durable terminal event; append() handles byte
                # idempotence below.  A new terminal with a new id is rejected.
                raise ReceiptStateError("a scan already has a terminal receipt")
            raise ReceiptStateError("no receipt may follow a terminal scan receipt")
        if kind is ScanReceiptKind.SCAN_STARTED:
            raise ReceiptStateError("a scan may have only one SCAN_STARTED receipt")
        if kind is ScanReceiptKind.SCAN_SHARD_COMPLETED and not shard_id:
            raise ReceiptStateError("SCAN_SHARD_COMPLETED requires shard_id")
        if kind is ScanReceiptKind.RECOVERY_CLEARED:
            if not any(item.kind is ScanReceiptKind.RECOVERY_REQUIRED for item in existing):
                raise ReceiptStateError("RECOVERY_CLEARED requires RECOVERY_REQUIRED")

    def start(
        self,
        *,
        session_id: str,
        scan_id: str,
        recorded_at: dt.datetime,
        tick_id: str | None = None,
        attempt_id: str | None = None,
        expected_shards: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> ScanReceipt:
        existing = self.read(scan_id)
        if existing:
            first = existing[0]
            if first.kind is ScanReceiptKind.SCAN_STARTED and first.session_id == session_id:
                return first
        body = dict(payload or {})
        if expected_shards is not None:
            if not isinstance(expected_shards, int) or expected_shards <= 0:
                raise ValueError("expected_shards must be positive")
            body["expected_shards"] = expected_shards
        return self.append(
            ScanReceiptKind.SCAN_STARTED,
            session_id=session_id,
            scan_id=scan_id,
            recorded_at=recorded_at,
            tick_id=tick_id,
            attempt_id=attempt_id,
            payload=body,
        )

    def shard_completed(
        self,
        *,
        session_id: str,
        scan_id: str,
        shard_id: str,
        recorded_at: dt.datetime,
        tick_id: str | None = None,
        attempt_id: str | None = None,
        evaluated: int | None = None,
        deferred: int | None = None,
        unavailable: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> ScanReceipt:
        body = dict(payload or {})
        for name, value in (
            ("evaluated", evaluated),
            ("deferred", deferred),
            ("unavailable", unavailable),
        ):
            if value is not None:
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(f"{name} must be a non-negative integer")
                body[name] = value
        return self.append(
            ScanReceiptKind.SCAN_SHARD_COMPLETED,
            session_id=session_id,
            scan_id=scan_id,
            recorded_at=recorded_at,
            tick_id=tick_id,
            attempt_id=attempt_id,
            shard_id=shard_id,
            payload=body,
        )

    def complete(
        self,
        *,
        session_id: str,
        scan_id: str,
        recorded_at: dt.datetime,
        tick_id: str | None = None,
        attempt_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> ScanReceipt:
        return self.append(
            ScanReceiptKind.SCAN_COMPLETED,
            session_id=session_id,
            scan_id=scan_id,
            recorded_at=recorded_at,
            tick_id=tick_id,
            attempt_id=attempt_id,
            payload=payload,
        )

    def abort(
        self,
        *,
        session_id: str,
        scan_id: str,
        recorded_at: dt.datetime,
        reason: str,
        reconciled: bool = False,
        tick_id: str | None = None,
        attempt_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> ScanReceipt:
        if not reason.strip():
            raise ValueError("an aborted scan requires a reason")
        body = dict(payload or {})
        body.update({"reason": reason, "reconciled": reconciled})
        return self.append(
            ScanReceiptKind.SCAN_ABORTED,
            session_id=session_id,
            scan_id=scan_id,
            recorded_at=recorded_at,
            tick_id=tick_id,
            attempt_id=attempt_id,
            payload=body,
        )

    def recovery_required(
        self,
        *,
        session_id: str,
        scan_id: str,
        recorded_at: dt.datetime,
        reason: str,
        payload: Mapping[str, Any] | None = None,
    ) -> ScanReceipt:
        if not reason.strip():
            raise ValueError("recovery_required needs a reason")
        body = dict(payload or {})
        body["reason"] = reason
        return self.append(
            ScanReceiptKind.RECOVERY_REQUIRED,
            session_id=session_id,
            scan_id=scan_id,
            recorded_at=recorded_at,
            payload=body,
        )

    def recovery_cleared(
        self,
        *,
        session_id: str,
        scan_id: str,
        recorded_at: dt.datetime,
        payload: Mapping[str, Any] | None = None,
    ) -> ScanReceipt:
        return self.append(
            ScanReceiptKind.RECOVERY_CLEARED,
            session_id=session_id,
            scan_id=scan_id,
            recorded_at=recorded_at,
            payload=payload,
        )

    def state(self, scan_id: str) -> ScanRecoveryState | None:
        receipts = self.read(scan_id)
        if not receipts:
            return None
        started = [item for item in receipts if item.kind is ScanReceiptKind.SCAN_STARTED]
        if len(started) != 1:
            raise ReceiptCorrupt("scan must have exactly one SCAN_STARTED receipt")
        terminals = [item for item in receipts if item.kind in _TERMINAL]
        if len(terminals) > 1:
            raise ReceiptCorrupt("scan has more than one terminal receipt")
        terminal = terminals[0].kind if terminals else None
        has_recovery = any(
            item.kind is ScanReceiptKind.RECOVERY_REQUIRED for item in receipts
        )
        cleared = any(item.kind is ScanReceiptKind.RECOVERY_CLEARED for item in receipts)
        return ScanRecoveryState(
            scan_id=scan_id,
            session_id=started[0].session_id,
            receipt_ids=tuple(item.receipt_id for item in receipts),
            shard_ids=tuple(
                item.shard_id
                for item in receipts
                if item.kind is ScanReceiptKind.SCAN_SHARD_COMPLETED and item.shard_id
            ),
            terminal_kind=terminal,
            recovery_required=(terminal is None or (has_recovery and not cleared)),
        )

    def unmatched(self, *, session_id: str | None = None) -> tuple[ScanRecoveryState, ...]:
        grouped: dict[str, list[ScanReceipt]] = {}
        for receipt in self._all():
            if session_id is None or receipt.session_id == session_id:
                grouped.setdefault(receipt.scan_id, []).append(receipt)
        states: list[ScanRecoveryState] = []
        for scan_id in sorted(grouped):
            state = self.state(scan_id)
            if state is not None and state.recovery_required:
                states.append(state)
        return tuple(states)

    def recoverable(self, *, session_id: str | None = None) -> tuple[ScanRecoveryState, ...]:
        """Alias used by startup code when it searches for unmatched scans."""

        return self.unmatched(session_id=session_id)

    def scan_is_resolved(self, scan_id: str) -> bool:
        state = self.state(scan_id)
        return state is not None and not state.recovery_required
