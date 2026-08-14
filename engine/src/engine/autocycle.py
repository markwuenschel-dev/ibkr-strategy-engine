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
            if not isinstance(previous, dict) or previous.get("schema") != RECEIPT_SCHEMA:
                raise ValueError("unknown receipt sequence schema")
            raw_sequence = previous.get("sequence")
            if (
                isinstance(raw_sequence, bool)
                or not isinstance(raw_sequence, int)
                or raw_sequence <= 0
            ):
                raise ValueError("malformed receipt sequence")
            sequence = raw_sequence
        except FileNotFoundError:
            sequence = 0
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise CycleError(
                f"FAIL-RECOVERY-BLOCKED: cycle receipt sequence is corrupt: "
                f"{self.sequence_path}"
            ) from exc
        if sequence < 0:
            raise CycleError(
                "FAIL-RECOVERY-BLOCKED: cycle receipt sequence is negative"
            )
        sequence += 1
        _atomic_json(self.sequence_path, {"schema": RECEIPT_SCHEMA, "sequence": sequence})
        return sequence

    def records(self) -> tuple[dict[str, Any], ...]:
        records: list[dict[str, Any]] = []
        seen_sequences: set[int] = set()
        for path in sorted(self.events_root.glob("[0-9]*-*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise CycleError(
                    f"FAIL-RECOVERY-BLOCKED: cycle receipt is unreadable: {path}"
                ) from exc
            if not isinstance(value, dict):
                raise CycleError(
                    f"FAIL-RECOVERY-BLOCKED: cycle receipt is not an object: {path}"
                )
            if value.get("schema") != RECEIPT_SCHEMA:
                raise CycleError(
                    f"FAIL-RECOVERY-BLOCKED: cycle receipt schema is unknown: {path}"
                )
            if (
                isinstance(value.get("sequence"), bool)
                or not isinstance(value.get("sequence"), int)
                or value["sequence"] <= 0
                or not isinstance(value.get("event"), str)
                or not value["event"].strip()
            ):
                raise CycleError(
                    f"FAIL-RECOVERY-BLOCKED: cycle receipt identity is malformed: {path}"
                )
            try:
                filename_sequence = int(path.name.split("-", 1)[0])
            except (ValueError, IndexError) as exc:
                raise CycleError(
                    f"FAIL-RECOVERY-BLOCKED: cycle receipt filename is malformed: {path}"
                ) from exc
            if filename_sequence != value["sequence"] or value["sequence"] in seen_sequences:
                raise CycleError(
                    f"FAIL-RECOVERY-BLOCKED: cycle receipt sequence collision: {path}"
                )
            seen_sequences.add(value["sequence"])
            records.append(value)
        return tuple(records)

    def unmatched_ticks(self, *, session_id: str | None = None) -> tuple[dict[str, Any], ...]:
        starts: dict[str, dict[str, Any]] = {}
        terminal: set[str] = set()
        reconciled: set[str] = set()
        unresolved: set[str] = set()
        for record in self.records():
            if session_id is not None and record.get("session_id") != session_id:
                continue
            tick_id = record.get("tick_id")
            if not isinstance(tick_id, str) or not tick_id:
                continue
            event = record.get("event")
            if event == ReceiptKind.TICK_STARTED.value:
                starts[tick_id] = record
            elif event == ReceiptKind.TICK_UNRESOLVED.value:
                # UNRESOLVED is a durable terminal *observation* but not a
                # clearance.  A later normal finish must not make a crash
                # look clean; only TICK_RECONCILED closes this fence.
                terminal.add(tick_id)
                unresolved.add(tick_id)
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
            if tick_id not in reconciled
            and (tick_id not in terminal or tick_id in unresolved)
        )


