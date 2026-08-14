"""Persistent, recovery-aware orchestration for unattended option cycles.

The scheduler owns process identity and process cadence.  This module owns the
application pass that runs *inside* that process.  It deliberately knows
nothing about subprocesses or paper-day locks: its inputs are a validated
policy, a broker factory, and phase functions.

There is one important operational property here: a cycle opens one broker
context and passes one :class:`CycleContext` to every phase.  Discovery cannot
accidentally create a second connection or a second pacing ledger beside
management and entry work.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol


RECEIPT_SCHEMA = "ibkr.autotrader.receipt/1"
SCHEDULE_SCHEMA = "ibkr.autotrader.schedule/1"


class CycleError(RuntimeError):
    """A fail-closed cycle configuration or lifecycle error."""


class CycleMode(str, Enum):
    DRY_RUN = "DRY_RUN"
    SHADOW = "SHADOW"
    REVIEW_ONLY = "REVIEW_ONLY"
    ARMED = "ARMED"


class JobKind(str, Enum):
    MANAGEMENT = "MANAGEMENT"
    DISCOVERY = "UNIVERSE_DISCOVERY"
    PROBE = "CANDIDATE_PROBE"
    ENTRY = "FULL_ENTRY"


class ReceiptKind(str, Enum):
    TICK_STARTED = "TICK_STARTED"
    TICK_FINISHED = "TICK_FINISHED"
    TICK_ABORTED = "TICK_ABORTED"
    TICK_UNRESOLVED = "TICK_UNRESOLVED"
    TICK_RECONCILED = "TICK_RECONCILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RECOVERY_CLEARED = "RECOVERY_CLEARED"


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CycleError("cycle timestamps must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dt.datetime):
        return _utc(value).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(v) for v in value]
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True)
class AutoCycleConfig:
    """Explicit policy values for the application worker.

    No constructor default is a production policy.  Callers should construct
    this from the hashed ``ibkr.autotrader/1`` artifact; the defaults below are
    only the reviewed seed values used by tests and artifact builders.
    """

    mandate: str
    mode: CycleMode
    management_seconds: int
    discovery_seconds: int
    probe_seconds: int
    entry_seconds: int
    missed_tick_policy: str
    entry_start: dt.time
    entry_end: dt.time
    coverage_sla_seconds: int
    max_pending_entries: int
    max_new_entries_per_pass: int
    phase2_limit: int
    policy_hash: str
    catalog_hash: str
    state_dir: Path

    def __post_init__(self) -> None:
        if self.mandate not in {"MANAGE_ONLY", "FULL"}:
            raise CycleError(f"unsupported mandate {self.mandate!r}")
        try:
            mode = CycleMode(self.mode)
        except ValueError as exc:
            raise CycleError(f"unsupported cycle mode {self.mode!r}") from exc
        if self.mandate == "MANAGE_ONLY" and mode in {
            CycleMode.REVIEW_ONLY,
            CycleMode.ARMED,
        }:
            raise CycleError("REVIEW_ONLY and ARMED require mandate FULL")
        if self.missed_tick_policy != "SKIP_MISSED_TICKS":
            raise CycleError(
                "only SKIP_MISSED_TICKS is supported; unattended work never bursts catch-up"
            )
        for name in (
            "management_seconds",
            "discovery_seconds",
            "probe_seconds",
            "entry_seconds",
            "coverage_sla_seconds",
            "max_pending_entries",
            "max_new_entries_per_pass",
            "phase2_limit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise CycleError(f"{name} must be a positive integer")
        if self.entry_start >= self.entry_end:
            raise CycleError("entry window must have a positive width")
        for name, value in (("policy_hash", self.policy_hash), ("catalog_hash", self.catalog_hash)):
            if len(value) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in value):
                raise CycleError(f"{name} must be a SHA-256 hex digest")

    @property
    def entry_enabled(self) -> bool:
        return self.mandate == "FULL" and self.mode in {
            CycleMode.REVIEW_ONLY,
            CycleMode.ARMED,
        }

    def transmission_enabled(self, *, arm: bool) -> bool:
        return self.entry_enabled and self.mode is CycleMode.ARMED and arm


@dataclass(frozen=True)
class CycleContext:
    session_id: str
    lease_nonce: str
    tick_id: str
    attempt_id: str
    policy_hash: str
    catalog_hash: str
    started_at: dt.datetime
    session_date: dt.date

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "lease_nonce",
            "tick_id",
            "attempt_id",
            "policy_hash",
            "catalog_hash",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise CycleError(f"cycle context {name} is required")
        _utc(self.started_at)

    def as_record(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "lease_nonce": self.lease_nonce,
            "tick_id": self.tick_id,
            "attempt_id": self.attempt_id,
            "policy_hash": self.policy_hash,
            "catalog_hash": self.catalog_hash,
            "started_at": _utc(self.started_at).isoformat(),
            "session_date": self.session_date.isoformat(),
        }


@dataclass(frozen=True)
class DueSlot:
    job: JobKind
    slot_index: int
    scheduled_at: dt.datetime
    missed_count: int


@dataclass(frozen=True)
class PhaseContext:
    cycle: CycleContext
    broker: Any
    pacing: Any
    config: AutoCycleConfig
    arm: bool
    due: tuple[DueSlot, ...]

    @property
    def entry_enabled(self) -> bool:
        return self.config.entry_enabled

    @property
    def transmission_enabled(self) -> bool:
        return self.config.transmission_enabled(arm=self.arm)


class Phase(Protocol):
    def __call__(self, context: PhaseContext) -> Mapping[str, Any] | None:
        ...


@dataclass(frozen=True)
class CyclePhases:
    """Application callbacks injected by the CLI/runtime adapter."""

    management: Phase
    discovery: Phase
    probe: Phase
    entry: Phase
    reconcile: Phase | None = None


@dataclass(frozen=True)
class CycleResult:
    context: CycleContext
    outcome: str
    receipt_kind: ReceiptKind
    phases: Mapping[str, Mapping[str, Any]]
    missed_slots: Mapping[str, int]
    recovery_blocked: bool


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(_jsonable(payload), sort_keys=True, indent=2) + "\n"
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


class ReceiptStore:
    """Atomic per-event receipt store with unmatched-tick recovery queries."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.events_root = self.root / "receipts"
        self.sequence_path = self.events_root / "sequence.json"
        self._lock = threading.Lock()

    def emit(
        self,
        kind: ReceiptKind | str,
        *,
        context: CycleContext | None = None,
        at: dt.datetime | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        event = kind.value if isinstance(kind, ReceiptKind) else str(kind)
        moment = _utc(at or dt.datetime.now(dt.timezone.utc))
        with self._lock:
            sequence = self._next_sequence()
            record: dict[str, Any] = {
                "schema": RECEIPT_SCHEMA,
                "sequence": sequence,
                "event": event,
                "at": moment.isoformat(),
            }
            if context is not None:
                record.update(context.as_record())
            record.update(payload)
            target = self.events_root / f"{sequence:020d}-{event}.json"
            _atomic_json(target, record)
            _atomic_json(self.events_root / "latest.json", record)
            return record

    def _next_sequence(self) -> int:
        self.events_root.mkdir(parents=True, exist_ok=True)
        try:
            previous = json.loads(self.sequence_path.read_text(encoding="utf-8"))
            sequence = int(previous["sequence"])
        except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            sequence = 0
        sequence += 1
        _atomic_json(self.sequence_path, {"schema": RECEIPT_SCHEMA, "sequence": sequence})
        return sequence

    def records(self) -> tuple[dict[str, Any], ...]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.events_root.glob("[0-9]*-*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                records.append(value)
        return tuple(records)

    def unmatched_ticks(self, *, session_id: str | None = None) -> tuple[dict[str, Any], ...]:
        starts: dict[str, dict[str, Any]] = {}
        terminal: set[str] = set()
        reconciled: set[str] = set()
        for record in self.records():
            if session_id is not None and record.get("session_id") != session_id:
                continue
            tick_id = record.get("tick_id")
            if not isinstance(tick_id, str) or not tick_id:
                continue
            event = record.get("event")
            if event == ReceiptKind.TICK_STARTED.value:
                starts[tick_id] = record
            elif event in {
                ReceiptKind.TICK_FINISHED.value,
                ReceiptKind.TICK_ABORTED.value,
                ReceiptKind.TICK_RECONCILED.value,
            }:
                terminal.add(tick_id)
            if event == ReceiptKind.TICK_RECONCILED.value:
                reconciled.add(tick_id)
        return tuple(
            record
            for tick_id, record in starts.items()
            if tick_id not in terminal or (tick_id in terminal and tick_id not in reconciled and record.get("outcome") == "UNRESOLVED")
        )


class FixedRateSchedule:
    """Durable fixed-rate slots; late wakeups coalesce to one current slot."""

    def __init__(self, root: Path, *, anchor: dt.datetime, cadences: Mapping[JobKind, int]):
        self.root = Path(root)
        self.path = self.root / "schedule.json"
        self.anchor = _utc(anchor)
        self.cadences = dict(cadences)
        if not self.cadences or any(isinstance(v, bool) or v <= 0 for v in self.cadences.values()):
            raise CycleError("fixed-rate schedule cadences must be positive")
        self._lock = threading.Lock()

    def due(self, now: dt.datetime) -> tuple[DueSlot, ...]:
        moment = _utc(now)
        with self._lock:
            state = self._read()
            due: list[DueSlot] = []
            for job in JobKind:
                cadence = self.cadences[job]
                if moment < self.anchor:
                    continue
                index = int((moment - self.anchor).total_seconds() // cadence)
                previous = state.get(job.value)
                if previous is not None and index <= int(previous):
                    continue
                missed = 0 if previous is None else max(0, index - int(previous) - 1)
                scheduled = self.anchor + dt.timedelta(seconds=index * cadence)
                due.append(DueSlot(job, index, scheduled, missed))
                state[job.value] = index
            if due:
                _atomic_json(
                    self.path,
                    {
                        "schema": SCHEDULE_SCHEMA,
                        "anchor": self.anchor.isoformat(),
                        "slots": state,
                    },
                )
            return tuple(due)

    def _read(self) -> dict[str, int]:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            values = loaded.get("slots", {})
            return {str(key): int(value) for key, value in values.items()}
        except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return {}


class OptionsCycleWorker:
    """One persistent, single-flight worker for all strategy phases."""

    def __init__(
        self,
        *,
        config: AutoCycleConfig,
        session_id: str,
        lease_nonce: str,
        broker_factory: Callable[[], Any],
        phases: CyclePhases,
        receipts: ReceiptStore,
        schedule: FixedRateSchedule,
        clock: Callable[[], dt.datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        poll_seconds: float = 1.0,
    ) -> None:
        self.config = config
        self.session_id = session_id
        self.lease_nonce = lease_nonce
        self.broker_factory = broker_factory
        self.phases = phases
        self.receipts = receipts
        self.schedule = schedule
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.sleeper = sleeper
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._single_flight = threading.Lock()
        self._recovery_blocked = bool(receipts.unmatched_ticks(session_id=session_id))
        if self._recovery_blocked:
            self.receipts.emit(
                ReceiptKind.RECOVERY_REQUIRED,
                payload_reason="unmatched tick receipt requires broker reconciliation",
            )

    @property
    def recovery_blocked(self) -> bool:
        return self._recovery_blocked

    def clear_recovery(self, *, reason: str, context: CycleContext | None = None) -> None:
        if not reason.strip():
            raise CycleError("recovery clearance requires a durable reason")
        self.receipts.emit(ReceiptKind.RECOVERY_CLEARED, context=context, reason=reason)
        self._recovery_blocked = False

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self, *, arm: bool = False) -> None:
        while not self._stop.is_set():
            due = self.schedule.due(self.clock())
            if due:
                self.run_tick(due=due, arm=arm)
            self.sleeper(self.poll_seconds)

    def run_tick(self, *, due: Iterable[DueSlot], arm: bool = False) -> CycleResult:
        slots = tuple(due)
        if not slots:
            raise CycleError("a cycle tick requires at least one due slot")
        if not self._single_flight.acquire(blocking=False):
            raise CycleError("FAIL-CADENCE-DRIFT: overlapping cycle is forbidden")
        now = _utc(self.clock())
        tick_id = f"{self.session_id}:{max(slot.slot_index for slot in slots)}"
        context = CycleContext(
            session_id=self.session_id,
            lease_nonce=self.lease_nonce,
            tick_id=tick_id,
            attempt_id=uuid.uuid4().hex,
            policy_hash=self.config.policy_hash,
            catalog_hash=self.config.catalog_hash,
            started_at=now,
            session_date=now.date(),
        )
        phase_results: dict[str, Mapping[str, Any]] = {}
        terminal = ReceiptKind.TICK_FINISHED
        outcome = "FINISHED"
        broker_started = False
        missed = {slot.job.value: slot.missed_count for slot in slots if slot.missed_count}
        self.receipts.emit(
            ReceiptKind.TICK_STARTED,
            context=context,
            jobs=[slot.job.value for slot in slots],
            scheduled_at=[slot.scheduled_at.isoformat() for slot in slots],
            missed_slots=missed,
        )
        try:
            with self._broker_context() as broker:
                broker_started = True
                phase_context = PhaseContext(
                    cycle=context,
                    broker=broker,
                    pacing=getattr(broker, "pacing", None),
                    config=self.config,
                    arm=arm,
                    due=slots,
                )
                due_jobs = {slot.job for slot in slots}
                # An entry tick includes management.  This is the coalescing
                # rule that prevents two broker passes back-to-back at 5m.
                if JobKind.MANAGEMENT in due_jobs or JobKind.ENTRY in due_jobs:
                    phase_results[JobKind.MANAGEMENT.value] = dict(
                        self.phases.management(phase_context) or {}
                    )
                if self._recovery_blocked and self.phases.reconcile is not None:
                    phase_results["RECOVERY"] = dict(self.phases.reconcile(phase_context) or {})
                    outcome = "RECOVERY_BLOCKED"
                if JobKind.ENTRY in due_jobs:
                    phase_results["ENTRY_SERVICE"] = dict(self.phases.entry(phase_context) or {})
                # Candidate work has priority over breadth when both are due.
                if JobKind.PROBE in due_jobs:
                    phase_results[JobKind.PROBE.value] = dict(self.phases.probe(phase_context) or {})
                if JobKind.DISCOVERY in due_jobs:
                    phase_results[JobKind.DISCOVERY.value] = dict(
                        self.phases.discovery(phase_context) or {}
                    )
                if self._recovery_blocked and JobKind.ENTRY in due_jobs:
                    phase_results.setdefault("ENTRY_SERVICE", {})
                    phase_results["ENTRY_SERVICE"] = {
                        **phase_results["ENTRY_SERVICE"],
                        "blocked": "FAIL-RECOVERY-BLOCKED",
                        "transmissions": 0,
                    }
                    outcome = "RECOVERY_BLOCKED"
                for key, value in phase_results.items():
                    transmissions = value.get("transmissions", 0)
                    if isinstance(transmissions, int) and transmissions > self.config.max_new_entries_per_pass:
                        raise CycleError("FAIL-UNAUTHORIZED-ENTRY: cycle exceeded opening cap")
            if outcome == "RECOVERY_BLOCKED":
                terminal = ReceiptKind.TICK_FINISHED
            self.receipts.emit(
                terminal,
                context=context,
                outcome=outcome,
                phases=phase_results,
                missed_slots=missed,
            )
            return CycleResult(context, outcome, terminal, phase_results, missed, self._recovery_blocked)
        except Exception as exc:  # noqa: BLE001 - ambiguity is a first-class outcome
            if broker_started:
                terminal = ReceiptKind.TICK_UNRESOLVED
                outcome = "UNRESOLVED"
            else:
                terminal = ReceiptKind.TICK_ABORTED
                outcome = "ABORTED"
            self.receipts.emit(
                terminal,
                context=context,
                outcome=outcome,
                error=f"{type(exc).__name__}: {exc}",
                phases=phase_results,
                recovery_required=broker_started,
            )
            if broker_started:
                self._recovery_blocked = True
            return CycleResult(context, outcome, terminal, phase_results, missed, self._recovery_blocked)
        finally:
            self._single_flight.release()

    @contextlib.contextmanager
    def _broker_context(self):
        value = self.broker_factory()
        if hasattr(value, "__enter__") and hasattr(value, "__exit__"):
            with value as broker:
                yield broker
        else:
            try:
                yield value
            finally:
                close = getattr(value, "close", None)
                if callable(close):
                    close()


__all__ = [
    "AutoCycleConfig",
    "CycleContext",
    "CycleError",
    "CycleMode",
    "CyclePhases",
    "CycleResult",
    "DueSlot",
    "FixedRateSchedule",
    "JobKind",
    "OptionsCycleWorker",
    "PhaseContext",
    "ReceiptKind",
    "ReceiptStore",
]
