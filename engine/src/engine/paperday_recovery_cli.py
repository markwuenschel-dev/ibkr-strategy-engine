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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from .scheduler import SchedulerPaths, find_unmatched_ticks

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


def _make_lock_acquirer(lock_path: Path) -> Any:
    """A zero-arg callable matching ``check_exclusive_recovery_lock``'s
    contract: return True on a clean exclusive acquire, False when another
    recovery attempt already holds it, never raise for ordinary contention.

    Reuses ``paperday._acquire_lock_atomically`` -- the same fsynced
    temp-file-plus-``os.link`` primitive BLOCKER-1 requirement 1 fixed for
    ``session.lock`` -- so this new lock inherits the same torn-write
    immunity rather than reintroducing the bug this whole project exists to
    fix.
    """

    def acquire() -> bool:
        payload = json.dumps({"pid": os.getpid(), "acquired_at": utc_now().isoformat()})
        return _acquire_lock_atomically(lock_path, payload)

    return acquire


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
        acquire_lock=_make_lock_acquirer(_recovery_lock_path(paths)),
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
    )
    result = evaluate_recovery_acceptance_bar(attempt)
    applied = False if dry_run else apply_recovery_result(paths, result)
    return RecoveryOutcome(refused_reason=None, acceptance=result, applied=applied)


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
