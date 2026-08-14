"""Durable receipt and execution-outbox primitives for opening sagas.

This module deliberately stops before broker submission.  Its job is to make
the dangerous boundary explicit and recoverable:

1. publish a physical-send intent;
2. consume the independent approval;
3. let the execution owner submit to the broker;
4. record what the broker actually accepted and the eventual outcome.

If the process dies between any two steps, the absence of the next receipt is
*not* interpreted as "nothing happened".  The unfinished intent is surfaced as
recovery-required and stays quarantined until an independent reconciliation
clears it.

The receipt journal is append-only, fsync'd, and protected by an OS file lock.
Each saga has a monotonically increasing version.  Callers that hold a stale
version receive a compare-and-swap refusal instead of silently appending a
second transition.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol
from uuid import uuid4

from ..errors import JournalError, RefusedError

__all__ = [
    "ExecutionIntent",
    "ExecutionOutbox",
    "OutboxState",
    "ReceiptConflict",
    "ReceiptJournal",
    "ReceiptKind",
    "authorize_after_intent",
    "utc_instant",
]


RECEIPT_SCHEMA_VERSION = 1


class ReceiptKind(str, Enum):
    """Durable saga vocabulary shared by approval, logical, and execution."""

    SESSION_ACQUIRED = "SESSION_ACQUIRED"
    SCHEDULER_STARTED = "SCHEDULER_STARTED"
    TICK_STARTED = "TICK_STARTED"
    TICK_FINISHED = "TICK_FINISHED"
    TICK_UNRESOLVED = "TICK_UNRESOLVED"
    TICK_RECONCILED = "TICK_RECONCILED"
    SCAN_STARTED = "SCAN_STARTED"
    SCAN_SHARD_COMPLETED = "SCAN_SHARD_COMPLETED"
    SCAN_COMPLETED = "SCAN_COMPLETED"
    SCAN_ABORTED = "SCAN_ABORTED"
    LOGICAL_ENTRY_CLAIMED = "LOGICAL_ENTRY_CLAIMED"
    REVIEW_REQUEST_INTENT = "REVIEW_REQUEST_INTENT"
    REVIEW_REQUEST_FILED = "REVIEW_REQUEST_FILED"
    REVIEW_APPROVAL_CONSUMED = "REVIEW_APPROVAL_CONSUMED"
    PHYSICAL_SEND_INTENT = "PHYSICAL_SEND_INTENT"
    BROKER_SUBMISSION_OBSERVED = "BROKER_SUBMISSION_OBSERVED"
    ORDER_OUTCOME = "ORDER_OUTCOME"
    BROKER_RECONCILIATION = "BROKER_RECONCILIATION"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RECOVERY_CLEARED = "RECOVERY_CLEARED"


def utc_instant(value: dt.datetime, label: str = "timestamp") -> dt.datetime:
    """Return an aware UTC instant, refusing naive or invalid timestamps."""

    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be a timezone-aware datetime, got {value!r}")
    return value.astimezone(dt.timezone.utc)


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold a process-safe lock that is released by the OS after a crash.

    The repository runs on Windows, but keeping the POSIX branch costs little
    and makes the journal usable in CI and diagnostic containers too.  A lock
    file is intentionally never deleted: deleting it while another process is
    waiting would create a split-brain lock.  The kernel releases the byte lock
    when the owning process exits.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - exercised on POSIX CI only
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised on POSIX CI only
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ReceiptConflict(RefusedError):
    """A caller attempted a transition from a stale saga version."""


@dataclass(frozen=True)
class ReceiptJournal:
    """Append-only, versioned receipt store with idempotent keys."""

    path: Path

    def __init__(self, path: Path | str) -> None:
        object.__setattr__(self, "path", Path(path))

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _read_unlocked(self) -> list[dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8", errors="strict")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise JournalError(f"cannot read receipt journal at {self.path}: {exc}") from exc
        records: list[dict[str, Any]] = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (TypeError, ValueError) as exc:
                raise JournalError(
                    f"receipt journal {self.path} has malformed line {number}",
                    hint="recovery is blocked until the journal is repaired or restored",
                ) from exc
            if not isinstance(record, dict) or record.get("v") != RECEIPT_SCHEMA_VERSION:
                raise JournalError(
                    f"receipt journal {self.path} has an unsupported record at line {number}",
                    hint="a newer or corrupt receipt journal cannot authorize a retry",
                )
            records.append(record)
        return records

    def records(self) -> tuple[dict[str, Any], ...]:
        with exclusive_file_lock(self.lock_path):
            return tuple(self._read_unlocked())

    def _current_version(self, records: list[dict[str, Any]], saga_id: str) -> int:
        versions = [
            int(record.get("version", 0))
            for record in records
            if str(record.get("saga_id", "")) == saga_id
        ]
        return max(versions, default=0)

    def append(
        self,
        kind: ReceiptKind | str,
        *,
        saga_id: str,
        at: dt.datetime,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Append one receipt, or return the prior receipt for its idempotency key."""

        if not str(saga_id).strip():
            raise ValueError("a durable receipt must name its saga")
        timestamp = utc_instant(at, "receipt timestamp")
        name = kind.value if isinstance(kind, ReceiptKind) else str(kind)
        if not name.strip():
            raise ValueError("a durable receipt must name its kind")

        with exclusive_file_lock(self.lock_path):
            records = self._read_unlocked()
            if idempotency_key:
                for record in records:
                    if record.get("idempotency_key") == idempotency_key:
                        return record

            current = self._current_version(records, str(saga_id))
            if expected_version is not None and current != expected_version:
                raise ReceiptConflict(
                    f"receipt saga {saga_id} is at version {current}, expected "
                    f"{expected_version}",
                    hint="reload the saga and reconcile before attempting another transition",
                )
            record = {
                "v": RECEIPT_SCHEMA_VERSION,
                "receipt_id": str(uuid4()),
                "kind": name,
                "saga_id": str(saga_id),
                "version": current + 1,
                "at": timestamp.isoformat(),
                "idempotency_key": idempotency_key,
                "payload": dict(payload or {}),
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                data = (
                    json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
                ).encode("utf-8")
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    written = os.write(fd, data)
                    if written != len(data):
                        raise JournalError(
                            f"short receipt write to {self.path}: {written}/{len(data)} bytes"
                        )
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except JournalError:
                raise
            except OSError as exc:
                raise JournalError(f"cannot append receipt at {self.path}: {exc}") from exc
            return record

    def by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        return next(
            (record for record in self.records() if record.get("idempotency_key") == idempotency_key),
            None,
        )

    def for_saga(self, saga_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(record for record in self.records() if record.get("saga_id") == str(saga_id))


class OutboxState(str, Enum):
    SEND_INTENT = "SEND_INTENT"
    APPROVAL_CONSUMED = "APPROVAL_CONSUMED"
    BROKER_SUBMISSION_OBSERVED = "BROKER_SUBMISSION_OBSERVED"
    ORDER_OUTCOME = "ORDER_OUTCOME"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RECOVERY_CLEARED = "RECOVERY_CLEARED"


@dataclass(frozen=True)
class ExecutionIntent:
    """The durable identity handed from approval to the broker owner."""

    saga_id: str
    logical_entry_id: str
    proposal_revision: int
    approval_id: str
    request_id: str
    spec_digest: str
    packet_digest: str
    created_at: dt.datetime
    version: int
    state: OutboxState


class ExecutionOutbox:
    """Prepare and recover the approval-to-physical-send saga.

    No method in this class submits an order.  The R6 execution owner must
    observe ``PHYSICAL_SEND_INTENT`` before calling the broker and must append
    ``BROKER_SUBMISSION_OBSERVED`` afterwards.  An unfinished intent is
    quarantined, never replayed automatically.
    """

    def __init__(self, path: Path | str) -> None:
        self.journal = ReceiptJournal(path)

    def _records(self, saga_id: str) -> tuple[dict[str, Any], ...]:
        return self.journal.for_saga(saga_id)

    def _intent_from_records(self, records: tuple[dict[str, Any], ...]) -> ExecutionIntent | None:
        if not records:
            return None
        first = records[0]
        payload = dict(first.get("payload") or {})
        state = OutboxState.SEND_INTENT
        for record in records[1:]:
            kind = record.get("kind")
            if kind == ReceiptKind.REVIEW_APPROVAL_CONSUMED.value:
                state = OutboxState.APPROVAL_CONSUMED
            elif kind == ReceiptKind.BROKER_SUBMISSION_OBSERVED.value:
                state = OutboxState.BROKER_SUBMISSION_OBSERVED
            elif kind == ReceiptKind.ORDER_OUTCOME.value:
                state = OutboxState.ORDER_OUTCOME
            elif kind == ReceiptKind.RECOVERY_REQUIRED.value:
                state = OutboxState.RECOVERY_REQUIRED
            elif kind == ReceiptKind.RECOVERY_CLEARED.value:
                state = OutboxState.RECOVERY_CLEARED
        return ExecutionIntent(
            saga_id=str(first["saga_id"]),
            logical_entry_id=str(payload["logical_entry_id"]),
            proposal_revision=int(payload["proposal_revision"]),
            approval_id=str(payload["approval_id"]),
            request_id=str(payload["request_id"]),
            spec_digest=str(payload["spec_digest"]),
            packet_digest=str(payload["packet_digest"]),
            created_at=utc_instant(dt.datetime.fromisoformat(str(first["at"])), "outbox timestamp"),
            version=int(records[-1]["version"]),
            state=state,
        )

    def prepare(
        self,
        *,
        logical_entry_id: str,
        proposal_revision: int,
        approval_id: str,
        request_id: str,
        spec_digest: str,
        packet_digest: str,
        at: dt.datetime,
    ) -> ExecutionIntent:
        """Publish the send intent exactly once for this logical revision."""

        saga_id = f"{logical_entry_id}:{proposal_revision}:{spec_digest}"
        existing = self._intent_from_records(self._records(saga_id))
        if existing is not None:
            expected = {
                "logical_entry_id": str(logical_entry_id),
                "proposal_revision": int(proposal_revision),
                "approval_id": str(approval_id),
                "request_id": str(request_id),
                "spec_digest": str(spec_digest),
                "packet_digest": str(packet_digest),
            }
            actual = {
                key: getattr(existing, key)
                for key in expected
            }
            if actual != expected:
                raise RefusedError(
                    f"execution saga {saga_id} already names different send facts",
                    hint="a logical entry cannot reuse an outbox identity for a changed approval",
                )
            return existing

        record = self.journal.append(
            ReceiptKind.PHYSICAL_SEND_INTENT,
            saga_id=saga_id,
            at=at,
            idempotency_key=f"physical-send-intent:{saga_id}",
            payload={
                "logical_entry_id": str(logical_entry_id),
                "proposal_revision": int(proposal_revision),
                "approval_id": str(approval_id),
                "request_id": str(request_id),
                "spec_digest": str(spec_digest),
                "packet_digest": str(packet_digest),
            },
        )
        return self._intent_from_records((record,))  # type: ignore[return-value]

    def _require_intent(self, intent: ExecutionIntent) -> ExecutionIntent:
        current = self._intent_from_records(self._records(intent.saga_id))
        if current is None:
            raise RefusedError(
                f"execution saga {intent.saga_id} is not durable",
                hint="publish PHYSICAL_SEND_INTENT before consuming approval",
            )
        if current.version != intent.version:
            raise ReceiptConflict(
                f"execution saga {intent.saga_id} advanced to version {current.version}",
                hint="reload and reconcile the existing outbox intent",
            )
        return current

    def record_approval_consumed(
        self, intent: ExecutionIntent, *, at: dt.datetime
    ) -> ExecutionIntent:
        existing = self._intent_from_records(self._records(intent.saga_id))
        if existing is not None and existing.state in (
            OutboxState.APPROVAL_CONSUMED,
            OutboxState.BROKER_SUBMISSION_OBSERVED,
            OutboxState.ORDER_OUTCOME,
        ):
            return existing
        current = self._require_intent(intent)
        if current.state is not OutboxState.SEND_INTENT:
            raise RefusedError(
                f"cannot consume approval for execution saga {intent.saga_id} in state {current.state.value}",
                hint="ambiguous or terminal outbox state requires reconciliation",
            )
        self.journal.append(
            ReceiptKind.REVIEW_APPROVAL_CONSUMED,
            saga_id=intent.saga_id,
            at=at,
            expected_version=current.version,
            idempotency_key=f"approval-consumed:{intent.saga_id}",
            payload={"approval_id": intent.approval_id, "spec_digest": intent.spec_digest},
        )
        return self._intent_from_records(self._records(intent.saga_id))  # type: ignore[return-value]

    def record_broker_submission_observed(
        self,
        intent: ExecutionIntent,
        *,
        broker_order_id: str,
        at: dt.datetime,
    ) -> ExecutionIntent:
        existing = self._intent_from_records(self._records(intent.saga_id))
        if existing is not None and existing.state in (
            OutboxState.BROKER_SUBMISSION_OBSERVED,
            OutboxState.ORDER_OUTCOME,
        ):
            return existing
        current = self._require_intent(intent)
        if current.state is not OutboxState.APPROVAL_CONSUMED:
            raise RefusedError(
                f"broker submission for {intent.saga_id} lacks consumed-approval proof",
                hint="the outbox must prove intent then approval consumption before broker work",
            )
        self.journal.append(
            ReceiptKind.BROKER_SUBMISSION_OBSERVED,
            saga_id=intent.saga_id,
            at=at,
            expected_version=current.version,
            idempotency_key=f"broker-submission:{intent.saga_id}:{broker_order_id}",
            payload={"broker_order_id": str(broker_order_id)},
        )
        return self._intent_from_records(self._records(intent.saga_id))  # type: ignore[return-value]

    def record_order_outcome(
        self,
        intent: ExecutionIntent,
        *,
        outcome: str,
        at: dt.datetime,
        detail: str = "",
    ) -> ExecutionIntent:
        existing = self._intent_from_records(self._records(intent.saga_id))
        if existing is not None and existing.state is OutboxState.ORDER_OUTCOME:
            return existing
        current = self._require_intent(intent)
        if current.state is not OutboxState.BROKER_SUBMISSION_OBSERVED:
            raise RefusedError(
                f"order outcome for {intent.saga_id} lacks broker submission proof",
                hint="reconcile the broker before closing the execution saga",
            )
        self.journal.append(
            ReceiptKind.ORDER_OUTCOME,
            saga_id=intent.saga_id,
            at=at,
            expected_version=current.version,
            idempotency_key=f"order-outcome:{intent.saga_id}",
            payload={"outcome": str(outcome), "detail": str(detail)},
        )
        return self._intent_from_records(self._records(intent.saga_id))  # type: ignore[return-value]

    def mark_recovery_required(
        self, intent: ExecutionIntent, *, at: dt.datetime, reason: str
    ) -> ExecutionIntent:
        current = self._intent_from_records(self._records(intent.saga_id))
        if current is None:
            raise RefusedError(f"execution saga {intent.saga_id} is not durable")
        if current.state is OutboxState.RECOVERY_REQUIRED:
            return current
        if current.state in (OutboxState.ORDER_OUTCOME, OutboxState.RECOVERY_CLEARED):
            return current
        self.journal.append(
            ReceiptKind.RECOVERY_REQUIRED,
            saga_id=intent.saga_id,
            at=at,
            expected_version=current.version,
            idempotency_key=f"recovery-required:{intent.saga_id}",
            payload={"reason": str(reason)},
        )
        return self._intent_from_records(self._records(intent.saga_id))  # type: ignore[return-value]

    def clear_recovery(
        self, intent: ExecutionIntent, *, at: dt.datetime, reconciliation: Mapping[str, Any]
    ) -> ExecutionIntent:
        existing = self._intent_from_records(self._records(intent.saga_id))
        if existing is not None and existing.state is OutboxState.RECOVERY_CLEARED:
            return existing
        current = self._require_intent(intent)
        if current.state is not OutboxState.RECOVERY_REQUIRED:
            raise RefusedError(
                f"execution saga {intent.saga_id} is not quarantined",
                hint="only an explicitly recovery-required saga may be cleared",
            )
        self.journal.append(
            ReceiptKind.RECOVERY_CLEARED,
            saga_id=intent.saga_id,
            at=at,
            expected_version=current.version,
            idempotency_key=f"recovery-cleared:{intent.saga_id}",
            payload=dict(reconciliation),
        )
        return self._intent_from_records(self._records(intent.saga_id))  # type: ignore[return-value]

    def unresolved(self) -> tuple[ExecutionIntent, ...]:
        """Return every non-terminal intent and emit no automatic replay."""

        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in self.journal.records():
            grouped.setdefault(str(record.get("saga_id", "")), []).append(record)
        result: list[ExecutionIntent] = []
        for records in grouped.values():
            intent = self._intent_from_records(tuple(records))
            if intent is not None and intent.state not in (
                OutboxState.ORDER_OUTCOME,
                OutboxState.RECOVERY_CLEARED,
            ):
                result.append(intent)
        return tuple(result)


class _Verifier(Protocol):
    def recheck(
        self, packet: Any, approval: Any, *, now: dt.datetime
    ) -> Any: ...

    def consume(self, approval: Any, *, now: dt.datetime) -> None: ...


def authorize_after_intent(
    *,
    outbox: ExecutionOutbox,
    verifier: _Verifier,
    packet: Any,
    approval: Any,
    logical_entry_id: str,
    proposal_revision: int,
    now: dt.datetime,
) -> ExecutionIntent:
    """Reference saga ordering for the future broker/execution owner.

    The function is intentionally broker-agnostic.  It is a reusable final
    door: fresh packet/approval validation happens after the intent exists and
    before approval consumption.  A crash after consumption leaves the outbox
    intent visible for reconciliation rather than allowing a replay.
    """

    intent = outbox.prepare(
        logical_entry_id=logical_entry_id,
        proposal_revision=proposal_revision,
        approval_id=str(approval.response_id),
        request_id=str(approval.request_id),
        spec_digest=str(packet.spec.digest),
        packet_digest=str(packet.spec.digest),
        at=now,
    )
    verifier.recheck(packet, approval, now=now)
    verifier.consume(approval, now=now)
    return outbox.record_approval_consumed(intent, at=now)
