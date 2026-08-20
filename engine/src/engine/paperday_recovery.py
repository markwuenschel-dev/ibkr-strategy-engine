"""The paper-day recovery verb's 9-point acceptance bar -- UNWIRED.

**This module is dead code from the runtime's perspective.** Every function
here is importable and unit-testable, and nothing else -- no CLI command, no
``engine.paperday`` subcommand, no ``main_start``/``main_stop``/``main_status``,
no scheduler entry point -- calls into it. That is deliberate, not an
oversight: ``docs/paper-day-recovery/design.md`` ("Ordering (N4)") places the
recovery verb at P2, gated behind P0 (atomic lock write, landed in
``b55912e``) and P1 (mode matrix + review-only non-transmission tests, not yet
merged as of this module). Wiring this into an operator-reachable path before
P1 lands is exactly what N4 forbids. If you are looking for how to *call*
this, you are early -- see design.md's ordering table first.

The nine requirements below are ``docs/paper-day-recovery/decisions.md``'s
"Recovery acceptance bar (binding)", implemented as independent, synthetically
testable check functions plus a thin orchestrator
(:func:`evaluate_recovery_acceptance_bar`) that runs all nine and returns a
structured :class:`RecoveryAcceptanceResult` -- never a single bool, because
an operator (or a future caller) needs to see *which* requirement failed and
why, not just that recovery was refused.

Known, explicitly-flagged gap (requirement 4): decisions.md's wording is
unconditional -- "prove no unmatched ticks and no opening outbox records
remain" -- so :func:`check_no_unmatched_ticks_or_opening_outbox` fails closed
on *any* blocking record, regardless of which session produced it. That
conservatism is not free: neither ``ExecutionOutbox`` implementation gives a
caller a safe way to tell "this blocking record belongs to a long-resolved,
irrelevant session" from "this is live and blocking right now".
``engine.options.execution_outbox.ExecutionOutbox`` (the still-unwired saga
tracker) has zero ``session_id`` occurrences in its 568 lines. ``session_id``
*is* recorded per-record by ``engine.options.order_outbox.ExecutionOutbox``,
but ``blocking_records()``/``assert_clear()`` never consult it -- the same shape
of gap ``open-questions.md`` Defect #6 names at ``cycle_adapter.py:777``
(``unmatched()`` called with no session filter, so a prior session's scans
block the next one). Until one of those closes, a real caller of this
function has no way to safely narrow the check to "this session only" --
this is a narrow, explicitly-flagged limitation of the current outbox
implementations, not something this function papers over.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive import ArchiveError, ArchiveManifest, archive_before_mutation
from .paperday import GATE_CLOSED

__all__ = [
    "REQUIREMENT_1_EXCLUSIVE_LOCK",
    "REQUIREMENT_2_SESSION_LEASE_PROCESS_IDENTITY",
    "REQUIREMENT_3_READABLE_KNOWN_STATE",
    "REQUIREMENT_4_NO_UNMATCHED_TICKS_OR_OPENING_OUTBOX",
    "REQUIREMENT_5_BROKER_RECONCILIATION",
    "REQUIREMENT_6_FENCING_TOKEN_CAS",
    "REQUIREMENT_7_ARCHIVE_EVIDENCE_BEFORE_MUTATION",
    "REQUIREMENT_8_PERSIST_REASON_AND_RECONCILIATION_RECEIPT",
    "REQUIREMENT_9_ENTRY_AUTHORITY_CLOSED",
    "BrokerReconciliationOutcome",
    "RecoveryAttempt",
    "RecoveryAcceptanceResult",
    "RecoveryCheck",
    "SessionIdentity",
    "check_archive_evidence_before_mutation",
    "check_broker_reconciliation",
    "check_exclusive_recovery_lock",
    "check_fencing_token_cas",
    "check_no_unmatched_ticks_or_opening_outbox",
    "check_readable_known_state",
    "check_session_lease_process_identity",
    "evaluate_recovery_acceptance_bar",
    "persist_reason_and_reconciliation_receipt",
]

REQUIREMENT_1_EXCLUSIVE_LOCK = "1_exclusive_lock"
REQUIREMENT_2_SESSION_LEASE_PROCESS_IDENTITY = "2_session_lease_process_identity"
REQUIREMENT_3_READABLE_KNOWN_STATE = "3_readable_known_state"
REQUIREMENT_4_NO_UNMATCHED_TICKS_OR_OPENING_OUTBOX = "4_no_unmatched_ticks_or_opening_outbox"
REQUIREMENT_5_BROKER_RECONCILIATION = "5_broker_reconciliation"
REQUIREMENT_6_FENCING_TOKEN_CAS = "6_fencing_token_cas"
REQUIREMENT_7_ARCHIVE_EVIDENCE_BEFORE_MUTATION = "7_archive_evidence_before_mutation"
REQUIREMENT_8_PERSIST_REASON_AND_RECONCILIATION_RECEIPT = (
    "8_persist_reason_and_reconciliation_receipt"
)
REQUIREMENT_9_ENTRY_AUTHORITY_CLOSED = "9_entry_authority_closed"


@dataclass(frozen=True)
class RecoveryCheck:
    """One requirement of the acceptance bar, and how it went."""

    requirement: str
    passed: bool
    detail: str
    evidence: Any = None


@dataclass(frozen=True)
class SessionIdentity:
    """The three identity components decisions.md item 2 names: session,
    lease, and process -- deliberately distinct from the fencing token, which
    item 6 checks separately as a compare-and-swap against a freshly re-read
    value, not as part of this fixed identity tuple."""

    session_id: str | None
    lease_nonce: str | None
    process_id: int | None


@dataclass(frozen=True)
class BrokerReconciliationOutcome:
    """The shape a BrokerReconciler-like caller is expected to hand back.

    Deliberately not :class:`engine.options.broker_reconciliation.BrokerReconciler`'s
    own return type: that class matches one expected combo against one
    broker observation and has zero production callers today
    (``open-questions.md``). This is the narrower outcome a recovery-scoped
    reconciliation pass (positions + orders + executions, all at once) would
    need to report; production wiring is deferred, out of scope for this
    lane.
    """

    agrees: bool
    detail: str


# ---------------------------------------------------------------------------
# Requirement 1 -- lock exclusively
# ---------------------------------------------------------------------------


def check_exclusive_recovery_lock(acquire: Callable[[], bool]) -> RecoveryCheck:
    """``acquire`` is expected to behave like ``paperday._acquire_lock_atomically``:
    return ``True`` on a clean exclusive acquire, ``False`` when another
    holder already has it, never raise for ordinary contention."""

    try:
        acquired = acquire()
    except Exception as exc:  # noqa: BLE001 - any acquisition failure refuses
        return RecoveryCheck(
            REQUIREMENT_1_EXCLUSIVE_LOCK,
            False,
            f"lock acquisition raised: {exc}",
        )
    if not acquired:
        return RecoveryCheck(
            REQUIREMENT_1_EXCLUSIVE_LOCK,
            False,
            "recovery lock is already held by another operation",
        )
    return RecoveryCheck(REQUIREMENT_1_EXCLUSIVE_LOCK, True, "recovery lock acquired exclusively")


# ---------------------------------------------------------------------------
# Requirement 2 -- exact session, lease, and process identity
# ---------------------------------------------------------------------------


def check_session_lease_process_identity(
    *, expected: SessionIdentity, observed: SessionIdentity | None
) -> RecoveryCheck:
    if observed is None:
        return RecoveryCheck(
            REQUIREMENT_2_SESSION_LEASE_PROCESS_IDENTITY,
            False,
            "no identity observed to verify against",
        )
    missing = [
        name
        for name, value in (
            ("session_id", observed.session_id),
            ("lease_nonce", observed.lease_nonce),
            ("process_id", observed.process_id),
        )
        if value is None
    ]
    if missing:
        return RecoveryCheck(
            REQUIREMENT_2_SESSION_LEASE_PROCESS_IDENTITY,
            False,
            f"observed identity missing component(s): {', '.join(missing)}",
        )
    if observed != expected:
        return RecoveryCheck(
            REQUIREMENT_2_SESSION_LEASE_PROCESS_IDENTITY,
            False,
            "identity mismatch: observed session/lease/process identity does not "
            "match the identity this recovery attempt was authorized against",
        )
    return RecoveryCheck(
        REQUIREMENT_2_SESSION_LEASE_PROCESS_IDENTITY,
        True,
        "session, lease, and process identity match exactly",
    )


# ---------------------------------------------------------------------------
# Requirement 3 -- reject unreadable or unknown state
# ---------------------------------------------------------------------------


def check_readable_known_state(
    *, state: Mapping[str, Any] | None, supported_schema_versions: frozenset[int]
) -> RecoveryCheck:
    """Mirrors ``paperday._read_json``'s collapse of missing/corrupt to
    ``None`` -- the caller is expected to have already done that collapse
    (or supplied a parsed dict), and this function refuses either way rather
    than trying to disambiguate after the fact."""

    if state is None:
        return RecoveryCheck(
            REQUIREMENT_3_READABLE_KNOWN_STATE,
            False,
            "state is unreadable, missing, or not a JSON object",
        )
    schema_version = state.get("schema_version")
    if schema_version not in supported_schema_versions:
        return RecoveryCheck(
            REQUIREMENT_3_READABLE_KNOWN_STATE,
            False,
            f"unknown schema_version {schema_version!r} "
            f"(supported: {sorted(supported_schema_versions)})",
        )
    return RecoveryCheck(REQUIREMENT_3_READABLE_KNOWN_STATE, True, "state is readable and known")


# ---------------------------------------------------------------------------
# Requirement 4 -- no unmatched ticks / no opening outbox records
# ---------------------------------------------------------------------------


def check_no_unmatched_ticks_or_opening_outbox(
    *,
    session_id: str,
    unmatched_ticks: Sequence[Any],
    outbox_blocking_records: Sequence[Mapping[str, Any]],
) -> RecoveryCheck:
    """Fails on *any* unmatched tick or blocking outbox record, unconditionally
    -- decisions.md item 4 says "no ... remain", not "none for this session".

    ``session_id`` is accepted (and expected to already have been used by the
    caller to scope ``unmatched_ticks``, e.g. via
    ``scheduler.find_unmatched_ticks(paths, session_id=...)``, which does
    support that filter) but this function does not use it to *narrow* the
    outbox side of the check -- see the module docstring for why neither
    ``ExecutionOutbox`` implementation makes that safe to do today. It is kept
    as a parameter so a future, safer implementation of this check can
    compare it against outbox records once that gap closes, without changing
    every call site's signature again.
    """

    del session_id  # not consulted -- see docstring above
    unmatched = list(unmatched_ticks)
    if unmatched:
        return RecoveryCheck(
            REQUIREMENT_4_NO_UNMATCHED_TICKS_OR_OPENING_OUTBOX,
            False,
            f"{len(unmatched)} unmatched tick(s) with no terminal lifecycle event",
        )
    blocking = list(outbox_blocking_records)
    if blocking:
        return RecoveryCheck(
            REQUIREMENT_4_NO_UNMATCHED_TICKS_OR_OPENING_OUTBOX,
            False,
            f"{len(blocking)} outbox record(s) still block recovery",
        )
    return RecoveryCheck(
        REQUIREMENT_4_NO_UNMATCHED_TICKS_OR_OPENING_OUTBOX,
        True,
        "no unmatched ticks, no opening outbox records",
    )


# ---------------------------------------------------------------------------
# Requirement 5 -- reconcile broker positions, orders, executions
# ---------------------------------------------------------------------------


def check_broker_reconciliation(
    reconcile: Callable[[], BrokerReconciliationOutcome],
) -> RecoveryCheck:
    """``reconcile`` stands in for a BrokerReconciler-shaped call a real
    caller would make. Not wired to any real broker client here (out of
    scope; see this module's docstring and the report this lane produced)."""

    outcome = reconcile()
    if not outcome.agrees:
        return RecoveryCheck(
            REQUIREMENT_5_BROKER_RECONCILIATION,
            False,
            f"broker reconciliation disagrees: {outcome.detail}",
            evidence=outcome,
        )
    return RecoveryCheck(
        REQUIREMENT_5_BROKER_RECONCILIATION,
        True,
        f"broker reconciliation agrees: {outcome.detail}",
        evidence=outcome,
    )


# ---------------------------------------------------------------------------
# Requirement 6 -- fencing-token CAS
# ---------------------------------------------------------------------------


def check_fencing_token_cas(
    *, expected_token: str, observed_token: str | None
) -> RecoveryCheck:
    """A compare-and-swap precondition check: ``observed_token`` is expected
    to be freshly re-read immediately before the caller would mutate, so a
    token that raced out from under ``expected_token`` is caught here rather
    than assumed away by requirement 2's earlier identity check."""

    if observed_token is None:
        return RecoveryCheck(
            REQUIREMENT_6_FENCING_TOKEN_CAS, False, "no fencing token observed"
        )
    if observed_token != expected_token:
        return RecoveryCheck(
            REQUIREMENT_6_FENCING_TOKEN_CAS,
            False,
            "stale fencing token: another session holds authority",
        )
    return RecoveryCheck(REQUIREMENT_6_FENCING_TOKEN_CAS, True, "fencing token matches (CAS ok)")


# ---------------------------------------------------------------------------
# Requirement 7 -- archive evidence before mutation
# ---------------------------------------------------------------------------


def check_archive_evidence_before_mutation(
    *,
    source: Path,
    archive_dir: Path,
    reason: str,
    now: dt.datetime,
    archive_fn: Callable[..., ArchiveManifest] = archive_before_mutation,
) -> RecoveryCheck:
    try:
        manifest = archive_fn(source, archive_dir=archive_dir, reason=reason, now=now)
    except (ArchiveError, ValueError) as exc:
        return RecoveryCheck(
            REQUIREMENT_7_ARCHIVE_EVIDENCE_BEFORE_MUTATION,
            False,
            f"archive-before-mutation refused: {exc}",
        )
    return RecoveryCheck(
        REQUIREMENT_7_ARCHIVE_EVIDENCE_BEFORE_MUTATION,
        True,
        f"evidence archived to {manifest.archive_path}",
        evidence=manifest,
    )


# ---------------------------------------------------------------------------
# Requirement 8 -- persist the reason and the reconciliation receipt
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        handle, name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(name)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def persist_reason_and_reconciliation_receipt(
    *,
    path: Path,
    reason: str,
    reconciliation: Mapping[str, Any],
    now: dt.datetime,
) -> RecoveryCheck:
    """Persist durably (decisions.md D11: "through a full cycle, in an
    immutable receipt" -- the exact failure ``write_gate`` has today, per
    ``open-questions.md`` Defect #2: it drops ``recovery_reason`` on the next
    write). This function does not touch ``gate.json`` at all -- it writes
    its own standalone receipt file -- precisely so it cannot inherit that
    defect."""

    if not isinstance(reason, str) or not reason.strip():
        return RecoveryCheck(
            REQUIREMENT_8_PERSIST_REASON_AND_RECONCILIATION_RECEIPT,
            False,
            "refusing to persist: recovery reason is empty",
        )
    payload = {
        "reason": reason,
        "reconciliation": dict(reconciliation),
        "persisted_at": now.isoformat(),
    }
    _atomic_write_json(path, payload)
    return RecoveryCheck(
        REQUIREMENT_8_PERSIST_REASON_AND_RECONCILIATION_RECEIPT,
        True,
        f"reason and reconciliation receipt persisted to {path}",
        evidence=payload,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryAttempt:
    """Every input the nine checks need, bundled for one evaluation call."""

    acquire_lock: Callable[[], bool]
    expected_identity: SessionIdentity
    observed_identity: SessionIdentity | None
    state: Mapping[str, Any] | None
    supported_schema_versions: frozenset[int]
    session_id: str
    unmatched_ticks: Sequence[Any]
    outbox_blocking_records: Sequence[Mapping[str, Any]]
    reconcile: Callable[[], BrokerReconciliationOutcome]
    expected_fencing_token: str
    observed_fencing_token: str | None
    archive_source: Path
    archive_dir: Path
    reason: str
    receipt_path: Path
    now: dt.datetime
    archive_fn: Callable[..., ArchiveManifest] = archive_before_mutation


@dataclass(frozen=True)
class RecoveryAcceptanceResult:
    """The structured verdict: which requirements passed, which failed and
    why, and the resulting entry-gate position -- always ``CLOSED``
    (requirement 9). ``entry_gate`` is a fixed constant on this dataclass,
    never computed from ``checks``, precisely so no future edit to the
    per-requirement checks can accidentally make it anything else."""

    checks: tuple[RecoveryCheck, ...]
    entry_gate: str

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def check(self, requirement: str) -> RecoveryCheck | None:
        return next((c for c in self.checks if c.requirement == requirement), None)


def evaluate_recovery_acceptance_bar(attempt: RecoveryAttempt) -> RecoveryAcceptanceResult:
    """Run all nine requirements and return a structured result.

    Requirement 8 is gated on requirement 7: the receipt is not persisted
    when archiving evidence failed, so a caller can never end up with a
    durable "recovery happened" record unaccompanied by the archived
    evidence that is supposed to back it.

    Requirement 9 is not "the ninth check" in the sense the other eight are
    -- it is the postcondition on this function itself. ``entry_gate`` below
    is hardcoded to :data:`engine.paperday.GATE_CLOSED` unconditionally, so
    there is no code path -- not even "every other check passed" -- that
    produces anything else. Opening entry authority is not this function's
    job; per decisions.md item 9, only a new, independently validated
    session does that.
    """

    lock_check = check_exclusive_recovery_lock(attempt.acquire_lock)
    identity_check = check_session_lease_process_identity(
        expected=attempt.expected_identity, observed=attempt.observed_identity
    )
    state_check = check_readable_known_state(
        state=attempt.state, supported_schema_versions=attempt.supported_schema_versions
    )
    outbox_check = check_no_unmatched_ticks_or_opening_outbox(
        session_id=attempt.session_id,
        unmatched_ticks=attempt.unmatched_ticks,
        outbox_blocking_records=attempt.outbox_blocking_records,
    )
    reconciliation_check = check_broker_reconciliation(attempt.reconcile)
    fencing_check = check_fencing_token_cas(
        expected_token=attempt.expected_fencing_token,
        observed_token=attempt.observed_fencing_token,
    )
    archive_check = check_archive_evidence_before_mutation(
        source=attempt.archive_source,
        archive_dir=attempt.archive_dir,
        reason=attempt.reason,
        now=attempt.now,
        archive_fn=attempt.archive_fn,
    )

    if archive_check.passed:
        reconciliation_evidence: dict[str, Any] = {}
        if reconciliation_check.evidence is not None:
            reconciliation_evidence = {
                "agrees": reconciliation_check.evidence.agrees,
                "detail": reconciliation_check.evidence.detail,
            }
        receipt_check = persist_reason_and_reconciliation_receipt(
            path=attempt.receipt_path,
            reason=attempt.reason,
            reconciliation=reconciliation_evidence,
            now=attempt.now,
        )
    else:
        receipt_check = RecoveryCheck(
            REQUIREMENT_8_PERSIST_REASON_AND_RECONCILIATION_RECEIPT,
            False,
            "skipped: archive-before-mutation (requirement 7) did not succeed; "
            "refusing to persist a receipt without first-preserved evidence",
        )

    return RecoveryAcceptanceResult(
        checks=(
            lock_check,
            identity_check,
            state_check,
            outbox_check,
            reconciliation_check,
            fencing_check,
            archive_check,
            receipt_check,
        ),
        entry_gate=GATE_CLOSED,
    )