class FixedRateSchedule:
    """Durable fixed-rate slots; late wakeups coalesce to one current slot.

    ``pending`` is a crash witness for the small interval between selecting a
    slot and publishing ``TICK_STARTED``.  Without it, a process dying in that
    interval would silently consume the slot with no receipt to reconcile.
    Production callers also bind the state to the session/fencing/policy
    identity, so a reused state directory cannot silently change cadence.
    """

    def __init__(
        self,
        root: Path,
        *,
        anchor: dt.datetime,
        cadences: Mapping[JobKind, int],
        session_id: str | None = None,
        lease_nonce: str | None = None,
        policy_hash: str | None = None,
        catalog_hash: str | None = None,
    ):
        self.root = Path(root)
        self.path = self.root / "schedule.json"
        self.anchor = _utc(anchor)
        self.cadences = dict(cadences)
        if set(self.cadences) != set(JobKind):
            raise CycleError(
                "fixed-rate schedule must declare exactly one positive cadence per job"
            )
        if any(
            isinstance(v, bool) or not isinstance(v, int) or v <= 0
            for v in self.cadences.values()
        ):
            raise CycleError("fixed-rate schedule cadences must be positive")
        self.identity = {
            "session_id": session_id,
            "lease_nonce": lease_nonce,
            "policy_hash": policy_hash,
            "catalog_hash": catalog_hash,
        }
        supplied_identity = tuple(value is not None for value in self.identity.values())
        if any(supplied_identity) and not all(supplied_identity):
            raise CycleError(
                "fixed-rate schedule identity must include session, lease, policy, and catalog"
            )
        if any(
            value is not None and (not isinstance(value, str) or not value.strip())
            for value in self.identity.values()
        ):
            raise CycleError("fixed-rate schedule identity values must be non-empty strings")
        self._lock = threading.Lock()

    def due(self, now: dt.datetime) -> tuple[DueSlot, ...]:
        moment = _utc(now)
        with self._lock:
            state = self._read()
            if state.get("pending"):
                # Recovery owns the pending selection; never select a second
                # slot while the first has no acknowledged TICK_STARTED.
                return ()
            slots = dict(state["slots"])
            due: list[DueSlot] = []
            for job in JobKind:
                cadence = self.cadences[job]
                if moment < self.anchor:
                    continue
                index = int((moment - self.anchor).total_seconds() // cadence)
                previous = slots.get(job.value)
                if previous is not None and index <= int(previous):
                    continue
                missed = 0 if previous is None else max(0, index - int(previous) - 1)
                scheduled = self.anchor + dt.timedelta(seconds=index * cadence)
                due.append(DueSlot(job, index, scheduled, missed))
                slots[job.value] = index
            if due:
                self._write(
                    {
                        "slots": slots,
                        "pending": [self._pending_record(slot) for slot in due],
                    }
                )
            return tuple(due)

    def pending_slots(self) -> tuple[DueSlot, ...]:
        """Return the selected-but-unacknowledged slots for recovery only."""

        with self._lock:
            pending = self._read()["pending"]
            return tuple(
                DueSlot(
                    JobKind(item["job"]),
                    item["slot_index"],
                    _utc(dt.datetime.fromisoformat(item["scheduled_at"])),
                    item["missed_count"],
                )
                for item in pending
            )

    def bind_identity(
        self,
        *,
        session_id: str,
        lease_nonce: str,
        policy_hash: str,
        catalog_hash: str,
    ) -> None:
        """Bind a legacy-constructed schedule before its first production read.

        The current cycle adapter may construct this value before it has the
        worker context.  A brand-new state file can be bound at that point;
        an existing file must already carry the exact requested identity.  An
        old unbound file is not upgraded in place because doing so would turn
        stale schedule history into current authority.
        """

        expected = {
            "session_id": session_id,
            "lease_nonce": lease_nonce,
            "policy_hash": policy_hash,
            "catalog_hash": catalog_hash,
        }
        if any(not isinstance(value, str) or not value.strip() for value in expected.values()):
            raise CycleError("fixed-rate schedule identity values must be non-empty strings")
        with self._lock:
            if self.identity == expected:
                return
            if any(value is not None for value in self.identity.values()):
                raise CycleError(
                    "FAIL-STALE-PAPERDAY-AUTHORITY: schedule identity is already bound"
                )
            if self.path.exists():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise CycleError(
                        "FAIL-RECOVERY-BLOCKED: fixed-rate schedule is unreadable"
                    ) from exc
                if not isinstance(loaded, dict) or loaded.get("identity") != expected:
                    raise CycleError(
                        "FAIL-STALE-PAPERDAY-AUTHORITY: refusing to adopt an unbound or foreign schedule"
                    )
            self.identity = expected
            if self.path.exists():
                # Validate the complete persisted state under the newly bound
                # identity before allowing the worker to proceed.
                self._read()

    def _read(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"slots": {}, "pending": []}
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise CycleError(
                f"FAIL-RECOVERY-BLOCKED: fixed-rate schedule is unreadable: {self.path}"
            ) from exc
        if not isinstance(loaded, dict):
            raise CycleError("FAIL-RECOVERY-BLOCKED: fixed-rate schedule state is malformed")
        if set(loaded) != {
            "schema",
            "anchor",
            "identity",
            "cadences",
            "slots",
            "pending",
        }:
            raise CycleError("FAIL-RECOVERY-BLOCKED: fixed-rate schedule state shape is unknown")
        if loaded.get("schema") != SCHEDULE_SCHEMA:
            raise CycleError(
                "FAIL-CADENCE-DRIFT: fixed-rate schedule schema is unknown"
            )
        if loaded.get("anchor") != self.anchor.isoformat():
            raise CycleError(
                "FAIL-CADENCE-DRIFT: fixed-rate schedule anchor changed"
            )
        expected_cadences = {
            job.value: cadence for job, cadence in self.cadences.items()
        }
        if loaded.get("cadences") != expected_cadences:
            raise CycleError(
                "FAIL-CADENCE-DRIFT: fixed-rate schedule cadence changed"
            )
        recorded_identity = loaded.get("identity")
        if recorded_identity != self.identity:
            raise CycleError(
                "FAIL-STALE-PAPERDAY-AUTHORITY: fixed-rate schedule identity changed"
            )
        values = loaded.get("slots")
        pending = loaded.get("pending", [])
        if not isinstance(values, dict) or not isinstance(pending, list):
            raise CycleError("FAIL-RECOVERY-BLOCKED: fixed-rate schedule state is malformed")
        unknown_slots = set(values) - set(expected_cadences)
        if unknown_slots:
            raise CycleError("FAIL-CADENCE-DRIFT: schedule contains an unknown job")
        slots: dict[str, int] = {}
        for key, value in values.items():
            if (
                not isinstance(key, str)
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise CycleError("FAIL-RECOVERY-BLOCKED: schedule slot counter is malformed")
            slots[key] = value
        checked_pending: list[dict[str, Any]] = []
        for item in pending:
            if not isinstance(item, dict) or set(item) != {
                "job",
                "slot_index",
                "scheduled_at",
                "missed_count",
            }:
                raise CycleError("FAIL-RECOVERY-BLOCKED: schedule pending state is malformed")
            if item["job"] not in expected_cadences:
                raise CycleError("FAIL-CADENCE-DRIFT: schedule pending job is unknown")
            for key in ("slot_index", "missed_count"):
                if (
                    isinstance(item[key], bool)
                    or not isinstance(item[key], int)
                    or item[key] < 0
                ):
                    raise CycleError("FAIL-RECOVERY-BLOCKED: schedule pending counter is malformed")
            try:
                scheduled_at = _utc(dt.datetime.fromisoformat(item["scheduled_at"]))
            except (TypeError, ValueError) as exc:
                raise CycleError(
                    "FAIL-RECOVERY-BLOCKED: schedule pending timestamp is malformed"
                ) from exc
            checked_pending.append(
                {
                    "job": item["job"],
                    "slot_index": item["slot_index"],
                    "scheduled_at": scheduled_at.isoformat(),
                    "missed_count": item["missed_count"],
                }
            )
        for item in checked_pending:
            if slots.get(item["job"]) != item["slot_index"]:
                raise CycleError(
                    "FAIL-RECOVERY-BLOCKED: pending slot does not match its durable counter"
                )
        return {"slots": slots, "pending": checked_pending}

    @staticmethod
    def _pending_record(slot: DueSlot) -> dict[str, Any]:
        return {
            "job": slot.job.value,
            "slot_index": slot.slot_index,
            "scheduled_at": _utc(slot.scheduled_at).isoformat(),
            "missed_count": slot.missed_count,
        }

    def acknowledge(self, slots: Iterable[DueSlot]) -> None:
        """Acknowledge a selection immediately after ``TICK_STARTED``."""

        selected = tuple(slots)
        with self._lock:
            state = self._read()
            pending = state.get("pending", [])
            expected = tuple(
                sorted(
                    (
                        item["job"],
                        item["slot_index"],
                        item["scheduled_at"],
                        item["missed_count"],
                    )
                    for item in pending
                )
            )
            actual = tuple(
                sorted(
                    (
                        slot.job.value,
                        slot.slot_index,
                        _utc(slot.scheduled_at).isoformat(),
                        slot.missed_count,
                    )
                    for slot in selected
                )
            )
            if len(actual) != len(set(actual)) or expected != actual:
                raise CycleError(
                    "FAIL-CADENCE-DRIFT: scheduled slot changed before TICK_STARTED"
                )
            state["pending"] = []
            self._write(state)

    def unresolved(self) -> bool:
        with self._lock:
            return bool(self._read().get("pending"))

    def reconcile_pending(self, slots: Iterable[DueSlot] | None = None) -> None:
        """Clear selected slots after recovery or an intentional window skip.

        Passing slots clears only that subset, which lets the worker discard
        jobs outside their session window while retaining the allowed jobs as
        the crash witness until ``TICK_STARTED`` is durable.  Omitting slots is
        reserved for an explicit broker-recovery decision.
        """

        with self._lock:
            state = self._read()
            pending = list(state["pending"])
            if slots is None:
                selected = None
            else:
                selected = {(slot.job.value, slot.slot_index) for slot in slots}
                pending_keys = {(item["job"], item["slot_index"]) for item in pending}
                if not selected <= pending_keys:
                    raise CycleError(
                        "FAIL-CADENCE-DRIFT: cannot reconcile an unselected schedule slot"
                    )
            remaining = (
                []
                if selected is None
                else [
                    item
                    for item in pending
                    if (item["job"], item["slot_index"]) not in selected
                ]
            )
            if remaining != pending:
                state["pending"] = remaining
                self._write(state)

    def _write(self, state: Mapping[str, Any]) -> None:
        _atomic_json(
            self.path,
            {
                "schema": SCHEDULE_SCHEMA,
                "anchor": self.anchor.isoformat(),
                "identity": self.identity,
                "cadences": {
                    job.value: cadence for job, cadence in self.cadences.items()
                },
                "slots": state.get("slots", {}),
                "pending": state.get("pending", []),
            },
        )


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
        stop_requested: Callable[[], bool] | None = None,
        job_allowed: Callable[[JobKind, dt.datetime], bool] | None = None,
        heartbeat: Callable[[], None] | None = None,
        recovery_required: Callable[[], bool] | None = None,
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
        self.stop_requested = stop_requested or (lambda: False)
        self.job_allowed = job_allowed or (lambda _job, _at: True)
        self.heartbeat = heartbeat
        self.recovery_required = recovery_required
        self._stop = threading.Event()
        self._single_flight = threading.Lock()
        schedule.bind_identity(
            session_id=session_id,
            lease_nonce=lease_nonce,
            policy_hash=config.policy_hash,
            catalog_hash=config.catalog_hash,
        )
        # The state root is shared across paper-day restarts.  A prior
        # session's unmatched tick is still a broker-ambiguity witness; do not
        # let a fresh session hide it merely because its session id differs.
        self._recovery_blocked = bool(receipts.unmatched_ticks()) or schedule.unresolved()
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
        # The schedule witness is cleared in the same explicit recovery
        # decision as the tick receipts.  If its identity or bytes are wrong,
        # this raises before any clearance receipt can claim success.
        self.schedule.reconcile_pending()
        unmatched = self.receipts.unmatched_ticks()
        foreign = [
            item
            for item in unmatched
            if item.get("session_id") not in {None, self.session_id}
        ]
        if foreign:
            raise CycleError(
                "FAIL-RECOVERY-BLOCKED: unresolved ticks belong to another "
                "session authority; operator reconciliation is required"
            )
        for item in unmatched:
            self.receipts.emit(
                ReceiptKind.TICK_RECONCILED,
                tick_id=item.get("tick_id"),
                attempt_id=item.get("attempt_id"),
                session_id=self.session_id,
                lease_nonce=self.lease_nonce,
                outcome="RECONCILED",
                reason=reason,
            )
        self.receipts.emit(ReceiptKind.RECOVERY_CLEARED, context=context, reason=reason)
        self._recovery_blocked = bool(self.receipts.unmatched_ticks())

    def stop(self) -> None:
        self._stop.set()

    def run_forever(
        self,
        *,
        arm: bool = False,
        broker: Any | None = None,
        max_cycles: int | None = None,
    ) -> None:
        """Run until stop/quiesce, keeping one broker connection for the day."""

        if broker is not None:
            self._run_loop(arm=arm, broker=broker, max_cycles=max_cycles)
            return
        with self._broker_context() as connected:
            self._run_loop(arm=arm, broker=connected, max_cycles=max_cycles)

    def _run_loop(self, *, arm: bool, broker: Any, max_cycles: int | None) -> None:
        completed = 0
        while not self._stop.is_set():
            if self.stop_requested():
                self.stop()
                break
            if self.recovery_required is not None and self.recovery_required():
                self._recovery_blocked = True
            moment = _utc(self.clock())
            pending = self.schedule.pending_slots()
            if pending:
                # A selection persisted without an acknowledged TICK_STARTED
                # is a recovery pass, never a normal replay.  The reconciler
                # may inspect the broker and clear it; entry/probe/discovery
                # remain suppressed while the witness is unresolved.
                due = pending
            else:
                selected = self.schedule.due(moment)
                due = tuple(
                    slot
                    for slot in selected
                    if self.job_allowed(slot.job, moment)
                )
                skipped = tuple(slot for slot in selected if slot not in due)
                if skipped:
                    self.schedule.reconcile_pending(skipped)
            if due:
                if self.heartbeat is not None:
                    self.heartbeat()
                self.run_tick(due=due, arm=arm, broker=broker)
                completed += 1
                if max_cycles is not None and completed >= max_cycles:
                    self.stop()
                    break
            self.sleeper(self.poll_seconds)

    def run_tick(
        self,
        *,
        due: Iterable[DueSlot],
        arm: bool = False,
        broker: Any | None = None,
    ) -> CycleResult:
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
            # ``due`` is produced by FixedRateSchedule and leaves a durable
            # pending witness.  Acknowledge it only after TICK_STARTED is
            # durable; a crash before this line is therefore recoverable even
            # though no broker call has happened yet.
            self.schedule.acknowledge(slots)
            broker_context = (
                contextlib.nullcontext(broker)
                if broker is not None
                else self._broker_context()
            )
            with broker_context as broker:
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
                recovery_was_blocked = self._recovery_blocked
                if recovery_was_blocked:
                    if self.phases.reconcile is not None:
                        recovery_result = dict(
                            self.phases.reconcile(phase_context) or {}
                        )
                        phase_results["RECOVERY"] = recovery_result
                        if recovery_result.get("recovery_cleared") is True:
                            self.clear_recovery(
                                reason=str(
                                    recovery_result.get(
                                        "reason", "broker and durable state reconciled"
                                    )
                                ),
                                context=context,
                            )
                    else:
                        phase_results["RECOVERY"] = {
                            "outcome": "RECOVERY_REQUIRED",
                            "failure_code": "FAIL-RECOVERY-BLOCKED",
                            "detail": "no recovery reconciler is configured",
                        }
                    # Even if reconciliation clears the in-memory flag, this
                    # tick began under an unresolved receipt.  Do not combine
                    # recovery and a new opening in one pass; the next tick
                    # must prove the cleared state from disk first.
                    outcome = "RECOVERY_BLOCKED"
                # An entry tick includes management.  This is the coalescing
                # rule that prevents two broker passes back-to-back at 5m.
                if JobKind.MANAGEMENT in due_jobs or JobKind.ENTRY in due_jobs:
                    phase_results[JobKind.MANAGEMENT.value] = dict(
                        self.phases.management(phase_context) or {}
                    )
                if JobKind.ENTRY in due_jobs and not recovery_was_blocked:
                    phase_results["ENTRY_SERVICE"] = dict(self.phases.entry(phase_context) or {})
                elif JobKind.ENTRY in due_jobs:
                    phase_results["ENTRY_SERVICE"] = {
                        "blocked": "FAIL-RECOVERY-BLOCKED",
                        "transmissions": 0,
                        "claims": 0,
                    }
                # Candidate work has priority over breadth when both are due.
                if not recovery_was_blocked and JobKind.PROBE in due_jobs:
                    phase_results[JobKind.PROBE.value] = dict(self.phases.probe(phase_context) or {})
                if not recovery_was_blocked and JobKind.DISCOVERY in due_jobs:
                    phase_results[JobKind.DISCOVERY.value] = dict(
                        self.phases.discovery(phase_context) or {}
                    )
                if recovery_was_blocked and JobKind.ENTRY in due_jobs:
                    phase_results.setdefault("ENTRY_SERVICE", {})
                    phase_results["ENTRY_SERVICE"] = {
                        **phase_results["ENTRY_SERVICE"],
                        "blocked": "FAIL-RECOVERY-BLOCKED",
                        "transmissions": 0,
                    }
                    outcome = "RECOVERY_BLOCKED"
                for key, value in phase_results.items():
                    openings = value.get("new_openings")
                    if openings is None:
                        # Keep the injected Phase contract useful for tests
                        # and non-runner adapters, while the real runner uses
                        # ``new_openings`` so repricing does not look like
                        # multiple logical entries.
                        openings = value.get("transmissions", 0)
                    if (
                        isinstance(openings, bool)
                        or not isinstance(openings, int)
                        or openings < 0
                    ):
                        raise CycleError(
                            "FAIL-UNAUTHORIZED-ENTRY: phase reported an invalid opening count"
                        )
                    if openings > self.config.max_new_entries_per_pass:
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
