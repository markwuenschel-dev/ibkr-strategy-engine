"""Wires the paper-day recovery verb (:mod:`engine.paperday_recovery`) into an
operator-reachable command: ``engine paperday-recover``.

``docs/paper-day-recovery/design.md``'s N4 ordering places this wiring at P2,
gated behind P0 (atomic lock write + corrupt-vs-missing identity, both
merged: PR #8 and PR #10) and P1 (mode matrix + review-only non-transmission
tests, merged: PR #11). Both are satisfied as of this module.

This is the ONLY place in the engine that is allowed to flip
``gate.json``'s ``recovery_required`` from ``True`` to ``False`` -- and it
only does so after :func:`engine.paperday_recovery.evaluate_recovery_acceptance_bar`
reports every one of the nine requirements passed. A single failing
requirement refuses the whole attempt and leaves every piece of state exactly
as found (decisions.md D4: no deletion of state files; requirement 9: entry
authority stays CLOSED regardless of outcome -- this module never writes
``entry_gate`` at all, only ``recovery_required``, so opening entry authority
remains, as decisions.md item 9 requires, the job of a new, independently
validated session starting afterward -- not this command).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive import ArchiveError, archive_before_mutation
from .config import EngineConfig
from .journal import OrderJournal, utc_now
from .paperday import PaperDayPaths, _acquire_lock_atomically, _read_json_or_corrupt
from .runtime import SubprocessProcessPort
from .paperday_recovery import (
    BrokerReconciliationOutcome,
    RecoveryAcceptanceResult,
    RecoveryAttempt,
    SessionIdentity,
    evaluate_recovery_acceptance_bar,
)
from .scheduler import SchedulerPaths, find_unmatched_ticks, identity_from_record

_SUPPORTED_SCHEMA_VERSIONS = frozenset({1})


def _recovery_lock_path(paths: PaperDayPaths) -> Path:
    return paths.root / "recovery.lock"


def _receipt_path(paths: PaperDayPaths, now: dt.datetime) -> Path:
    return (
        paths.root
        / "recovery-archive"
        / f"recovery-receipt-{now.strftime('%Y%m%d-%H%M%S')}.json"
    )


def _archive_dir(paths: PaperDayPaths) -> Path:
    return paths.root / "recovery-archive"


def _make_lock_acquirer(lock_path: Path, token: str) -> Any:
    """A zero-arg callable matching ``check_exclusive_recovery_lock``'s
    contract: return True on a clean exclusive acquire, False when another
    recovery attempt already holds it, never raise for ordinary contention.

    Reuses ``paperday._acquire_lock_atomically`` -- the same fsynced
    temp-file-plus-``os.link`` primitive BLOCKER-1 requirement 1 fixed for
    ``session.lock`` -- so this new lock inherits the same torn-write
    immunity rather than reintroducing the bug this whole project exists to
    fix.

    ``token`` is written into the lock payload so a caller can later tell,
    by reading the file back, whether THIS acquisition is still the one
    holding it -- see :func:`_release_lock_if_ours`. Two ordinary Python
    dicts loaded from the same on-disk JSON would compare equal on content
    alone; the token exists specifically so "is this my lock" is decidable
    without relying on that, since relying on it would make a same-process,
    same-pid retry after a bug indistinguishable from a genuinely different
    holder.
    """

    def acquire() -> bool:
        payload = json.dumps(
            {"pid": os.getpid(), "token": token, "acquired_at": utc_now().isoformat()}
        )
        return _acquire_lock_atomically(lock_path, payload)

    return acquire


def _release_lock_if_ours(lock_path: Path, token: str) -> None:
    """Release the recovery lock ONLY if it is still the exact acquisition
    this call made -- never unconditionally.

    An unconditional unlink in a ``finally`` would let a failed CONCURRENT
    attempt delete a genuinely still-running attempt's lock the moment the
    failed one exits -- exactly the split-brain scenario decisions.md's
    requirement 1 (lock exclusively) and requirement 6 (fencing-token CAS)
    exist to prevent. This only ever removes a lock this exact call is
    still holding, identified by ``token``, not by pid (a pid can be
    reused) and not by dict-equality of the whole payload (two acquisitions
    from the same process could otherwise look identical).
    """
    current, corrupt = _read_json_or_corrupt(lock_path)
    if corrupt or current is None:
        return
    if current.get("token") != token:
        return  # not ours -- someone else's lock, or already released and re-acquired
    with contextlib.suppress(FileNotFoundError):
        lock_path.unlink()


def _make_broker_reconciler(
    config: EngineConfig, broker_factory: Any
) -> Any:
    """Adapts the engine's existing, already-production ``options-positions``
    reconciliation path (``PositionStore.reconcile_against_broker``, the same
    code ``cmd_options_positions`` in cli.py calls) into the narrower
    ``Callable[[], BrokerReconciliationOutcome]`` shape requirement 5 expects.

    Deliberately NOT ``engine.options.broker_reconciliation.BrokerReconciler``
    -- that class matches one expected order combo against one broker
    observation and has zero production callers (open-questions.md); it is
    not shaped for a whole-book recovery reconciliation. This reuses the path
    that already runs successfully in production instead of building a new,
    untested one under time pressure.
    """
    from .options.positions import PositionStore

    def reconcile() -> BrokerReconciliationOutcome:
        from .options.adapters import read_open_orders

        store = PositionStore(config.state_dir / "positions.jsonl")
        journal = OrderJournal(config.journal_path)
        try:
            with broker_factory(config, journal) as broker:
                broker_positions = broker.positions()
                # Mirrors cli.py's own _open_orders_or_none: None means "could
                # not ask", () means "asked, nothing working" -- collapsing
                # them is how a live working order gets reported as absent.
                try:
                    open_orders = read_open_orders(getattr(broker, "ib", broker))
                except Exception:  # noqa: BLE001 - an unanswered question is not an answer of no
                    open_orders = None
                report = store.reconcile_against_broker(
                    broker_positions,
                    checked_at=utc_now(),
                    broker_orders=open_orders,
                )
        except Exception as exc:  # noqa: BLE001 - any broker failure is a disagreement, not a crash
            return BrokerReconciliationOutcome(
                agrees=False, detail=f"broker reconciliation raised: {exc}"
            )
        return BrokerReconciliationOutcome(agrees=report.agrees, detail=report.describe())

    return reconcile


def build_recovery_attempt(
    *,
    paths: PaperDayPaths,
    expected_session_id: str,
    expected_lease_nonce: str,
    expected_process_id: int,
    expected_fencing_token: str,
    reason: str,
    now: dt.datetime,
    config: EngineConfig,
    broker_factory: Any,
    lock_token: str,
) -> RecoveryAttempt:
    """Assemble a :class:`RecoveryAttempt` from live on-disk and broker state.

    The "expected" identity is supplied by the operator (CLI args), never
    reconstructed from disk (decisions.md D5: "operator-supplied hashes MUST
    NOT reconstruct missing authority state") -- the operator states which
    stuck session they intend to recover against, and this function verifies
    that against what is actually on disk right now, catching a race where
    it changed underneath them.
    """
    gate, gate_corrupt = _read_json_or_corrupt(paths.gate)
    state: dict[str, Any] | None = None if gate_corrupt else gate

    scheduler_paths = SchedulerPaths(root=paths.root)
    scheduler_record, scheduler_corrupt = _read_json_or_corrupt(scheduler_paths.pid)
    observed_identity: SessionIdentity | None = None
    if not scheduler_corrupt and scheduler_record is not None:
        observed_identity = SessionIdentity(
            session_id=scheduler_record.get("session_id"),
            lease_nonce=scheduler_record.get("nonce"),
            process_id=scheduler_record.get("pid"),
        )

    expected_identity = SessionIdentity(
        session_id=expected_session_id,
        lease_nonce=expected_lease_nonce,
        process_id=expected_process_id,
    )

    unmatched_ticks = find_unmatched_ticks(
        scheduler_paths, session_id=expected_session_id, lease_nonce=expected_lease_nonce
    )

    from .options.order_outbox import ExecutionOutbox

    outbox = ExecutionOutbox(config.state_dir / "execution-outbox")
    outbox_blocking = outbox.blocking_records()

    # CAS semantics (requirement 6, decisions.md item 6): expected_fencing_token
    # is what the OPERATOR asserts they observed earlier -- e.g. from
    # `paper-day-status` output gathered before deciding to recover -- never
    # read from the same live file this function also re-reads as "observed".
    # Deriving both from one read, at nearly the same instant, could never
    # actually catch a race; it would just compare a value to itself.
    observed_fencing_token = None if gate_corrupt else (gate or {}).get("fencing_token")

    return RecoveryAttempt(
        acquire_lock=_make_lock_acquirer(_recovery_lock_path(paths), lock_token),
        expected_identity=expected_identity,
        observed_identity=observed_identity,
        state=state,
        supported_schema_versions=_SUPPORTED_SCHEMA_VERSIONS,
        session_id=expected_session_id,
        unmatched_ticks=unmatched_ticks,
        outbox_blocking_records=outbox_blocking,
        reconcile=_make_broker_reconciler(config, broker_factory),
        expected_fencing_token=expected_fencing_token,
        observed_fencing_token=observed_fencing_token,
        archive_source=paths.gate,
        archive_dir=_archive_dir(paths),
        reason=reason,
        receipt_path=_receipt_path(paths, now),
        now=now,
    )


def target_process_is_still_alive(process_id: int, process_port: Any) -> bool:
    """Not one of decisions.md's 9 numbered requirements -- an additional
    guard this wiring layer adds on top of them, because recovering against
    a process that is provably still running would be exactly the
    split-brain scenario the acceptance bar's requirement 2 (identity match)
    cannot catch by itself: identity matching only proves the operator named
    the right session, not that it has actually died.

    Uses ``process_port.alive(pid)`` (``runtime.SubprocessProcessPort`` by
    default in production), which checks the OS process table's command
    line, not bare PID existence -- a PID number alone can be silently
    reused by an unrelated process (observed for real: pid 64020 was reused
    by ``neostack-mcp-proxy.exe`` four minutes after the paper-day
    controller that held it exited, 2026-08-20 incident). ``os.kill(pid, 0)``
    is deliberately not used here: on Windows, ``os.kill`` with an arbitrary
    signal number does not probe liveness the way it does on POSIX -- it can
    attempt to terminate the process. This engine runs on Windows.
    """
    return bool(process_port.alive(process_id))


@dataclass(frozen=True)
class RecoveryOutcome:
    """What actually happened, start to finish. ``refused_reason`` is set
    only for the pre-check this module adds (target still alive); everything
    else is visible on ``acceptance`` (may be ``None`` if refused before the
    acceptance bar ever ran)."""

    refused_reason: str | None
    acceptance: RecoveryAcceptanceResult | None
    applied: bool
    #: Per-file result of consuming the stale scheduler records this recovery
    #: resolved. Empty when the recovery did not apply (refused, failed, or a
    #: dry run) -- consumption is never attempted in those cases.
    stale_records: tuple[StaleRecordOutcome, ...] = ()


def run_recovery(
    *,
    paths: PaperDayPaths,
    expected_session_id: str,
    expected_lease_nonce: str,
    expected_process_id: int,
    expected_fencing_token: str,
    reason: str,
    now: dt.datetime,
    config: EngineConfig,
    broker_factory: Any,
    process_port: Any | None = None,
    dry_run: bool = False,
) -> RecoveryOutcome:
    """The one function that runs the whole recovery attempt end to end:
    the still-alive pre-check, the 9-point acceptance bar, and (only on a
    full pass, and only when ``dry_run`` is False) the single
    ``recovery_required`` write. Nothing else in the engine calls this --
    see the module docstring.

    ``dry_run=True`` still runs every requirement's real check (broker
    connection included) so the operator sees the true result, but
    unconditionally skips :func:`apply_recovery_result` -- it is never
    called at all, not called-and-made-a-no-op, so a bug in that function
    cannot leak a write through a dry run."""

    port = process_port if process_port is not None else SubprocessProcessPort()
    if target_process_is_still_alive(expected_process_id, port):
        return RecoveryOutcome(
            refused_reason=(
                f"refusing: process {expected_process_id} (asserted as the stuck "
                "session's owner) is still alive on this machine -- this is not "
                "a dead session to recover, or the operator named the wrong "
                "process. Recovery must not proceed against a live owner."
            ),
            acceptance=None,
            applied=False,
        )

    lock_token = uuid.uuid4().hex
    attempt = build_recovery_attempt(
        paths=paths,
        expected_session_id=expected_session_id,
        expected_lease_nonce=expected_lease_nonce,
        expected_process_id=expected_process_id,
        expected_fencing_token=expected_fencing_token,
        reason=reason,
        now=now,
        config=config,
        broker_factory=broker_factory,
        lock_token=lock_token,
    )
    try:
        result = evaluate_recovery_acceptance_bar(attempt)
        applied = False if dry_run else apply_recovery_result(paths, result)
    finally:
        # The recovery lock exists to serialize CONCURRENT attempts, not to
        # permanently block every attempt after the first. Release it once
        # this attempt (successful, refused, or dry-run) is fully done -- in
        # a finally, so a mid-evaluation exception can't leave it orphaned
        # either. Only ever releases OUR OWN acquisition (matched by
        # lock_token, see _release_lock_if_ours): a genuinely concurrent
        # attempt that lost the acquire race above never held this lock in
        # the first place, so it has nothing of its own to release here,
        # and cannot accidentally release the winner's lock out from under
        # it.
        _release_lock_if_ours(_recovery_lock_path(paths), lock_token)

    # Only a recovery that actually APPLIED may consume the records that
    # caused it. A refusal, a failed requirement, or a dry run must leave
    # every file exactly as found -- so this is gated on `applied`, not on
    # `result.all_passed`, which would let a dry run mutate state.
    stale_records: tuple[StaleRecordOutcome, ...] = ()
    if applied:
        stale_records = consume_stale_scheduler_records(
            paths=paths, process_port=port, now=now, reason=reason
        )
    return RecoveryOutcome(
        refused_reason=None,
        acceptance=result,
        applied=applied,
        stale_records=stale_records,
    )


def apply_recovery_result(paths: PaperDayPaths, result: RecoveryAcceptanceResult) -> bool:
    """The ONLY write this module makes to ``gate.json`` itself: flips
    ``recovery_required`` to ``False`` when every requirement passed, and
    changes nothing else -- ``entry_gate`` is left exactly as read.

    Returns ``True`` if the mutation was applied, ``False`` if refused
    (either because the result did not pass, or because the gate could not
    be re-read cleanly at write time -- a corrupt gate at this point refuses
    just like every other corrupt-identity path in this project, it is never
    treated as "nothing to preserve").
    """
    if not result.all_passed:
        return False
    current, corrupt = _read_json_or_corrupt(paths.gate)
    if corrupt or current is None:
        return False
    current["recovery_required"] = False
    current["recovery_cleared_reason"] = next(
        (c.detail for c in result.checks if c.requirement == "8_persist_reason_and_reconciliation_receipt"),
        "recovery acceptance bar passed",
    )
    _atomic_write_json(paths.gate, current)
    return True


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        handle, name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
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


def format_result(result: RecoveryAcceptanceResult) -> str:
    lines = ["PAPER DAY RECOVERY", ""]
    for check in result.checks:
        mark = "ok" if check.passed else "!!"
        lines.append(f"  {mark} {check.requirement:45s} {check.detail}")
    lines.append("")
    lines.append("ALL REQUIREMENTS PASSED" if result.all_passed else "REFUSED -- see failing requirement(s) above")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Consuming the stale scheduler records a completed recovery has resolved
# ---------------------------------------------------------------------------
#
# Why this exists (2026-08-21, third dirty stop in eight days):
#
# ``scheduler.pid`` and ``scheduler.claim`` are the two durable statements
# that a persistent scheduler was started. Neither is removed when the
# scheduler dies without a clean terminal receipt:
#
#   * stop  -- ``scheduler.drain_and_stop`` returns STOP_DIRTY and pointedly
#     does NOT unlink ``paths.pid`` (scheduler.py:1702-1707); only the
#     proven-clean branch unlinks. ``paperday``'s stop then turns any failed
#     "scheduler" step into ``recovery_required = True``
#     (paperday.py:2085-2091).
#   * start -- ``scheduler._claim_start`` refuses outright on a foreign claim
#     and will not remove it without a clean terminal receipt
#     (scheduler.py:879-886).
#
# Both are correct the FIRST time: a scheduler really did die mid-tick and its
# final tick really is unaccounted for. The defect is that neither is
# idempotent. The evidence that raised the alarm stays on disk after the alarm
# has been answered, so it re-fires on every subsequent session forever. That
# is what happened here: pid 240328 / session paperday-20260819-fa4081f5 died
# on 2026-08-19 and was still latching STOP_DIRTY on 2026-08-21, across six
# separate recovery attempts, because the recovery verb cleared the flag but
# never touched the record that regenerates it.
#
# The fix is deliberately NOT "let stop age out old records". Teaching the
# safety-critical stop path a new way to say "clean" is how this repo has
# repeatedly ended up with guards that pin nothing. Instead the record is
# consumed by the one operation that has already PROVEN it resolved: a
# recovery attempt that passed all nine requirements. Every precondition below
# is re-proven here rather than inherited, because the acceptance bar is
# scoped to the session the operator named, which is not necessarily the
# session named in the scheduler record.


@dataclass(frozen=True)
class StaleRecordOutcome:
    """What consumption did to one scheduler record. ``consumed`` is True only
    when the file was archived AND removed."""

    path: Path
    consumed: bool
    detail: str
    archive_path: Path | None = None


def _refused(path: Path, detail: str) -> StaleRecordOutcome:
    return StaleRecordOutcome(path=path, consumed=False, detail=detail)


def consume_stale_scheduler_records(
    *,
    paths: PaperDayPaths,
    process_port: Any,
    now: dt.datetime,
    reason: str,
) -> tuple[StaleRecordOutcome, ...]:
    """Archive-then-remove the scheduler records a passed recovery resolved.

    Call ONLY after :func:`evaluate_recovery_acceptance_bar` reported
    ``all_passed`` and the ``recovery_required`` write was applied. Refuses,
    per file, on anything it cannot prove; a refusal leaves that file exactly
    as found (decisions.md D4 -- state files are archived, never blind-deleted)
    and never raises, because a failure to tidy up must not turn an otherwise
    successful recovery into an error.

    The five things re-proven here before ``scheduler.pid`` is touched:

    1. the file exists (nothing to do is not a failure);
    2. it is readable JSON -- a corrupt record is refused, not assumed empty,
       exactly as BLOCKER-1 requirement 2 requires everywhere else;
    3. it names a session AND a nonce (``identity_from_record``); an
       unidentifiable record is a stranger's by default and is left alone;
    4. its PID is proven dead through the same command-line-checking
       ``process_port.alive`` the live-owner pre-check uses -- never bare PID
       existence, since PID numbers get reused (2026-08-20: pid 64020 was
       reused by an unrelated process four minutes after the controller exited);
    5. the record's OWN session/nonce has zero unmatched ticks -- consuming a
       record whose ticks were never checked would be precisely the "smooth
       over unreconciled broker work" failure this project exists to prevent.

    Through :func:`run_recovery` all five are already guaranteed before this
    is ever called: :func:`build_recovery_attempt` derives the bar's
    ``observed_identity`` from ``scheduler.pid`` itself, so requirement 2
    refuses any record that is corrupt, nameless, or names a different
    session/pid than the operator asserted, and requirements 2 and 4 then run
    the liveness and unmatched-tick checks against that same identity. These
    checks are therefore defence in depth on an operation that DELETES state,
    not the primary gate -- they are stated and tested (by direct call, in
    ``TestConsumptionRefusesWhatItCannotProve``) so that a future caller which
    is not ``run_recovery`` cannot reach the delete without them.

    ``scheduler.claim`` is consumed only when it names the exact identity just
    proven dead and resolved above. A claim naming anything else is refused --
    it may belong to a live supervisor this function knows nothing about.
    """

    scheduler_paths = SchedulerPaths(root=paths.root)
    outcomes: list[StaleRecordOutcome] = []
    pid_path = scheduler_paths.pid

    if not pid_path.exists():
        return (_refused(pid_path, "no scheduler.pid on disk -- nothing to consume"),)

    record, corrupt = _read_json_or_corrupt(pid_path)
    if corrupt or record is None:
        return (
            _refused(
                pid_path,
                "refused: scheduler.pid exists but is not readable JSON -- what "
                "would be discarded cannot be named, so it is left untouched "
                "for a human to resolve",
            ),
        )

    identity = identity_from_record(record)
    if identity is None:
        return (
            _refused(
                pid_path,
                "refused: scheduler.pid names no session/nonce -- an "
                "unidentifiable record is a stranger's by default",
            ),
        )

    pid = record.get("pid")
    if type(pid) is not int or pid <= 0:
        return (
            _refused(pid_path, f"refused: scheduler.pid carries no usable pid ({pid!r})"),
        )

    if process_port.alive(pid):
        return (
            _refused(
                pid_path,
                f"refused: pid {pid} ({identity.session_id}) is still alive on "
                "this machine -- this is a running scheduler, not a stale "
                "record, and removing its record would strand it",
            ),
        )

    unmatched = find_unmatched_ticks(
        scheduler_paths,
        session_id=identity.session_id,
        lease_nonce=identity.nonce,
    )
    if unmatched:
        return (
            _refused(
                pid_path,
                f"refused: {len(unmatched)} unmatched tick(s) remain for "
                f"{identity.session_id}:{identity.nonce} -- that session's "
                "broker effects are still unaccounted for, so its record must "
                "stay on disk and keep raising recovery",
            ),
        )

    pid_reason = (
        f"{now:%Y-%m-%d} recovery consumed the stale scheduler record for "
        f"{identity.session_id}:{identity.nonce} (pid {pid}): the recovery "
        f"acceptance bar passed in full, the pid is proven dead via the OS "
        f"process table, and that session has zero unmatched ticks. Retaining "
        f"it would re-latch STOP_DIRTY on every future stop. Recovery reason: "
        f"{reason}"
    )
    try:
        manifest = archive_before_mutation(
            pid_path, archive_dir=_archive_dir(paths), reason=pid_reason, now=now
        )
    except ArchiveError as exc:
        return (
            _refused(
                pid_path,
                f"refused: could not archive scheduler.pid ({exc}) -- not "
                "removed; nothing is discarded that was not first preserved",
            ),
        )

    try:
        pid_path.unlink()
    except OSError as exc:
        return (
            _refused(
                pid_path,
                f"archived to {manifest.archive_path} but could not be removed "
                f"({exc}) -- the latch is still armed",
            ),
        )

    outcomes.append(
        StaleRecordOutcome(
            path=pid_path,
            consumed=True,
            detail=(
                f"archived and cleared: {identity.session_id}:{identity.nonce} "
                f"(pid {pid}), 0 unmatched ticks, proven dead"
            ),
            archive_path=manifest.archive_path,
        )
    )
    outcomes.append(
        _consume_matching_claim(
            paths=paths,
            scheduler_paths=scheduler_paths,
            identity=identity,
            now=now,
            reason=pid_reason,
        )
    )
    return tuple(outcomes)


def _consume_matching_claim(
    *,
    paths: PaperDayPaths,
    scheduler_paths: SchedulerPaths,
    identity: Any,
    now: dt.datetime,
    reason: str,
) -> StaleRecordOutcome:
    """Consume ``scheduler.claim`` only when it names ``identity`` exactly.

    The claim is the START-side half of the same latch: ``_claim_start``
    refuses to spawn a scheduler while a foreign claim sits on disk
    (scheduler.py:879-886). Clearing the pid record without clearing a
    matching claim would fix stop and leave start blocked -- the manual
    two-step an operator had to perform by hand on 2026-08-21.
    """
    claim_path = scheduler_paths.claim
    if not claim_path.exists():
        return _refused(claim_path, "no scheduler.claim on disk -- nothing to consume")

    claim, corrupt = _read_json_or_corrupt(claim_path)
    if corrupt or claim is None:
        return _refused(
            claim_path,
            "refused: scheduler.claim exists but is not readable JSON -- left "
            "untouched for a human to resolve",
        )

    if (
        claim.get("session_id") != identity.session_id
        or claim.get("nonce") != identity.nonce
    ):
        return _refused(
            claim_path,
            "refused: scheduler.claim names a different identity than the "
            f"{identity.session_id}:{identity.nonce} record just consumed -- it "
            "may belong to a supervisor this recovery knows nothing about",
        )

    try:
        manifest = archive_before_mutation(
            claim_path, archive_dir=_archive_dir(paths), reason=reason, now=now
        )
    except ArchiveError as exc:
        return _refused(
            claim_path,
            f"refused: could not archive scheduler.claim ({exc}) -- not removed",
        )

    try:
        claim_path.unlink()
    except OSError as exc:
        return _refused(
            claim_path,
            f"archived to {manifest.archive_path} but could not be removed ({exc})",
        )

    return StaleRecordOutcome(
        path=claim_path,
        consumed=True,
        detail=(
            f"archived and cleared: start claim for "
            f"{identity.session_id}:{identity.nonce}"
        ),
        archive_path=manifest.archive_path,
    )
