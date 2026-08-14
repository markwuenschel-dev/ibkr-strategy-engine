"""Durable execution intent, transmission budget, and ambiguity quarantine.

The position store answers *what the strategy believes it owns*.  This module
answers the narrower question that matters between approval and a broker call:
what physical send did the process intend, what did the broker acknowledge, and
is it safe to try another opening order after a restart?

The outbox is deliberately separate from the order journal.  The journal is a
human-facing history of orders; the outbox is a recovery state machine.  Each
attempt has an atomically replaced state file and every transition also emits a
durable receipt.  A missing terminal transition is therefore an unresolved
physical action, never an implicit "nothing happened".

The transmission budget uses the same durable principle.  A reprice is a new
broker transmission, not an implementation detail of one logical entry.  A
reservation is written before the authorization is consumed and remains held
when the broker call has an ambiguous outcome.  This prevents a crash loop from
escaping the session cap by repeatedly forgetting the last rung.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4

from ..errors import JournalError, RefusedError

__all__ = [
    "FAIL_BROKER_AMBIGUOUS",
    "FAIL_EXECUTION_OUTBOX",
    "FAIL_LEASE_MISSING",
    "FAIL_REPRICE_BUDGET",
    "ExecutionOutbox",
    "OutboxState",
    "TransmissionBudget",
    "TransmissionReservation",
]


SCHEMA_VERSION = 1
FAIL_EXECUTION_OUTBOX = "FAIL-EXECUTION-OUTBOX"
FAIL_BROKER_AMBIGUOUS = "FAIL-BROKER-AMBIGUOUS"
FAIL_LEASE_MISSING = "FAIL-LEASE-MISSING"
FAIL_REPRICE_BUDGET = "FAIL-REPRICE-BUDGET"


class OutboxState(str, Enum):
    """Durable state of one physical opening attempt."""

    PREPARED = "PREPARED"
    APPROVAL_CONSUMED = "APPROVAL_CONSUMED"
    SEND_INTENT = "SEND_INTENT"
    SUBMISSION_OBSERVED = "SUBMISSION_OBSERVED"
    OUTCOME = "OUTCOME"
    AMBIGUOUS = "AMBIGUOUS"
    RECONCILED = "RECONCILED"
    ABORTED = "ABORTED"


_BLOCKING_STATES = frozenset(
    {
        OutboxState.PREPARED.value,
        OutboxState.APPROVAL_CONSUMED.value,
        OutboxState.SEND_INTENT.value,
        OutboxState.AMBIGUOUS.value,
    }
)


def _utc(value: dt.datetime | None = None) -> dt.datetime:
    value = value or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        raise ValueError("outbox timestamps must be timezone-aware")
    return value.astimezone(dt.timezone.utc).replace(microsecond=0)


def _iso(value: dt.datetime | None = None) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_uuid(value: Any, *, label: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise JournalError(f"{FAIL_EXECUTION_OUTBOX}: invalid {label}") from exc


def _leg_record(leg: Any) -> dict[str, Any]:
    """Serialize every contract fact needed to identify a combo exactly."""

    return {
        "con_id": int(leg.con_id),
        "symbol": str(leg.symbol).strip().upper(),
        "expiration": leg.expiration.isoformat(),
        "strike": str(leg.strike),
        "right": getattr(leg.right, "value", str(leg.right)),
        "action": getattr(leg.action, "value", str(leg.action)),
        "ratio": int(leg.ratio),
        "multiplier": int(leg.multiplier),
        "exchange": str(leg.exchange).strip().upper(),
        "trading_class": leg.trading_class,
    }


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync; Windows does not expose it uniformly."""

    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise JournalError(
            f"{FAIL_EXECUTION_OUTBOX}: cannot read durable state {path}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("v") != SCHEMA_VERSION:
        raise JournalError(
            f"{FAIL_EXECUTION_OUTBOX}: unsupported or corrupt state in {path}"
        )
    return payload


@dataclass(frozen=True)
class TransmissionReservation:
    """A durable slot reserved for one physical broker submission."""

    reservation_id: str
    strategy_id: UUID
    reserved_at: dt.datetime


class TransmissionBudget:
    """Crash-safe session transmission cap shared by initial sends and reprices.

    The caller reserves before consuming an approval.  ``commit`` is called
    after the broker accepts the API call.  A reservation that remains after a
    crash is intentionally counted until reconciliation releases or resolves
    it; releasing an unknown submission would make the cap fail open.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        limit: int,
        journal: Any | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError(f"transmission budget limit must be positive, got {limit!r}")
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.limit = limit
        self.journal = journal
        self.now = now

    @property
    def session_date(self) -> str:
        return _utc(self.now).date().isoformat()

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Acquire a fail-closed process lock for a read/modify/write cycle."""

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError as exc:
            raise RefusedError(
                f"{FAIL_EXECUTION_OUTBOX}: transmission budget is already locked",
                hint="another worker owns the broker session or the previous one died; reconcile before retrying",
            ) from exc
        try:
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
            yield
        finally:
            os.close(fd)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def _journal_count(self) -> int:
        if self.journal is None:
            return 0
        try:
            return int(self.journal.orders_today(now=_utc(self.now)))
        except Exception as exc:  # noqa: BLE001 - a broken cap answer is unsafe
            raise RefusedError(
                f"{FAIL_EXECUTION_OUTBOX}: cannot establish the journal order count"
            ) from exc

    def _initial(self) -> dict[str, Any]:
        return {
            "v": SCHEMA_VERSION,
            "session_date": self.session_date,
            "limit": self.limit,
            "journal_count": self._journal_count(),
            "committed": 0,
            "reservations": {},
        }

    def _load(self) -> dict[str, Any]:
        try:
            state = _read_json(self.path)
        except FileNotFoundError:
            return self._initial()
        if state.get("session_date") != self.session_date:
            if state.get("committed") or state.get("reservations"):
                raise RefusedError(
                    f"{FAIL_EXECUTION_OUTBOX}: prior-session transmission reservations remain",
                    hint="reconcile the prior broker session before starting a new one",
                )
            return self._initial()
        if state.get("limit") != self.limit:
            raise RefusedError(
                f"{FAIL_EXECUTION_OUTBOX}: transmission cap changed for an active session",
                hint="start a new paper day with a new policy fingerprint",
            )
        if not isinstance(state.get("reservations"), dict):
            raise JournalError(f"{FAIL_EXECUTION_OUTBOX}: malformed reservation map")
        return state

    def _sync_journal(self, state: dict[str, Any]) -> None:
        current = self._journal_count()
        previous = int(state.get("journal_count", 0))
        if current < previous:
            raise RefusedError(
                f"{FAIL_EXECUTION_OUTBOX}: order journal moved backwards",
                hint="journal truncation or replacement must be reconciled before entry",
            )
        delta = current - previous
        committed = int(state.get("committed", 0))
        state["committed"] = max(0, committed - delta)
        state["journal_count"] = current

    def _write(self, state: dict[str, Any]) -> None:
        state["v"] = SCHEMA_VERSION
        _atomic_json(self.path, state)

    def reserve(
        self,
        strategy_id: UUID,
        *,
        attempt_id: str | None = None,
        now: dt.datetime | None = None,
    ) -> TransmissionReservation:
        if not isinstance(strategy_id, UUID):
            raise ValueError("a transmission reservation must name a UUID strategy")
        at = _utc(now or self.now)
        with self._exclusive():
            state = self._load()
            self._sync_journal(state)
            used = (
                int(state["journal_count"])
                + int(state["committed"])
                + len(state["reservations"])
            )
            if used >= self.limit:
                raise RefusedError(
                    f"{FAIL_REPRICE_BUDGET}: {used} transmissions already reserved or placed, at the cap of {self.limit}",
                    hint="reconcile or wait for a new paper day; reprices count as real transmissions",
                )
            reservation_id = attempt_id or uuid4().hex
            if reservation_id in state["reservations"]:
                raise RefusedError(
                    f"{FAIL_EXECUTION_OUTBOX}: duplicate transmission reservation {reservation_id}"
                )
            state["reservations"][reservation_id] = {
                "strategy_id": str(strategy_id),
                "reserved_at": _iso(at),
            }
            self._write(state)
        return TransmissionReservation(reservation_id, strategy_id, at)

    def commit(self, reservation_id: str) -> None:
        with self._exclusive():
            state = self._load()
            if reservation_id not in state["reservations"]:
                raise RefusedError(
                    f"{FAIL_EXECUTION_OUTBOX}: unknown transmission reservation {reservation_id}"
                )
            del state["reservations"][reservation_id]
            state["committed"] = int(state["committed"]) + 1
            self._write(state)

    def release(self, reservation_id: str, *, reason: str) -> None:
        """Release only a reservation proven never to reach the broker."""

        with self._exclusive():
            state = self._load()
            reservation = state["reservations"].pop(reservation_id, None)
            if reservation is None:
                raise RefusedError(
                    f"{FAIL_EXECUTION_OUTBOX}: unknown transmission reservation {reservation_id}"
                )
            state.setdefault("released", []).append(
                {"reservation_id": reservation_id, "reason": reason, "at": _iso()}
            )
            self._write(state)

    def snapshot(self) -> dict[str, Any]:
        with self._exclusive():
            state = self._load()
            self._sync_journal(state)
            self._write(state)
            return json.loads(json.dumps(state))


class ExecutionOutbox:
    """Atomically published state and receipts for physical opening sends."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.receipts_path = self.root / "receipts.jsonl"

    def _attempt_path(self, attempt_id: str) -> Path:
        return self.root / f"attempt-{attempt_id}.json"

    def _load_attempt(self, attempt_id: str) -> dict[str, Any]:
        try:
            return _read_json(self._attempt_path(attempt_id))
        except FileNotFoundError as exc:
            raise RefusedError(
                f"{FAIL_EXECUTION_OUTBOX}: unknown execution attempt {attempt_id}"
            ) from exc

    def _receipt(self, event: str, record: dict[str, Any], **extra: Any) -> None:
        payload = {
            "v": SCHEMA_VERSION,
            "ts": _iso(),
            "event": event,
            "attempt_id": record.get("attempt_id"),
            **extra,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        try:
            fd = os.open(
                str(self.receipts_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            try:
                if os.write(fd, data) != len(data):
                    raise JournalError(f"{FAIL_EXECUTION_OUTBOX}: short receipt write")
                os.fsync(fd)
            finally:
                os.close(fd)
        except JournalError:
            raise
        except OSError as exc:
            raise JournalError(
                f"{FAIL_EXECUTION_OUTBOX}: cannot write execution receipt"
            ) from exc

    def _save(self, record: dict[str, Any], event: str, **extra: Any) -> dict[str, Any]:
        _atomic_json(self._attempt_path(str(record["attempt_id"])), record)
        self._receipt(event, record, **extra)
        return json.loads(json.dumps(record))

    def prepare(
        self,
        intent: Any,
        *,
        structure_digest: str,
        spec_digest: str,
        account: str,
        approval_id: str | None = None,
        attempt_id: str | None = None,
        session_id: str | None = None,
        lease_nonce: str | None = None,
        tick_id: str | None = None,
        now: dt.datetime | None = None,
    ) -> str:
        attempt = attempt_id or uuid4().hex
        record = {
            "v": SCHEMA_VERSION,
            "attempt_id": attempt,
            "strategy_id": str(intent.strategy_id),
            "strategy_order_ref": str(intent.strategy_id),
            "action": getattr(intent.strategy_action, "value", str(intent.strategy_action)),
            "structure_digest": structure_digest,
            "spec_digest": spec_digest,
            "account": account,
            "quantity": int(intent.quantity),
            "underlying": str(intent.underlying).strip().upper(),
            "legs": [_leg_record(leg) for leg in intent.legs],
            "created_at": _iso(now),
            "state": OutboxState.PREPARED.value,
            "approval_id": approval_id,
            "session_id": session_id,
            "lease_nonce": lease_nonce,
            "tick_id": tick_id,
        }
        if self._attempt_path(attempt).exists():
            raise RefusedError(
                f"{FAIL_EXECUTION_OUTBOX}: duplicate physical-send intent {attempt}"
            )
        self._save(record, "PHYSICAL_SEND_INTENT")
        return attempt

    def _transition(
        self, attempt_id: str, state: OutboxState, event: str, **fields: Any
    ) -> dict[str, Any]:
        record = self._load_attempt(attempt_id)
        current = str(record.get("state"))
        if current in {
            OutboxState.OUTCOME.value,
            OutboxState.RECONCILED.value,
            OutboxState.ABORTED.value,
        }:
            raise RefusedError(
                f"{FAIL_EXECUTION_OUTBOX}: attempt {attempt_id} is already terminal ({current})"
            )
        if current == OutboxState.AMBIGUOUS.value and state is not OutboxState.RECONCILED:
            raise RefusedError(
                f"{FAIL_BROKER_AMBIGUOUS}: attempt {attempt_id} is quarantined",
                hint="reconcile the broker observation before recording another local outcome",
            )
        record.update(fields)
        record["state"] = state.value
        record["updated_at"] = _iso()
        return self._save(record, event)

    def approval_consumed(self, attempt_id: str) -> dict[str, Any]:
        return self._transition(
            attempt_id,
            OutboxState.APPROVAL_CONSUMED,
            "REVIEW_APPROVAL_CONSUMED",
        )

    def send_intent(self, attempt_id: str, *, order_ref: str) -> dict[str, Any]:
        return self._transition(
            attempt_id,
            OutboxState.SEND_INTENT,
            "PHYSICAL_SEND_INTENT",
            broker_order_ref=order_ref,
        )

    def submission_observed(self, attempt_id: str, *, observation: dict[str, Any]) -> dict[str, Any]:
        return self._transition(
            attempt_id,
            OutboxState.SUBMISSION_OBSERVED,
            "BROKER_SUBMISSION_OBSERVED",
            observation=observation,
        )

    def outcome(
        self,
        attempt_id: str,
        *,
        classification: str,
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        state = (
            OutboxState.AMBIGUOUS
            if classification.upper() == "UNKNOWN"
            else OutboxState.OUTCOME
        )
        event = "RECOVERY_REQUIRED" if state is OutboxState.AMBIGUOUS else "ORDER_OUTCOME"
        return self._transition(
            attempt_id,
            state,
            event,
            classification=classification.upper(),
            observation=observation,
        )

    def quarantine(self, attempt_id: str, *, reason: str) -> dict[str, Any]:
        return self._transition(
            attempt_id,
            OutboxState.AMBIGUOUS,
            "RECOVERY_REQUIRED",
            reason=reason,
        )

    def abort(self, attempt_id: str, *, reason: str) -> dict[str, Any]:
        record = self._load_attempt(attempt_id)
        if record.get("state") not in {
            OutboxState.PREPARED.value,
            OutboxState.APPROVAL_CONSUMED.value,
        }:
            raise RefusedError(
                f"{FAIL_EXECUTION_OUTBOX}: cannot abort a physically touched attempt {attempt_id}"
            )
        record["state"] = OutboxState.ABORTED.value
        record["reason"] = reason
        record["updated_at"] = _iso()
        return self._save(record, "TICK_ABORTED", reason=reason)

    def reconcile(self, attempt_id: str, *, result: str, evidence: dict[str, Any]) -> dict[str, Any]:
        record = self._load_attempt(attempt_id)
        if record.get("state") not in {
            OutboxState.AMBIGUOUS.value,
            OutboxState.SUBMISSION_OBSERVED.value,
        }:
            raise RefusedError(
                f"{FAIL_EXECUTION_OUTBOX}: attempt {attempt_id} is not awaiting reconciliation"
            )
        record.update(
            {
                "state": OutboxState.RECONCILED.value,
                "classification": result.upper(),
                "reconciliation": evidence,
                "updated_at": _iso(),
            }
        )
        return self._save(record, "BROKER_RECONCILIATION", result=result.upper())

    def records(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("attempt-*.json")):
            records.append(_read_json(path))
        return records

    def blocking_records(self) -> list[dict[str, Any]]:
        blocking: list[dict[str, Any]] = []
        for record in self.records():
            if record.get("state") in _BLOCKING_STATES:
                blocking.append(record)
            elif record.get("state") == OutboxState.OUTCOME.value and str(
                record.get("classification", "")
            ).upper() == "UNKNOWN":
                blocking.append(record)
        return blocking

    def assert_clear(self) -> None:
        blocking = self.blocking_records()
        if blocking:
            ids = ", ".join(str(item.get("attempt_id")) for item in blocking)
            raise RefusedError(
                f"{FAIL_BROKER_AMBIGUOUS}: execution attempt(s) require broker reconciliation: {ids}",
                hint="new entries remain blocked until every ambiguous physical send is reconciled",
            )
