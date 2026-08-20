"""The daily paper-trading session controller.

One deep module behind three thin PowerShell wrappers (``bin\\start-paper-day.ps1``,
``bin\\stop-paper-day.ps1``, ``bin\\paper-day-status.ps1``). Everything the wrappers
print comes from here, and everything here is driven through injectable ports, so
the thirteen operational scenarios the controller exists for -- stale PIDs, dead
watchers, absent reviewers, leftover approvals -- are pytest cases rather than
incidents.

Three session states, and what each one licenses:

- ``PAPER_DAY_READY``     -- every required dependency is healthy; the entry gate
  is OPEN and armed opening trades may proceed (through every existing engine
  gate, which this module does not weaken).
- ``PAPER_DAY_DEGRADED``  -- something an *opening* trade needs is unhealthy
  (reviewer absent, marking refused, watcher missing), but the book itself is
  trustworthy. Management, exits, cancels and reconciliation all still run;
  the entry gate is PROOF_ONLY, so unarmed passes work and armed entries refuse.
- ``PAPER_DAY_BLOCKED``   -- the book itself cannot be trusted (no broker, bad
  config, failed reconciliation). The entry gate is CLOSED: no proposals either.

The gate file is *enforced*, not advisory: :func:`entry_gate_preflight` is wired
into the strategy CLI as the runner's ``entry_preflight``, which runs after risk
and the governor and **before** a verification proposal is filed -- so a CLOSED
gate stops new proposals as well as new orders, while management and exits are
untouched (the preflight only ever sees entry candidates, by construction of
``run_once``).

Fail-closed inheritance: this module refuses live endpoints by *constructing*
:class:`engine.config.EngineConfig`, whose ``__post_init__`` raises on any
non-paper port. There is no second port list here to drift.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import platform
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import _collabkit
from .errors import EngineError
from .runtime import (
    EngineCommandResult,
    EngineCommandRunner,
    SubprocessProcessPort,
    _engine_dir,
    default_tcp_probe,
)

__all__ = [
    "PaperDayPaths",
    "Check",
    "StartReport",
    "StopReport",
    "StatusReport",
    "PaperDayController",
    "entry_gate_preflight",
    "effective_configuration_fingerprint",
    "READY",
    "DEGRADED",
    "BLOCKED",
    "STOPPED",
    "GATE_OPEN",
    "GATE_PROOF_ONLY",
    "GATE_CLOSED",
    "GATE_SCHEMA_VERSION",
    "GATE_CONFIGURATION_FINGERPRINT",
    "MANDATE_MANAGE_ONLY",
    "MANDATE_FULL",
    "main_start",
    "main_stop",
    "main_status",
    # Re-exported from :mod:`engine.runtime`, which now owns the shared process
    # primitives so a scheduler can use them without importing this module.
    # Callers that imported them from here keep working.
    "SubprocessProcessPort",
    "EngineCommandResult",
    "EngineCommandRunner",
    "default_tcp_probe",
]

READY = "PAPER_DAY_READY"
DEGRADED = "PAPER_DAY_DEGRADED"
BLOCKED = "PAPER_DAY_BLOCKED"
STOPPED = "PAPER_DAY_STOPPED"

#: The entry-gate positions the preflight enforces. OPEN requires READY;
#: PROOF_ONLY lets unarmed passes do everything (including filing verification
#: proposals) while refusing armed entries; CLOSED refuses entry consideration
#: entirely, which is what "prevent creation of new verification proposals"
#: means in code.
GATE_OPEN = "OPEN"
GATE_PROOF_ONLY = "PROOF_ONLY"
GATE_CLOSED = "CLOSED"
GATE_SCHEMA_VERSION = 1
GATE_CONFIGURATION_FINGERPRINT = "configuration_fingerprint"
GATE_STATE_DIR = "state_dir"
GATE_POLICY_SHA256 = "policy_sha256"
GATE_CATALOG_SHA256 = "catalog_sha256"
GATE_CONFIG_SHA256 = "config_sha256"
GATE_SESSION_DATE = "session_date"
GATE_AUTHORITY_REQUIRED = "authority_required"
GATE_RECOVERY_REQUIRED = "recovery_required"
GATE_CONTROLLER_PID = "controller_pid"
GATE_SCHEDULER_IDENTITY = "scheduler_identity"
GATE_REVIEWER_LIVENESS_EPOCH = "reviewer_liveness_epoch"
GATE_REVIEWER_LIVENESS_AT = "reviewer_liveness_at"
AUTHORITY_LIVENESS_TTL_SECONDS = 15 * 60
MANDATE_MANAGE_ONLY = "MANAGE_ONLY"
MANDATE_FULL = "FULL"
_MANDATES = frozenset({MANDATE_MANAGE_ONLY, MANDATE_FULL})

EXIT_READY = 0
EXIT_DEGRADED = 10
EXIT_BLOCKED = 20
EXIT_STOPPED = 0
EXIT_STOP_DIRTY = 10

_WATCHER_NEEDLE = "watch-for-claude-handoffs.py"
_REVIEWER_NEEDLES = ("watch-for-grok-handoffs.py", "autonomous-reviewer-watch.py")


# ---------------------------------------------------------------------------
# paths and small records
# ---------------------------------------------------------------------------


# ``_engine_dir`` now lives in ``engine.runtime`` (imported above) because
# ``EngineCommandRunner`` needs it and that module may not import this one.
# ``runtime.py`` sits in the same package directory as this file, so
# ``Path(__file__).resolve().parents[2]`` resolves to the identical ``engine/``
# directory -- the anchor, and therefore the state dir, is unchanged.


def _repo_root() -> Path:
    return _engine_dir().parent


@dataclass(frozen=True)
class PaperDayPaths:
    """Every file the controller owns, in one place."""

    state_dir: Path

    def __post_init__(self) -> None:
        """Reject split-brain state roots before any state can be written.

        A relative state path is interpreted relative to whichever process
        happens to launch the wrapper.  That is unacceptable for a fenced
        paper-day session: the controller, scheduler, and options command must
        all address the same durable authority.  Callers that want a relative
        convenience path must resolve it before constructing this value.
        """
        resolved = Path(self.state_dir)
        if not resolved.is_absolute():
            raise ValueError(
                f"paper-day StateDir must be absolute, got {self.state_dir!s}"
            )
        object.__setattr__(self, "state_dir", resolved)

    @classmethod
    def default(cls) -> "PaperDayPaths":
        return cls(state_dir=_engine_dir() / ".engine")

    @property
    def root(self) -> Path:
        return self.state_dir / "paperday"

    @property
    def lock(self) -> Path:
        return self.root / "session.lock"

    @property
    def watcher_pid(self) -> Path:
        return self.root / "watcher.pid"

    @property
    def gate(self) -> Path:
        return self.root / "gate.json"

    @property
    def last_verification(self) -> Path:
        return self.root / "last-verification.json"

    @property
    def last_shutdown(self) -> Path:
        return self.root / "last-shutdown.json"

    @property
    def summaries(self) -> Path:
        return self.root / "summaries"

    @property
    def watcher_log(self) -> Path:
        return self.root / "watcher.log"

    @property
    def verification_ledger(self) -> Path:
        return self.state_dir / "verification"

    @property
    def journal(self) -> Path:
        return self.state_dir / "orders.jsonl"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def effective_configuration_fingerprint(
    base: str,
    *,
    policy_sha256: str,
    catalog_sha256: str,
    config_sha256: str,
) -> str:
    """Bind the operator's base fingerprint to FULL authority artifacts."""

    if not isinstance(base, str) or not base.strip():
        raise ValueError("base configuration fingerprint must be non-empty")
    return hashlib.sha256(
        json.dumps(
            {
                "base": base,
                "autotrader_policy_sha256": policy_sha256,
                "catalog_sha256": catalog_sha256,
                "config_sha256": config_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Compatibility writer whose publication is atomic by construction."""
    _atomic_write_json(path, payload)


def _validate_hash(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    rendered = value.strip().lower()
    if not rendered:
        raise ValueError(f"{name} must be non-empty when supplied")
    if not re.fullmatch(r"[0-9a-f]{64}", rendered):
        raise ValueError(f"{name} must be a 64-character SHA-256 digest")
    return rendered


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Publish controller state without exposing a torn JSON record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def _acquire_lock_atomically(path: Path, payload: str) -> bool:
    """Create ``path`` exclusively, with no torn-file window.

    (BLOCKER-1, ``docs/paper-day-recovery/open-questions.md``: the previous
    writer was a bare ``os.open(O_CREAT|O_EXCL) + write`` with no fsync and
    no atomic publish -- a crash between create and flush left a zero-byte or
    partial lock on disk, which ``_read_json`` cannot tell apart from a
    deliberately corrupted one, and which every recovery path then refuses
    with no way forward. This writes the full payload to a private, fsynced
    temp file first -- a crash there orphans an unreferenced temp file, never
    ``path`` itself -- then publishes with ``os.link``, which is atomic *and*
    preserves the exact mutual-exclusion contract ``O_CREAT|O_EXCL`` gave:
    it raises when ``path`` already exists, so two concurrent starts still
    cannot both win.

    Returns ``False`` (does not raise) when another process already holds
    the lock -- the caller's existing "another start acquired the lock
    concurrently" branch is unchanged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


@dataclass
class Check:
    """One start-time verification and how it went.

    ``severity`` states what a failure means for the day: ``"blocking"`` makes
    the session BLOCKED (the book cannot be trusted), ``"degrading"`` makes it
    DEGRADED (opens are unsafe, management is fine), ``"info"`` never changes
    the state -- it exists so the operator sees what was looked at.
    """

    name: str
    ok: bool
    detail: str
    severity: str = "blocking"

    def line(self) -> str:
        mark = "ok " if self.ok else ("!! " if self.severity == "blocking" else "~~ ")
        return f"  {mark}{self.name:<28} {self.detail}"


@dataclass
class StartReport:
    checks: list[Check] = field(default_factory=list)
    state: str = BLOCKED
    session_id: str = ""
    watcher_pid: int | None = None
    already_running: bool = False

    def add(self, name: str, ok: bool, detail: str, severity: str = "blocking") -> Check:
        check = Check(name=name, ok=ok, detail=detail, severity=severity)
        self.checks.append(check)
        return check

    def decide(self) -> str:
        if any(not c.ok and c.severity == "blocking" for c in self.checks):
            self.state = BLOCKED
        elif any(not c.ok and c.severity == "degrading" for c in self.checks):
            self.state = DEGRADED
        else:
            self.state = READY
        return self.state

    @property
    def exit_code(self) -> int:
        return {READY: EXIT_READY, DEGRADED: EXIT_DEGRADED}.get(self.state, EXIT_BLOCKED)

    def render(self) -> str:
        lines = ["PAPER DAY START", ""]
        lines += [check.line() for check in self.checks]
        lines += ["", self.state]
        return "\n".join(lines)


@dataclass
class StopReport:
    steps: list[Check] = field(default_factory=list)
    clean: bool = True

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.steps.append(Check(name=name, ok=ok, detail=detail, severity="info"))
        if not ok:
            self.clean = False

    @property
    def exit_code(self) -> int:
        return EXIT_STOPPED if self.clean else EXIT_STOP_DIRTY

    def render(self) -> str:
        lines = ["PAPER DAY STOP", ""]
        lines += [step.line() for step in self.steps]
        lines += ["", STOPPED if self.clean else f"{STOPPED} (dirty -- see above)"]
        return "\n".join(lines)


@dataclass
class StatusReport:
    rows: list[tuple[str, str]] = field(default_factory=list)

    def add(self, name: str, value: str) -> None:
        self.rows.append((name, value))

    def render(self) -> str:
        width = max((len(name) for name, _ in self.rows), default=0)
        return "\n".join(f"  {name:<{width}}  {value}" for name, value in self.rows)


# ---------------------------------------------------------------------------
# ports -- everything the tests need to fake lives behind one of these
#
# ``SubprocessProcessPort``, ``EngineCommandResult``, ``EngineCommandRunner``
# and ``default_tcp_probe`` now live in :mod:`engine.runtime` and are imported
# at the top of this module. They moved so a scheduler can reuse them without
# importing this controller; they are re-exported here (see ``__all__``) so
# every existing caller and test import keeps working unchanged.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# the enforced entry gate
# ---------------------------------------------------------------------------


def read_gate(paths: PaperDayPaths) -> dict[str, Any] | None:
    return _read_json(paths.gate)


def write_gate(
    paths: PaperDayPaths,
    *,
    entry_gate: str,
    state: str,
    session_id: str,
    now: dt.datetime,
    fencing_token: str | None = None,
    mandate: str = MANDATE_MANAGE_ONLY,
    configuration_fingerprint: str | None = None,
    policy_sha256: str | None = None,
    catalog_sha256: str | None = None,
    config_sha256: str | None = None,
    authority_required: bool = False,
    recovery_required: bool = False,
    controller_pid: int | None = None,
    scheduler_identity: dict[str, Any] | None = None,
    reviewer_liveness_epoch: str | None = None,
    reviewer_liveness_at: str | None = None,
) -> None:
    if mandate not in _MANDATES:
        raise ValueError(f"unknown paper-day mandate {mandate!r}")
    if configuration_fingerprint is not None and not configuration_fingerprint.strip():
        raise ValueError("paper-day configuration fingerprint must be non-empty when supplied")
    policy_sha256 = _validate_hash(policy_sha256, "policy_sha256")
    catalog_sha256 = _validate_hash(catalog_sha256, "catalog_sha256")
    config_sha256 = _validate_hash(config_sha256, "config_sha256")
    payload = {
        "schema_version": GATE_SCHEMA_VERSION,
        "entry_gate": entry_gate,
        "state": state,
        "session_id": session_id,
        "mandate": mandate,
        "fencing_token": fencing_token,
        "as_of": now.isoformat(),
        GATE_STATE_DIR: str(paths.state_dir),
        GATE_SESSION_DATE: now.astimezone(dt.timezone.utc).date().isoformat(),
        GATE_AUTHORITY_REQUIRED: bool(authority_required),
        GATE_RECOVERY_REQUIRED: bool(recovery_required),
    }
    if configuration_fingerprint is not None:
        payload[GATE_CONFIGURATION_FINGERPRINT] = configuration_fingerprint
    if policy_sha256 is not None:
        payload[GATE_POLICY_SHA256] = policy_sha256
    if catalog_sha256 is not None:
        payload[GATE_CATALOG_SHA256] = catalog_sha256
    if config_sha256 is not None:
        payload[GATE_CONFIG_SHA256] = config_sha256
    if controller_pid is not None:
        payload[GATE_CONTROLLER_PID] = controller_pid
    if scheduler_identity is not None:
        payload[GATE_SCHEDULER_IDENTITY] = dict(scheduler_identity)
    if reviewer_liveness_epoch is not None:
        payload[GATE_REVIEWER_LIVENESS_EPOCH] = reviewer_liveness_epoch
    if reviewer_liveness_at is not None:
        payload[GATE_REVIEWER_LIVENESS_AT] = reviewer_liveness_at
    _atomic_write_json(paths.gate, payload)


def entry_gate_preflight(
    paths: PaperDayPaths | None = None,
    *,
    expected_configuration_fingerprint: str | None = None,
    processes: Any | None = None,
    authority_liveness_ttl_seconds: float = AUTHORITY_LIVENESS_TTL_SECONDS,
) -> Callable[..., str | None]:
    """The runner ``entry_preflight`` that makes the session gate real.

    Refusal semantics (the preflight runs before a verification proposal is
    filed, so refusing here prevents both proposals and orders):

    - an unknown schema, mandate or gate value -> refuse armed entries
    - MANAGE_ONLY -> refuse armed entries regardless of health
    - FULL with a supplied expected config/policy fingerprint -> refuse armed
      entries when the session recorded no fingerprint or a different one
    - gate file says CLOSED           -> refuse always
    - gate file says PROOF_ONLY       -> refuse only armed entries
    - gate file says OPEN             -> allow, *unless* the session lock is
      gone (a crashed or half-stopped session must not leave a standing armed
      licence) -- in which case armed entries refuse
    - no gate file at all             -> unarmed passes work (nothing to break
      for operators exercising the pipeline); armed entries refuse, because
      new opening risk requires PAPER_DAY_READY and only start-paper-day
      writes that
    """
    resolved = paths or PaperDayPaths.default()

    def _as_utc(value: Any) -> dt.datetime | None:
        if isinstance(value, dt.datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = dt.datetime.fromisoformat(value)
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(dt.timezone.utc)

    def _strict_authority_refusal(gate: dict[str, Any], observed_now: dt.datetime) -> str | None:
        """Validate the authority generated by a controller-managed FULL day.

        Manual ``write_gate`` fixtures remain compatible for management-only
        and proof tests.  A production controller marks its gate
        ``authority_required``; that bit is the explicit boundary at which a
        stale gate, scheduler, or reviewer epoch becomes a hard refusal.
        """
        if not gate.get(GATE_AUTHORITY_REQUIRED, False):
            return None
        if gate.get(GATE_STATE_DIR) != str(resolved.state_dir):
            return (
                "FAIL-STALE-PAPERDAY-AUTHORITY: gate StateDir does not match the "
                "active absolute StateDir"
            )
        expected_date = observed_now.astimezone(dt.timezone.utc).date().isoformat()
        if gate.get(GATE_SESSION_DATE) != expected_date:
            return (
                "FAIL-STALE-PAPERDAY-AUTHORITY: paper-day session date is not "
                "today; restart the paper day"
            )
        if gate.get(GATE_RECOVERY_REQUIRED):
            return (
                "FAIL-RECOVERY-BLOCKED: paper-day recovery is outstanding; "
                "reconcile before allowing entry"
            )
        for key, label in (
            (GATE_POLICY_SHA256, "policy"),
            (GATE_CATALOG_SHA256, "catalog"),
            (GATE_CONFIG_SHA256, "config"),
        ):
            value = gate.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                return (
                    f"FAIL-{label.upper()}-HASH: controller authority has no valid "
                    f"{label} SHA-256 digest"
                )
        configuration_fingerprint = gate.get(GATE_CONFIGURATION_FINGERPRINT)
        if not isinstance(configuration_fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", configuration_fingerprint
        ):
            return (
                "FAIL-CONFIGURATION-FINGERPRINT: controller authority has no valid "
                "configuration fingerprint"
            )

        scheduler_identity = gate.get(GATE_SCHEDULER_IDENTITY)
        if not isinstance(scheduler_identity, dict):
            return (
                "FAIL-STALE-PAPERDAY-AUTHORITY: scheduler identity is missing; "
                "the armed worker is not licensed"
            )
        session_id = scheduler_identity.get("session_id")
        nonce = scheduler_identity.get("nonce")
        pid = scheduler_identity.get("pid")
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(nonce, str)
            or not nonce
            or type(pid) is not int
            or pid <= 0
        ):
            return (
                "FAIL-STALE-PAPERDAY-AUTHORITY: scheduler identity is malformed; "
                "armed entry refuses"
            )
        try:
            from .scheduler import (
                SchedulerIdentity,
                SchedulerPaths,
                read_scheduler_record,
                ready_for,
            )

            scheduler_paths = SchedulerPaths(root=resolved.root)
            identity = SchedulerIdentity(session_id=session_id, nonce=nonce)
            record = read_scheduler_record(scheduler_paths)
            if not isinstance(record, dict):
                return "FAIL-STALE-PAPERDAY-AUTHORITY: scheduler PID record is missing"
            if record.get("session_id") != session_id or record.get("nonce") != nonce:
                return (
                    "FAIL-STALE-PAPERDAY-AUTHORITY: scheduler PID record belongs to "
                    "a different fencing identity"
                )
            if record.get("pid") != pid:
                return (
                    "FAIL-STALE-PAPERDAY-AUTHORITY: scheduler PID changed after "
                    "the gate was published"
                )
            if not ready_for(scheduler_paths, identity):
                return (
                    "FAIL-STALE-PAPERDAY-AUTHORITY: scheduler heartbeat is absent "
                    "or belongs to another session"
                )
            process_port = processes or SubprocessProcessPort()
            if not process_port.alive(pid):
                return (
                    "FAIL-STALE-PAPERDAY-AUTHORITY: scheduler PID is not alive; "
                    "entry is blocked"
                )
            if f"--scheduler-session={session_id}:{nonce}" not in process_port.cmdline(pid):
                return (
                    "FAIL-STALE-PAPERDAY-AUTHORITY: scheduler PID command identity "
                    "does not match the gate"
                )
            heartbeat = _read_json(scheduler_paths.heartbeat)
            heartbeat_at = _as_utc(heartbeat.get("at")) if heartbeat else None
            if heartbeat_at is None or (
                observed_now.astimezone(dt.timezone.utc) - heartbeat_at
            ).total_seconds() > authority_liveness_ttl_seconds:
                return (
                    "FAIL-STALE-PAPERDAY-AUTHORITY: scheduler heartbeat is stale; "
                    "restart or recover the worker"
                )
        except (ImportError, OSError, ValueError) as exc:
            return f"FAIL-STALE-PAPERDAY-AUTHORITY: scheduler authority unreadable ({exc})"

        epoch = gate.get(GATE_REVIEWER_LIVENESS_EPOCH)
        verification = _read_json(resolved.last_verification)
        if not isinstance(epoch, str) or not epoch or not verification:
            return (
                "FAIL-STALE-PAPERDAY-AUTHORITY: reviewer liveness epoch is missing"
            )
        if (
            verification.get("session_id") != gate.get("session_id")
            or verification.get(GATE_REVIEWER_LIVENESS_EPOCH) != epoch
        ):
            return (
                "FAIL-STALE-PAPERDAY-AUTHORITY: reviewer heartbeat belongs to a "
                "different session epoch"
            )
        liveness_at = _as_utc(verification.get("liveness_at"))
        if liveness_at is None or (
            observed_now.astimezone(dt.timezone.utc) - liveness_at
        ).total_seconds() > authority_liveness_ttl_seconds:
            return (
                "FAIL-STALE-PAPERDAY-AUTHORITY: reviewer liveness epoch is stale"
            )
        return None

    def preflight(*, intent: Any = None, snapshot: Any = None, market_data: Any = None,
                  policy: Any = None, now: Any = None, armed: bool = False) -> str | None:
        observed_now = _as_utc(now) or dt.datetime.now(dt.timezone.utc)
        gate = read_gate(resolved)
        if gate is None:
            if resolved.gate.exists():
                return (
                    "paper-day gate exists but is unreadable; entry consideration "
                    "refuses until start-paper-day rewrites it"
                )
            if armed:
                return (
                    "no paper-day session gate exists; armed opening entries require "
                    "PAPER_DAY_READY -- run bin\\start-paper-day.ps1"
                )
            return None
        schema = gate.get("schema_version")
        mandate = gate.get("mandate")
        entry_gate = gate.get("entry_gate")
        if schema != GATE_SCHEMA_VERSION:
            return (
                "ENTRY_REFUSED_BY_MANAGE_ONLY: paper-day gate schema is unknown "
                "or stale; run start-paper-day again"
            )
        if mandate not in _MANDATES:
            return (
                "ENTRY_REFUSED_BY_MANAGE_ONLY: paper-day mandate is unknown; "
                "opening risk is refused until the session is restarted"
            )
        authority_refusal = _strict_authority_refusal(gate, observed_now)
        if authority_refusal is not None:
            return authority_refusal
        if armed and mandate == MANDATE_MANAGE_ONLY:
            return (
                "ENTRY_REFUSED_BY_MANAGE_ONLY: this paper-day session is "
                "management-only; exits and reconciliation remain enabled"
            )
        if armed and mandate == MANDATE_FULL and expected_configuration_fingerprint is not None:
            expected = expected_configuration_fingerprint.strip()
            if not expected:
                return (
                    "ENTRY_REFUSED_BY_FINGERPRINT: the live configuration "
                    "fingerprint is missing; armed opening entries refuse"
                )
            recorded = gate.get(GATE_CONFIGURATION_FINGERPRINT)
            if not isinstance(recorded, str) or not recorded.strip():
                return (
                    "ENTRY_REFUSED_BY_FINGERPRINT: this FULL paper-day session "
                    "recorded no risk/configuration fingerprint; restart the "
                    "paper day under the current config and policy"
                )
            if recorded != expected:
                return (
                    "ENTRY_REFUSED_BY_FINGERPRINT: this FULL paper-day session "
                    "was armed under a different risk/configuration fingerprint; "
                    "restart the paper day before opening new risk"
                )
        if entry_gate not in {GATE_OPEN, GATE_PROOF_ONLY, GATE_CLOSED}:
            return "paper-day gate value is unknown; entry consideration refuses"
        entry_gate = str(entry_gate)
        state = str(gate.get("state", "?"))
        if entry_gate == GATE_CLOSED:
            return (
                f"the paper-day entry gate is CLOSED (session state {state}); "
                "no new entry proposals or orders until the next start-paper-day"
            )
        if entry_gate == GATE_OPEN:
            lock = _read_json(resolved.lock)
            if lock is None:
                return (
                    "the paper-day gate says OPEN but no session lock exists -- "
                    "treating as a crashed session; entry consideration refuses until "
                    "start-paper-day runs again"
                )
            if (
                lock.get("session_id") != gate.get("session_id")
                or not isinstance(gate.get("fencing_token"), str)
                or not gate.get("fencing_token")
                or lock.get("fencing_token") != gate.get("fencing_token")
            ):
                return (
                    "the paper-day gate identity does not match the active session "
                    "lock; refusing stale entry authority"
                )
            return None
        if armed:
            return (
                f"paper-day session state is {state}, not {READY}; armed opening "
                "entries are refused (management and exits are unaffected)"
            )
        return None

    return preflight


# ---------------------------------------------------------------------------
# the controller
# ---------------------------------------------------------------------------


@dataclass
class PaperDayController:
    """start / stop / status over one shared set of injectable ports."""

    paths: PaperDayPaths = field(default_factory=PaperDayPaths.default)
    processes: Any = field(default_factory=SubprocessProcessPort)
    engine: Any = None  # EngineCommandRunner-shaped
    tcp_probe: Callable[[str, int], bool] = default_tcp_probe
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)
    sleep: Callable[[float], None] = time.sleep
    collab_root: Path | None = None
    liveness_timeout: float = 180.0
    liveness_poll: float = 3.0
    #: Hook for the consumption proof; overridable so tests can force failure.
    consumption_proof: Callable[[], tuple[bool, str]] | None = None
    #: Overridable config source, so tests can inject a stub or a refusing
    #: config without touching the operator's real .env.
    config_loader: Callable[[], Any] | None = None
    #: The scheduler policy, or None for no scheduler at all.
    #:
    #: There is deliberately no default :class:`~engine.scheduler.SchedulerSpec`.
    #: Cadence and the command to run are policy, and a policy nobody stated is
    #: the kind of thing that gets inherited by accident and discovered in a
    #: fill. ``None`` means a paper day behaves exactly as it did before the
    #: scheduler existed.
    scheduler: Any = None  # SchedulerSpec | None
    #: Overridable so a test gets a deterministic scheduler nonce.
    nonce_factory: Callable[[], str] = lambda: uuid.uuid4().hex[:8]
    #: How long stop waits for an in-flight tick before forcing the issue.
    scheduler_drain_timeout: float = 120.0
    #: How long start waits for the scheduler's readiness handshake.
    scheduler_ready_timeout: float = 30.0
    #: New sessions are safe by default. FULL is an explicit caller choice and
    #: is never inferred from a healthy broker or an OPEN gate.
    mandate: str = MANDATE_MANAGE_ONLY
    #: Optional live config+policy fingerprint supplied by the caller that owns
    #: those objects. The controller records it but deliberately does not invent
    #: policy defaults to compute one for itself.
    configuration_fingerprint: str | None = None
    #: Hash-pinned authority inputs for controller-managed unattended sessions.
    #: Management-only callers may omit them for backwards compatibility;
    #: FULL/scheduled sessions are refused until all three are present.
    policy_sha256: str | None = None
    catalog_sha256: str | None = None
    config_sha256: str | None = None
    #: Stable reviewer epoch for this paper-day session. It is minted once and
    #: copied into the gate and liveness receipt, so yesterday's reply cannot
    #: silently license today's worker.
    reviewer_liveness_epoch: str | None = None
    _active_stop_owner: tuple[str, ...] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.mandate not in _MANDATES:
            raise ValueError(f"unknown paper-day mandate {self.mandate!r}")
        if (
            self.configuration_fingerprint is not None
            and not self.configuration_fingerprint.strip()
        ):
            raise ValueError("paper-day configuration fingerprint must be non-empty")
        self.policy_sha256 = _validate_hash(self.policy_sha256, "policy_sha256")
        self.catalog_sha256 = _validate_hash(self.catalog_sha256, "catalog_sha256")
        self.config_sha256 = _validate_hash(self.config_sha256, "config_sha256")
        if self.reviewer_liveness_epoch is not None and not self.reviewer_liveness_epoch.strip():
            raise ValueError("reviewer_liveness_epoch must be non-empty when supplied")
        if self.engine is None:
            self.engine = EngineCommandRunner(self.paths.state_dir)

    def _authority_required(self) -> bool:
        # Existing MANAGE_ONLY sessions may use the scheduler without the
        # entry-authority artifact.  FULL is the explicit boundary at which
        # hashes, live scheduler proof, and reviewer epoch become mandatory.
        return self.mandate == MANDATE_FULL

    def _authority_hashes_complete(self) -> bool:
        artifact_hashes_complete = all(
            isinstance(value, str) and bool(value)
            for value in (self.policy_sha256, self.catalog_sha256, self.config_sha256)
        )
        if not artifact_hashes_complete:
            return False
        # The three artifact hashes identify the inputs, but FULL also needs
        # the reviewed base configuration fingerprint that binds those inputs
        # to the broker/risk configuration.  Requiring it here closes the
        # direct-controller path; the CLI wrapper already requires the same
        # value, and a healthy broker or OPEN gate must not make up for its
        # absence.
        return self.mandate != MANDATE_FULL or bool(
            isinstance(self.configuration_fingerprint, str)
            and self.configuration_fingerprint.strip()
        )

    def _reviewer_epoch_for_session(self, session_id: str) -> str:
        existing_gate = read_gate(self.paths)
        if (
            existing_gate is not None
            and existing_gate.get("session_id") == session_id
            and isinstance(existing_gate.get(GATE_REVIEWER_LIVENESS_EPOCH), str)
            and existing_gate.get(GATE_REVIEWER_LIVENESS_EPOCH)
        ):
            return str(existing_gate[GATE_REVIEWER_LIVENESS_EPOCH])
        if self.reviewer_liveness_epoch:
            return self.reviewer_liveness_epoch
        self.reviewer_liveness_epoch = uuid.uuid4().hex
        return self.reviewer_liveness_epoch

    # -- collab plumbing --------------------------------------------------

    def _store(self) -> Any:
        root = self.collab_root
        if root is None:
            from .options.approval import default_collab_root

            root = default_collab_root()
        if root is None:
            raise EngineError(
                "no collab root could be found for the reviewer exchange",
                hint="set IBKR_COLLAB_ROOT or register .collab/ibkr in collabs.json",
            )
        paths = _collab_load("paths", "CollabPaths").at(root)
        return _collab_load("store", "HandoffStore")(paths)

    # ==================================================================
    # START
    # ==================================================================

    def start(self) -> StartReport:
        report = StartReport()
        now = self.clock()
        report.session_id = f"paperday-{now:%Y%m%d}-{uuid.uuid4().hex[:8]}"

        # -- 1. environment ------------------------------------------------
        report.add(
            "platform",
            True,
            f"{platform.system()} {platform.release()}, python {platform.python_version()}",
            severity="info",
        )
        if not (_engine_dir() / "src" / "engine").is_dir():
            report.add("repository", False, f"engine package not found under {_engine_dir()}")
            report.decide()
            return report
        report.add("repository", True, str(_repo_root()), severity="info")

        # -- 2. configuration (this is where live ports die) ---------------
        try:
            config = (self.config_loader or self._load_config)()
        except EngineError as exc:
            report.add("configuration", False, f"{exc}")
            report.decide()
            self._write_gate_for(report, now)
            return report
        report.add(
            "configuration",
            True,
            f"{config.venue} {config.host}:{config.port} account {config.account_id}",
        )

        if self._authority_required() and not self._authority_hashes_complete():
            report.add(
                "authority inputs",
                False,
                "FULL/scheduled paper days require policy_sha256, catalog_sha256, "
                "config_sha256, and configuration_fingerprint before any worker "
                "can be licensed",
            )
            report.decide()
            self._write_gate_for(report, now)
            return report

        # -- 3. session lock (idempotent + stale-aware) ---------------------
        lock_state = self._acquire_lock(report, now)
        if lock_state is None:
            report.decide()
            return report
        report.already_running = lock_state == "already"

        # -- 4. broker ------------------------------------------------------
        if not self.tcp_probe(config.host, config.port):
            report.add(
                "broker",
                False,
                f"nothing is listening on {config.host}:{config.port} -- is TWS/Gateway "
                "running and logged in to the paper account?",
            )
            report.decide()
            self._write_gate_for(report, now)
            return report
        status = self.engine.run(["status"])
        if status.code != 0:
            report.add("broker", False, f"engine status exited {status.code}")
            report.decide()
            self._write_gate_for(report, now)
            return report
        report.add("broker", True, f"connected read-only as {config.account_id} (paper)")

        # -- 5. builder watcher --------------------------------------------
        self._ensure_watcher(report)

        # -- 6. reviewer watcher (detection only -- it is not ours to run) --
        reviewer_pids = [
            pid for needle in _REVIEWER_NEEDLES for pid in self.processes.pids_matching(needle)
        ]
        report.add(
            "reviewer watcher",
            bool(reviewer_pids),
            f"pids {sorted(set(reviewer_pids))}" if reviewer_pids
            else "no reviewer-side watcher process found -- verifier will be unavailable",
            severity="info",
        )

        # -- 7. recover incomplete handoffs --------------------------------
        self._recover_handoffs(report, now)

        # -- 8. leftover approvals and expired proposals -------------------
        self._audit_ledger(report, now)

        # -- 9. reconcile ---------------------------------------------------
        recon = self.engine.run(["options-positions"])
        if recon.code != 0:
            report.add("reconciliation", False, f"options-positions exited {recon.code}")
        elif "broker agrees" in recon.stdout:
            report.add("reconciliation", True, "broker agrees")
        else:
            report.add(
                "reconciliation",
                False,
                "reconciliation ran but the broker does not agree -- entries stay shut, "
                "management continues",
                severity="degrading",
            )

        # -- 10. engine-native marking -------------------------------------
        mark = self.engine.run(["options-mark"])
        if mark.code != 0:
            report.add("marking", False, f"options-mark exited {mark.code}", severity="degrading")
        elif re.search(r"MARKED|COMMISSION_INCOMPLETE", mark.stdout):
            report.add("marking", True, "positions marked from live leg quotes")
        elif "open positions   0" in mark.stdout:
            report.add("marking", True, "no open positions to mark", severity="info")
        else:
            report.add(
                "marking",
                False,
                "marking refused (one-sided or stale book -- normal outside market hours); "
                "profit rule cannot fire until it succeeds",
                severity="degrading",
            )

        # -- 11. the real liveness round-trip ------------------------------
        if any(c.name == "reviewer watcher" and c.ok for c in report.checks):
            self._liveness_roundtrip(report, now)
        else:
            report.add(
                "verifier liveness",
                False,
                "skipped -- no reviewer watcher to answer",
                severity="info",
            )

        # -- 12. approval-consumption mechanics proof ----------------------
        proof = self.consumption_proof or _consumption_mechanics_proof
        try:
            proof_ok, proof_detail = proof()
        except Exception as exc:  # noqa: BLE001 - a crashed proof is a failed proof
            proof_ok, proof_detail = False, f"proof crashed: {type(exc).__name__}: {exc}"
        report.add("consumption proof", proof_ok, proof_detail, severity="degrading")
        if proof_ok:
            payload = _read_json(self.paths.last_verification) or {}
            payload["mechanics_proof_at"] = now.isoformat()
            _write_json(self.paths.last_verification, payload)

        # -- 13. decide, write the gate ------------------------------------
        report.decide()
        self._write_gate_for(report, now)

        # -- 14. only now may a scheduler exist ----------------------------
        #
        # Deliberately last, and after the gate write at step 13. The scheduler
        # runs ``options-run --arm``; its very first tick reads whatever
        # ``gate.json`` says at that instant. Started any earlier it would be
        # reading the *previous* session's gate -- which may say OPEN -- while
        # this session is still reconciling, marking and proving the verifier.
        # An armed child acting on a stale opening licence is precisely the
        # stale-gate race, and starting it beside the builder watcher for
        # symmetry reintroduced it.
        #
        # A scheduler that fails to start still only degrades the day, so this
        # cannot turn a healthy book into a refused one -- but the state was
        # already decided above, so a degrading failure here is recorded and
        # re-decided rather than silently ignored.
        if self._ensure_scheduler(report):
            report.decide()
            self._write_gate_for(report, now)
        return report

    # -- start helpers -----------------------------------------------------

    def _load_config(self) -> Any:
        from .config import EngineConfig

        _collabkit.load_dotenv()
        return EngineConfig.from_env(state_dir=self.paths.state_dir)

    def _write_gate_for(self, report: StartReport, now: dt.datetime) -> None:
        entry_gate = {
            READY: GATE_OPEN,
            DEGRADED: GATE_PROOF_ONLY,
        }.get(report.state, GATE_CLOSED)
        verification = _read_json(self.paths.last_verification) or {}
        write_gate(
            self.paths,
            entry_gate=entry_gate,
            state=report.state,
            session_id=report.session_id,
            now=now,
            fencing_token=self._current_fencing_token(),
            mandate=self._current_mandate(),
            configuration_fingerprint=self._current_configuration_fingerprint(),
            policy_sha256=self.policy_sha256,
            catalog_sha256=self.catalog_sha256,
            config_sha256=self.config_sha256,
            authority_required=self._authority_required(),
            recovery_required=self._recovery_required_on_disk(),
            controller_pid=os.getpid(),
            scheduler_identity=self._scheduler_identity_payload(report.session_id),
            reviewer_liveness_epoch=self._reviewer_epoch_for_session(report.session_id),
            reviewer_liveness_at=(
                str(verification.get("liveness_at"))
                if verification.get("session_id") == report.session_id
                else None
            ),
        )

    def _recovery_required_on_disk(self) -> bool:
        gate = read_gate(self.paths) or {}
        if gate.get(GATE_RECOVERY_REQUIRED) is True:
            return True
        try:
            from .scheduler import SchedulerPaths, find_unmatched_ticks

            return bool(find_unmatched_ticks(SchedulerPaths(root=self.paths.root)))
        except (OSError, ValueError):
            return True

    def _scheduler_identity_payload(self, session_id: str) -> dict[str, Any] | None:
        if self.scheduler is None:
            return None
        try:
            from .scheduler import SchedulerPaths, read_scheduler_record

            record = read_scheduler_record(SchedulerPaths(root=self.paths.root))
        except (ImportError, OSError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        recorded_session_id = record.get("session_id")
        nonce = record.get("nonce")
        pid = record.get("pid")
        if (
            not isinstance(recorded_session_id, str)
            or recorded_session_id != session_id
            or not isinstance(nonce, str)
            or not nonce
            or type(pid) is not int
            or pid <= 0
        ):
            return None
        return {"session_id": recorded_session_id, "nonce": nonce, "pid": pid}

    def _close_stale_authority(
        self, gate: dict[str, Any] | None, *, now: dt.datetime, reason: str
    ) -> None:
        """Publish a fail-closed marker before recovering a stale session.

        The old OPEN file is evidence of a crashed authority, not permission
        to carry it forward.  This write is deliberately atomic and retains
        the old identity so an operator can reconcile exactly what was fenced.
        """
        prior = dict(gate or {})
        prior.update(
            {
                "schema_version": GATE_SCHEMA_VERSION,
                "entry_gate": GATE_CLOSED,
                "state": BLOCKED,
                GATE_STATE_DIR: str(self.paths.state_dir),
                GATE_SESSION_DATE: now.astimezone(dt.timezone.utc).date().isoformat(),
                GATE_RECOVERY_REQUIRED: True,
                "recovery_reason": reason,
                "as_of": now.isoformat(),
            }
        )
        _atomic_write_json(self.paths.gate, prior)

    def _current_mandate(self) -> str:
        existing = read_gate(self.paths)
        if (
            existing is not None
            and existing.get("session_id") == self._session_id_from_lock()
            and existing.get("mandate") in _MANDATES
        ):
            return str(existing["mandate"])
        return self.mandate

    def _current_configuration_fingerprint(self) -> str | None:
        existing = read_gate(self.paths)
        session_id = self._session_id_from_lock()
        existing_fingerprint = (
            existing.get(GATE_CONFIGURATION_FINGERPRINT) if existing is not None else None
        )
        if (
            existing is not None
            and existing.get("session_id") == session_id
            and isinstance(existing_fingerprint, str)
            and existing_fingerprint.strip()
        ):
            return existing_fingerprint
        if (
            self.mandate == MANDATE_FULL
            and self.configuration_fingerprint
            and self.policy_sha256
            and self.catalog_sha256
            and self.config_sha256
        ):
            return effective_configuration_fingerprint(
                self.configuration_fingerprint,
                policy_sha256=self.policy_sha256,
                catalog_sha256=self.catalog_sha256,
                config_sha256=self.config_sha256,
            )
        return self.configuration_fingerprint

    def _session_id_from_lock(self) -> str | None:
        lock = _read_json(self.paths.lock)
        value = lock.get("session_id") if lock else None
        return value if isinstance(value, str) else None

    def _current_fencing_token(self) -> str | None:
        lock = _read_json(self.paths.lock)
        token = lock.get("fencing_token") if lock else None
        return token if isinstance(token, str) and token else None

    @staticmethod
    def _watcher_record_matches_lock(
        record: dict[str, Any], lock: dict[str, Any]
    ) -> bool:
        session_id = lock.get("session_id")
        fencing_token = lock.get("fencing_token")
        return (
            isinstance(session_id, str)
            and bool(session_id)
            and isinstance(fencing_token, str)
            and bool(fencing_token)
            and record.get("session_id") == session_id
            and record.get("fencing_token") == fencing_token
        )

    def _acquire_lock(self, report: StartReport, now: dt.datetime) -> str | None:
        """Returns "fresh", "already", or None (blocked)."""
        self.paths.root.mkdir(parents=True, exist_ok=True)
        existing = _read_json(self.paths.lock)
        existing_gate = read_gate(self.paths)
        if existing is not None:
            recorded = _read_json(self.paths.watcher_pid) or {}
            pid = recorded.get("pid")
            watcher_alive = (
                isinstance(pid, int)
                and self.processes.alive(pid)
                and _WATCHER_NEEDLE in self.processes.cmdline(pid)
                and self._watcher_record_matches_lock(recorded, existing)
                and existing.get(GATE_SESSION_DATE)
                == now.astimezone(dt.timezone.utc).date().isoformat()
                and isinstance(existing_gate, dict)
                and existing_gate.get("session_id") == existing.get("session_id")
                and existing_gate.get("fencing_token") == existing.get("fencing_token")
            )
            if watcher_alive:
                if (
                    existing_gate is not None
                    and existing_gate.get("session_id") != existing.get("session_id")
                ):
                    report.add(
                        "session authority",
                        False,
                        "live lock and gate identify different sessions; stale OPEN "
                        "authority was not adopted",
                    )
                    return None
                report.session_id = str(existing.get("session_id", report.session_id))
                self.reviewer_liveness_epoch = self._reviewer_epoch_for_session(
                    report.session_id
                )
                report.add(
                    "session lock",
                    True,
                    f"session {report.session_id} already running (watcher pid {pid}); "
                    "re-verifying idempotently",
                )
                return "already"
            report.add(
                "session lock",
                True,
                f"stale lock from {existing.get('started_at', '?')} (watcher dead) -- "
                "recovered",
                severity="info",
            )
            if existing_gate is not None and existing_gate.get("entry_gate") != GATE_CLOSED:
                self._close_stale_authority(
                    existing_gate,
                    now=now,
                    reason="previous session lock was stale or its watcher was dead",
                )
            with contextlib.suppress(OSError):
                self.paths.lock.unlink()
        elif existing_gate is not None and existing_gate.get("entry_gate") != GATE_CLOSED:
            self._close_stale_authority(
                existing_gate,
                now=now,
                reason="gate was OPEN/PROOF_ONLY without a current session lock",
            )
        self._reviewer_epoch_for_session(report.session_id)
        lock_payload = {
            "session_id": report.session_id,
            "started_at": now.isoformat(),
            "controller_pid": os.getpid(),
            "fencing_token": uuid.uuid4().hex,
            GATE_STATE_DIR: str(self.paths.state_dir),
            GATE_SESSION_DATE: now.astimezone(dt.timezone.utc).date().isoformat(),
        }
        if self.configuration_fingerprint is not None:
            lock_payload[GATE_CONFIGURATION_FINGERPRINT] = self.configuration_fingerprint
        if self.policy_sha256 is not None:
            lock_payload[GATE_POLICY_SHA256] = self.policy_sha256
        if self.catalog_sha256 is not None:
            lock_payload[GATE_CATALOG_SHA256] = self.catalog_sha256
        if self.config_sha256 is not None:
            lock_payload[GATE_CONFIG_SHA256] = self.config_sha256
        payload = json.dumps(lock_payload, indent=2)
        if not _acquire_lock_atomically(self.paths.lock, payload):
            report.add(
                "session lock",
                False,
                "another start acquired the lock concurrently; re-run to verify",
            )
            return None
        report.add("session lock", True, f"acquired for {report.session_id}")
        return "fresh"

    def _ensure_watcher(self, report: StartReport) -> None:
        recorded = _read_json(self.paths.watcher_pid) or {}
        lock = _read_json(self.paths.lock) or {}
        pid = recorded.get("pid")
        if isinstance(pid, int) and self.processes.alive(pid):
            if (
                _WATCHER_NEEDLE in self.processes.cmdline(pid)
                and self._watcher_record_matches_lock(recorded, lock)
            ):
                report.watcher_pid = pid
                report.add("builder watcher", True, f"already running, pid {pid}")
                return
            report.add(
                "builder watcher",
                True,
                f"pid {pid} was reused by another process -- stale record discarded",
                severity="info",
            )
        elif pid is not None:
            report.add(
                "builder watcher", True, f"stale pid {pid} (dead) -- record discarded",
                severity="info",
            )
        collab_root = self.collab_root or (_repo_root() / ".collab" / "ibkr")
        try:
            new_pid = self.processes.spawn_detached(
                [sys.executable, str(_repo_root() / "tools" / _WATCHER_NEEDLE)],
                env={**os.environ, "HANDOFF_ROOT": str(collab_root)},
                cwd=_repo_root(),
                log=self.paths.watcher_log,
            )
        except Exception as exc:  # noqa: BLE001 - spawn failure degrades the day
            report.add(
                "builder watcher",
                False,
                f"could not start: {type(exc).__name__}: {exc}",
                severity="degrading",
            )
            return
        self.sleep(1.0)
        if not self.processes.alive(new_pid):
            report.add(
                "builder watcher",
                False,
                f"pid {new_pid} exited immediately -- see {self.paths.watcher_log}",
                severity="degrading",
            )
            return
        _write_json(
            self.paths.watcher_pid,
            {
                "pid": new_pid,
                "started_at": self.clock().isoformat(),
                "needle": _WATCHER_NEEDLE,
                "session_id": lock.get("session_id"),
                "fencing_token": lock.get("fencing_token"),
            },
        )
        report.watcher_pid = new_pid
        report.add("builder watcher", True, f"started, pid {new_pid}")

    def _scheduler_identity(self, session_id: str) -> Any:
        """This session's scheduler identity, reusing the nonce if one exists.

        Minting a fresh nonce on every start would break idempotent restart: the
        running child carries the *old* nonce, adoption compares the new one,
        the match fails, and a perfectly healthy scheduler is reported as a
        stranger while the day degrades. The nonce identifies the scheduler for
        the life of the session, not the life of a ``start`` call.
        """
        from .scheduler import SchedulerIdentity, SchedulerPaths, read_scheduler_record

        record = read_scheduler_record(SchedulerPaths(root=self.paths.root)) or {}
        if record.get("session_id") == session_id:
            nonce = record.get("nonce")
            if isinstance(nonce, str) and nonce.strip():
                return SchedulerIdentity(session_id=session_id, nonce=nonce)
        return SchedulerIdentity(session_id=session_id, nonce=self.nonce_factory())

    def _ensure_scheduler(self, report: StartReport) -> bool:
        """Adopt or start this session's scheduler, if the day was given one.

        Returns whether a check was added, so the caller can re-decide the
        session state -- this runs after ``decide()`` on purpose (see step 14).

        Absent policy, this is a no-op that adds no check at all -- a paper day
        without a :class:`SchedulerSpec` is exactly the day that existed before
        this module. A scheduler that will not start is *degrading*: the book
        stays trustworthy and every manual command still works, so refusing the
        whole day over it would be the wrong trade.
        """
        if self.scheduler is None:
            if self.mandate == MANDATE_FULL:
                report.add(
                    "scheduler",
                    False,
                    "FULL paper-day authority requires a live scheduler policy",
                    severity="blocking",
                )
                return True
            return False

        from .scheduler import SchedulerPaths, adopt_or_spawn

        identity = self._scheduler_identity(report.session_id)
        pid, detail = adopt_or_spawn(
            processes=self.processes,
            paths=SchedulerPaths(root=self.paths.root),
            identity=identity,
            spec=self.scheduler,
            cwd=_engine_dir(),
            env={**os.environ, "IBKR_STATE_DIR": str(self.paths.state_dir)},
            clock=self.clock,
            sleep=self.sleep,
            python=sys.executable,
            monotonic=time.monotonic,
            ready_timeout=self.scheduler_ready_timeout,
        )
        report.add(
            "scheduler",
            pid is not None,
            detail,
            severity="blocking" if self.mandate == MANDATE_FULL else "degrading",
        )
        return True

    def _stop_lock_identity(self, lock: dict[str, Any] | None) -> tuple[str, ...]:
        """Return the session/fencing identity represented by a lock snapshot."""
        if lock is None:
            return ("no-session",)
        session_id = str(lock.get("session_id", ""))
        token = lock.get("fencing_token")
        if not isinstance(token, str) or not token:
            # Keep recovery of legacy locks possible while still distinguishing
            # their session metadata from a replacement lock.
            token = "legacy:" + json.dumps(
                {
                    "session_id": session_id,
                    "started_at": lock.get("started_at"),
                    "controller_pid": lock.get("controller_pid"),
                },
                sort_keys=True,
            )
        return ("session", session_id, token)

    def _current_stop_lock_identity(self) -> tuple[str, ...]:
        if not self.paths.lock.exists():
            return ("no-session",)
        lock = _read_json(self.paths.lock)
        return ("invalid-lock",) if lock is None else self._stop_lock_identity(lock)

    def _stop_owns(self, expected: tuple[str, ...]) -> bool:
        return self._current_stop_lock_identity() == expected

    def _taken_over_by_another_session(self, expected: tuple[str, ...]) -> bool:
        """Whether a DIFFERENT, readable session demonstrably holds the lock now.

        Deliberately narrower than ``not _stop_owns(...)``, and the difference is
        a safety property rather than a nicety. Two situations are not the same:

        * **We cannot prove who owns this** -- the lock is absent, truncated or
          unparseable. Ownership is unknown.
        * **Somebody else provably owns this** -- a readable lock naming another
          session.

        Only the second is a takeover. Conflating them made a *corrupt* lock
        abandon ``stop`` before it wrote the gate, so a crash mid-lock-write left
        the entry gate in whatever position the day held -- possibly OPEN. That
        is a fail-*open*, and the gate-closes-first ordering exists precisely so
        no downstream failure can leave a standing opening licence.

        Closing the gate is risk-reducing: it can only refuse new entries, and
        never traps a position. So it proceeds whenever ownership is merely
        unknown. The risk-bearing steps -- draining, cancelling, unlinking --
        still demand positive proof via :meth:`_stop_owns`.
        """
        current = self._current_stop_lock_identity()
        if not current or current[0] != "session":
            return False
        return current != expected

    def _require_stop_ownership(
        self, report: StopReport, expected: tuple[str, ...], phase: str
    ) -> bool:
        if self._stop_owns(expected):
            return True
        report.add(
            "session ownership",
            False,
            f"stop abandoned before {phase}; lock/fencing identity changed, "
            "so replacement session state was left untouched",
        )
        return False

    def _publish_last_shutdown(
        self,
        report: StopReport,
        expected: tuple[str, ...],
        payload: dict[str, Any],
    ) -> bool:
        """Publish shutdown proof only while the released lease is still free.

        Releasing the old lock is not the end of the ownership protocol.  A
        replacement can acquire it before the old controller writes its
        shutdown marker.  The marker is therefore a compare-and-swap boundary:
        require the lock to be absent, publish atomically, then check again and
        remove only our marker if a replacement won the race.
        """
        del expected  # the post-release state is intentionally ``no-session``
        free = ("no-session",)
        if self._current_stop_lock_identity() != free:
            report.add(
                "session ownership",
                False,
                "replacement session acquired the lock before last shutdown; "
                "old shutdown proof was not published",
            )
            return False

        _atomic_write_json(self.paths.last_shutdown, payload)
        if self._current_stop_lock_identity() != free:
            if _read_json(self.paths.last_shutdown) == payload:
                with contextlib.suppress(OSError):
                    self.paths.last_shutdown.unlink()
            report.add(
                "session ownership",
                False,
                "replacement session acquired the lock during last-shutdown "
                "publication; old shutdown proof was withdrawn",
            )
            return False
        return True

    def _stop_scheduler(
        self, report: StopReport, now: dt.datetime, expected: tuple[str, ...]
    ) -> None:
        """Quiesce the scheduler and wait for its tick, before anything cancels.

        The identity comes from the scheduler's own record rather than from this
        controller, because stop must be able to reach a scheduler started by a
        *previous* invocation. A record that cannot be identified terminates
        nothing: an unidentifiable process is a stranger.
        """
        from .scheduler import (
            SchedulerPaths,
            drain_and_stop,
            identity_from_record,
            read_terminal_receipt,
            read_scheduler_record,
            request_quiesce,
        )

        if not self._require_stop_ownership(report, expected, "scheduler drain"):
            return
        paths = SchedulerPaths(root=self.paths.root)
        record = read_scheduler_record(paths)
        if record is None:
            gate = read_gate(self.paths) or {}
            expected_scheduler = gate.get(GATE_SCHEDULER_IDENTITY)
            scheduler_expected = bool(
                self.scheduler is not None
                or gate.get(GATE_AUTHORITY_REQUIRED)
                or isinstance(expected_scheduler, dict)
            )
            if not scheduler_expected:
                return
            # A missing/dead scheduler is clean only when its own durable
            # terminal receipt proves the exact session/nonce exited cleanly.
            # Absence of a PID is not evidence that no broker work happened.
            scheduler_identity = None
            if isinstance(expected_scheduler, dict):
                session_id = expected_scheduler.get("session_id")
                nonce = expected_scheduler.get("nonce")
                if isinstance(session_id, str) and isinstance(nonce, str):
                    from .scheduler import SchedulerIdentity

                    scheduler_identity = SchedulerIdentity(
                        session_id=session_id, nonce=nonce
                    )
            if scheduler_identity is not None:
                terminal = read_terminal_receipt(paths)
                if (
                    terminal is not None
                    and terminal.get("session_id") == scheduler_identity.session_id
                    and terminal.get("nonce") == scheduler_identity.nonce
                    and terminal.get("clean_exit") is True
                ):
                    report.add(
                        "scheduler",
                        True,
                        "scheduler PID is absent but the matching durable clean-exit "
                        "receipt proves its final tick",
                    )
                    return
            if paths.pid.exists():
                if scheduler_identity is not None:
                    request_quiesce(
                        paths,
                        reason="paper-day stop",
                        now=now,
                        identity=scheduler_identity,
                    )
            report.add(
                "scheduler",
                False,
                "STOP_DIRTY: scheduler PID is missing or unreadable without a "
                "matching durable clean-exit receipt; reconcile broker effects",
            )
            return

        if not self._require_stop_ownership(report, expected, "scheduler quiesce"):
            return

        identity = identity_from_record(record)
        if identity is None:
            # Quiesce must precede identity interpretation. A malformed record
            # is exactly the case where the supervisor cannot name the child,
            # but a durable stop request still gives a live child a chance to
            # halt at its next boundary. No termination or cleanup is attempted
            # because ownership remains unproven.
            request_quiesce(paths, reason="paper-day stop", now=now)
            report.add(
                "scheduler",
                False,
                f"scheduler record at {paths.pid} names no session/nonce -- "
                "quiesce requested, nothing terminated; a live tick will stop at "
                "its next boundary, but its shutdown is unproven. Reconcile, and "
                "remove the record by hand once you know what it was",
            )
            return

        request_quiesce(
            paths,
            reason="paper-day stop",
            now=now,
            identity=identity,
        )

        gate_identity = (read_gate(self.paths) or {}).get(GATE_SCHEDULER_IDENTITY)
        if isinstance(gate_identity, dict) and (
            gate_identity.get("session_id") != identity.session_id
            or gate_identity.get("nonce") != identity.nonce
            or gate_identity.get("pid") != record.get("pid")
        ):
            report.add(
                "scheduler",
                False,
                "STOP_DIRTY: scheduler record does not match the gate's fencing "
                "identity; no process was terminated",
            )
            return

        if not self._require_stop_ownership(report, expected, "scheduler termination"):
            return
        clean, detail = drain_and_stop(
            processes=self.processes,
            paths=paths,
            identity=identity,
            now=now,
            drain_timeout=self.scheduler_drain_timeout,
            sleep=self.sleep,
            monotonic=time.monotonic,
        )
        report.add("scheduler", clean, detail)

    def _recover_handoffs(self, report: StartReport, now: dt.datetime) -> None:
        try:
            store = self._store()
        except EngineError as exc:
            report.add("handoff recovery", False, str(exc), severity="degrading")
            return
        recovered = 0
        for handoff in store.list(("pending", "claimed"), to="builder"):
            with contextlib.suppress(Exception):
                if handoff.status == "pending":
                    store.claim(handoff.id, by="builder")
                store.complete(
                    handoff.id,
                    note=f"recovered unacknowledged at paper-day start {now.isoformat()}",
                    by="builder",
                )
                recovered += 1
        expired = 0
        for handoff in store.list(("pending", "claimed"), sender="builder", tag="verification"):
            with contextlib.suppress(Exception):
                if handoff.status == "pending":
                    store.claim(handoff.id, by="builder")
                store.complete(
                    handoff.id,
                    note=f"EXPIRED unanswered at paper-day start {now.isoformat()}; "
                    "a fresh packet is required for any new opening",
                    by="builder",
                )
                expired += 1
        report.add(
            "handoff recovery",
            True,
            f"{recovered} inbound acknowledged, {expired} stale proposal(s) expired",
            severity="info",
        )

    def _audit_ledger(self, report: StartReport, now: dt.datetime) -> None:
        requests = self.paths.verification_ledger / "requests"
        consumed = self.paths.verification_ledger / "consumed"
        live: list[str] = []
        expired_proposals = 0
        for record_path in sorted(requests.glob("*.json")) if requests.is_dir() else []:
            record = _read_json(record_path) or {}
            raw_expiry = str(record.get("expires_at", ""))
            try:
                expires = dt.datetime.fromisoformat(raw_expiry)
            except ValueError:
                continue
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=dt.timezone.utc)
            if expires < now and record.get("state") == "PROPOSED":
                record["state"] = "EXPIRED"
                record["expired_at_start"] = now.isoformat()
                _write_json(record_path, record)
                expired_proposals += 1
            elif expires >= now:
                live.append(str(record.get("spec_digest", record_path.stem))[:12])
        markers = len(list(consumed.glob("*.used"))) if consumed.is_dir() else 0
        detail = (
            f"{expired_proposals} expired proposal(s) marked, {markers} consumed marker(s), "
            + (f"UNEXPIRED specs on file: {', '.join(live)} -- each still requires its "
               "exact digest to match before it authorizes anything" if live
               else "no unexpired approvals carried over")
        )
        report.add("approval ledger", True, detail, severity="info")

    def _liveness_roundtrip(self, report: StartReport, now: dt.datetime) -> None:
        try:
            store = self._store()
        except EngineError as exc:
            report.add("verifier liveness", False, str(exc), severity="degrading")
            return
        token = f"PAPERDAY-ACK-{now:%Y%m%d}-{uuid.uuid4().hex[:6]}"
        body = "\n".join(
            [
                "Daily paper-day liveness handshake. Nothing is authorized by a reply.",
                "",
                "Please reply with:",
                "  1. your model identity,",
                "  2. the current UTC timestamp as you observe it,",
                f"  3. the literal token: {token}",
                "  4. REVIEWER_READY if you will review real opening packets today.",
            ]
        )
        request = store.create(
            to="reviewer",
            sender="builder",
            title=f"HANDSHAKE: paper-day liveness {now:%Y-%m-%d}",
            body=body,
            priority="normal",
            tags=["handshake", "verifier", "paperday"],
        )
        lock = _read_json(self.paths.lock) or {}
        session_id = str(lock.get("session_id", ""))
        liveness_epoch = self._reviewer_epoch_for_session(session_id)
        deadline = time.monotonic() + self.liveness_timeout
        while time.monotonic() < deadline:
            for handoff in store.list(("pending", "claimed"), to="builder"):
                threaded = handoff.thread in (request.thread, request.id)
                if not threaded:
                    continue
                text = str(handoff.body or "")
                if token in text and "REVIEWER_READY" in text:
                    with contextlib.suppress(Exception):
                        if handoff.status == "pending":
                            store.claim(handoff.id, by="builder")
                        store.complete(
                            handoff.id,
                            note=f"liveness verified at {self.clock().isoformat()}",
                            by="builder",
                        )
                    payload = _read_json(self.paths.last_verification) or {}
                    payload["liveness_at"] = self.clock().isoformat()
                    payload["liveness_reply"] = handoff.id
                    payload["session_id"] = session_id
                    payload[GATE_REVIEWER_LIVENESS_EPOCH] = liveness_epoch
                    _write_json(self.paths.last_verification, payload)
                    report.add(
                        "verifier liveness",
                        True,
                        f"REVIEWER_READY with token echoed ({handoff.id})",
                    )
                    return
                report.add(
                    "verifier liveness",
                    False,
                    f"reply {handoff.id} lacks the token or REVIEWER_READY",
                    severity="degrading",
                )
                return
            self.sleep(self.liveness_poll)
        report.add(
            "verifier liveness",
            False,
            f"no reviewer reply within {int(self.liveness_timeout)}s "
            f"(request {request.id} left for later pickup)",
            severity="degrading",
        )

    # ==================================================================
    # STOP
    # ==================================================================

    def stop(self) -> StopReport:
        report = StopReport()
        now = self.clock()
        lock = _read_json(self.paths.lock)
        expected_owner = self._stop_lock_identity(lock)
        self._active_stop_owner = expected_owner
        expected_watcher = _read_json(self.paths.watcher_pid)
        session_id = str((lock or {}).get("session_id", "unknown-session"))
        prior_gate = read_gate(self.paths) or {}

        # -- 1. gate first: no new proposals from this instant --------------
        #
        # Guarded by takeover, NOT by strict ownership. Closing the gate is the
        # one risk-reducing act in this whole sequence, so it is withheld only
        # when another readable session provably owns the lock -- never merely
        # because we could not read it. See _taken_over_by_another_session.
        if self._taken_over_by_another_session(expected_owner):
            report.add(
                "session ownership",
                False,
                "stop abandoned before entry gate; another session now holds the "
                "lock, so its gate and state were left untouched",
            )
            return report
        write_gate(
            self.paths,
            entry_gate=GATE_CLOSED,
            state=STOPPED,
            session_id=session_id,
            now=now,
            fencing_token=(lock or {}).get("fencing_token") if lock else None,
            mandate=(
                prior_gate.get("mandate")
                if prior_gate.get("mandate") in _MANDATES
                else self.mandate
            ),
            configuration_fingerprint=prior_gate.get(GATE_CONFIGURATION_FINGERPRINT),
            policy_sha256=prior_gate.get(GATE_POLICY_SHA256),
            catalog_sha256=prior_gate.get(GATE_CATALOG_SHA256),
            config_sha256=prior_gate.get(GATE_CONFIG_SHA256),
            authority_required=bool(prior_gate.get(GATE_AUTHORITY_REQUIRED, False)),
            recovery_required=bool(prior_gate.get(GATE_RECOVERY_REQUIRED, False)),
            controller_pid=prior_gate.get(GATE_CONTROLLER_PID),
            scheduler_identity=prior_gate.get(GATE_SCHEDULER_IDENTITY),
            reviewer_liveness_epoch=prior_gate.get(GATE_REVIEWER_LIVENESS_EPOCH),
            reviewer_liveness_at=prior_gate.get(GATE_REVIEWER_LIVENESS_AT),
        )
        report.add("entry gate", True, "CLOSED before anything else")
        if not self._stop_owns(expected_owner):
            current_gate = read_gate(self.paths) or {}
            if (
                not self.paths.lock.exists()
                and
                current_gate.get("session_id") == session_id
                and current_gate.get("fencing_token")
                == (lock or {}).get("fencing_token")
            ):
                with contextlib.suppress(OSError):
                    self.paths.gate.unlink()
            report.add(
                "session ownership",
                False,
                "replacement session acquired the lock during gate publication; "
                "old stop did not proceed",
            )
            return report
        if lock is None:
            report.add("session lock", True, "no active session -- verifying stopped state")
            from .scheduler import SchedulerPaths

            scheduler_paths = SchedulerPaths(root=self.paths.root)
            scheduler_evidence = bool(
                self.paths.lock.exists()
                or scheduler_paths.pid.exists()
                or self.paths.watcher_pid.exists()
                or self.scheduler is not None
                or prior_gate.get(GATE_AUTHORITY_REQUIRED)
                or isinstance(prior_gate.get(GATE_SCHEDULER_IDENTITY), dict)
                or prior_gate.get("entry_gate") == GATE_OPEN
                or prior_gate.get("state") not in (None, STOPPED)
            )
            if scheduler_evidence:
                # A session lock that disappeared does not grant permission to
                # cancel orders or unlink state. We can still ask a surviving
                # scheduler to quiesce and record whether its exit is proven;
                # all destructive teardown remains refused without ownership.
                self._stop_scheduler(report, now, expected_owner)
                report.add(
                    "session ownership",
                    False,
                    "no owned session lock; destructive order cancellation and teardown "
                    "were refused",
                )
            return report

        # -- 1b. drain the scheduler, before anything transmits --------------
        #
        # Deliberately here and not beside the builder watcher at step 7. Step 2
        # cancels working entries with --arm and the session lock is not
        # released until step 8, so a scheduler still ticking through those
        # steps could have a pass in flight while stop is cancelling. Closing
        # the gate first bounds what a live tick may still *open*; it does not
        # stop one that already passed the gate.
        self._stop_scheduler(report, now, expected_owner)
        if not self._require_stop_ownership(report, expected_owner, "working-order cancel"):
            return report

        # -- 2. working entry orders ----------------------------------------
        self._cancel_working_entries(report, expected_owner)
        if not self._require_stop_ownership(report, expected_owner, "handoff settlement"):
            return report

        # -- 3. outstanding handoffs ----------------------------------------
        pending_reviews = self._settle_handoffs(report, now)

        # -- 4. reconcile and mark ------------------------------------------
        if not self._require_stop_ownership(report, expected_owner, "final reconcile"):
            return report
        recon = self.engine.run(["options-positions"])
        report.add(
            "final reconcile",
            recon.code == 0 and "broker agrees" in recon.stdout,
            "broker agrees" if "broker agrees" in recon.stdout
            else f"exit {recon.code} -- resolve before the next session",
        )
        if not self._require_stop_ownership(report, expected_owner, "final mark"):
            return report
        mark = self.engine.run(["options-mark"])
        marked = mark.code == 0 and bool(re.search(r"MARKED|COMMISSION_INCOMPLETE", mark.stdout))
        report.add(
            "final mark",
            True,
            "positions marked" if marked
            else "marking refused (normal outside market hours) -- last good mark stands",
        )

        # -- 5. session summary ---------------------------------------------
        if not self._require_stop_ownership(report, expected_owner, "session summary"):
            return report
        summary_path = self._write_summary(report, now, session_id, pending_reviews)
        report.add("session summary", True, str(summary_path))

        # -- 6. ask the reviewer to stop ------------------------------------
        if not self._require_stop_ownership(report, expected_owner, "reviewer shutdown"):
            return report
        self._reviewer_shutdown(report, now)

        # -- 7. builder watcher ---------------------------------------------
        self._stop_watcher(report, expected_owner, expected_watcher)

        # Publish unresolved scheduler recovery while this controller still
        # owns the lock; after release the CAS quite correctly rejects writes.
        if any(step.name == "scheduler" and not step.ok for step in report.steps):
            self._mark_recovery_required(
                report,
                expected_owner,
                reason="scheduler shutdown was not proven clean",
            )

        # -- 8. clear only what is ours and valid ---------------------------
        if not self._require_stop_ownership(report, expected_owner, "session release"):
            return report
        released = expected_owner == ("no-session",)
        current_lock = _read_json(self.paths.lock)
        if current_lock is not None:
            if self._stop_lock_identity(current_lock) != expected_owner:
                report.add(
                    "session lock",
                    False,
                    "lock identity changed before release -- replacement lock retained",
                )
                return report
            try:
                self.paths.lock.unlink()
            except OSError as exc:
                report.add("session lock", False, f"could not release: {exc}")
                return report
            released = True
            report.add("session lock", True, "released")
        if released:
            self._publish_last_shutdown(
                report,
                expected_owner,
                {
                    "at": now.isoformat(),
                    "clean": report.clean,
                    "session_id": session_id,
                    "fencing_token": (lock or {}).get("fencing_token") if lock else None,
                },
            )
        return report

    def _mark_recovery_required(
        self, report: StopReport, expected: tuple[str, ...], *, reason: str
    ) -> None:
        """Mark unresolved shutdown without overwriting a replacement session."""
        if not self._require_stop_ownership(report, expected, "recovery marker"):
            return
        gate = read_gate(self.paths)
        if gate is None:
            return
        gate[GATE_RECOVERY_REQUIRED] = True
        gate["recovery_reason"] = reason
        gate["as_of"] = self.clock().isoformat()
        _atomic_write_json(self.paths.gate, gate)
        if not self._stop_owns(expected):
            current = read_gate(self.paths) or {}
            if (
                expected[0] == "session"
                and current.get("session_id") == expected[1]
                and current.get("fencing_token") == expected[2]
            ):
                with contextlib.suppress(OSError):
                    self.paths.gate.unlink()
            report.add(
                "session ownership",
                False,
                "replacement session acquired the lock during recovery-marker publication",
            )

    def _cancel_working_entries(
        self, report: StopReport, expected: tuple[str, ...]
    ) -> None:
        if not self._require_stop_ownership(report, expected, "working-order listing"):
            return
        listing = self.engine.run(["options-positions"])
        working = set(
            re.findall(
                r"working[^\n]*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                listing.stdout,
                flags=re.IGNORECASE,
            )
        )
        if not working:
            report.add("working entry orders", True, "none to cancel")
            return
        failures = 0
        for strategy_id in sorted(working):
            if not self._require_stop_ownership(report, expected, "working-order cancel"):
                return
            cancelled = self.engine.run(
                [
                    "options-cancel",
                    "--strategy-id",
                    strategy_id,
                    "--reason",
                    "paper-day stop",
                    "--arm",
                ]
            )
            if cancelled.code != 0:
                failures += 1
        report.add(
            "working entry orders",
            failures == 0,
            f"cancelled {len(working) - failures}/{len(working)}"
            + (" -- manual attention required" if failures else ""),
        )

    def _settle_handoffs(self, report: StopReport, now: dt.datetime) -> int:
        expected = self._active_stop_owner
        if expected is not None and not self._require_stop_ownership(
            report, expected, "handoff listing"
        ):
            return 0
        try:
            store = self._store()
        except EngineError as exc:
            report.add("outstanding handoffs", False, str(exc))
            return 0
        settled = 0
        for handoff in store.list(("pending", "claimed"), sender="builder", tag="verification"):
            if expected is not None and not self._require_stop_ownership(
                report, expected, "handoff settlement"
            ):
                return settled
            with contextlib.suppress(Exception):
                if handoff.status == "pending":
                    store.claim(handoff.id, by="builder")
                if expected is not None and not self._require_stop_ownership(
                    report, expected, "handoff completion"
                ):
                    return settled
                store.complete(
                    handoff.id,
                    note=f"SESSION_CLOSED unanswered at paper-day stop {now.isoformat()}",
                    by="builder",
                )
                settled += 1
        acknowledged = 0
        for handoff in store.list(("pending", "claimed"), to="builder"):
            if expected is not None and not self._require_stop_ownership(
                report, expected, "inbound handoff settlement"
            ):
                return settled
            with contextlib.suppress(Exception):
                if handoff.status == "pending":
                    store.claim(handoff.id, by="builder")
                if expected is not None and not self._require_stop_ownership(
                    report, expected, "inbound handoff completion"
                ):
                    return settled
                store.complete(
                    handoff.id,
                    note=f"received during paper-day stop {now.isoformat()}",
                    by="builder",
                )
                acknowledged += 1
        report.add(
            "outstanding handoffs",
            True,
            f"{settled} open proposal(s) closed SESSION_CLOSED, {acknowledged} inbound acknowledged",
        )
        return settled

    def _write_summary(
        self, report: StopReport, now: dt.datetime, session_id: str, pending_reviews: int
    ) -> Path:
        self.paths.summaries.mkdir(parents=True, exist_ok=True)
        path = self.paths.summaries / f"{now:%Y-%m-%d}-session-summary.md"
        orders_today = self._orders_today(now)
        consumed_dir = self.paths.verification_ledger / "consumed"
        consumed = len(list(consumed_dir.glob("*.used"))) if consumed_dir.is_dir() else 0
        verification = _read_json(self.paths.last_verification) or {}
        lines = [
            f"# Paper-day session summary -- {now:%Y-%m-%d}",
            "",
            f"- session id: {session_id}",
            f"- stopped at: {now.isoformat()}",
            f"- orders placed today: {orders_today}",
            f"- approvals consumed to date: {consumed}",
            f"- proposals closed SESSION_CLOSED at stop: {pending_reviews}",
            f"- last liveness verification: {verification.get('liveness_at', 'never')}",
            f"- last mechanics proof: {verification.get('mechanics_proof_at', 'never')}",
            "",
            "## Stop steps",
            "",
        ]
        lines += [step.line() for step in report.steps]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _reviewer_shutdown(self, report: StopReport, now: dt.datetime) -> None:
        expected = self._active_stop_owner
        if expected is not None and not self._require_stop_ownership(
            report, expected, "reviewer shutdown request"
        ):
            return
        try:
            store = self._store()
        except EngineError as exc:
            report.add("reviewer shutdown", False, str(exc))
            return
        request = store.create(
            to="reviewer",
            sender="builder",
            title=f"TRADING_DAY_CLOSED {now:%Y-%m-%d} -- please reply REVIEWER_STOPPED",
            body=(
                "The paper-day controller is stopping. Entry gate is CLOSED; all open\n"
                "proposals were completed SESSION_CLOSED. Please wind down and reply\n"
                "REVIEWER_STOPPED."
            ),
            priority="high",
            tags=["shutdown", "verifier", "paperday"],
        )
        deadline = time.monotonic() + self.liveness_timeout
        while time.monotonic() < deadline:
            for handoff in store.list(("pending", "claimed"), to="builder"):
                if expected is not None and not self._require_stop_ownership(
                    report, expected, "reviewer shutdown response"
                ):
                    return
                if handoff.thread not in (request.thread, request.id):
                    continue
                if "REVIEWER_STOPPED" in str(handoff.body or ""):
                    with contextlib.suppress(Exception):
                        if handoff.status == "pending":
                            store.claim(handoff.id, by="builder")
                        if expected is not None and not self._require_stop_ownership(
                            report, expected, "reviewer shutdown completion"
                        ):
                            return
                        store.complete(
                            handoff.id,
                            note=f"builder acknowledged at {self.clock().isoformat()}",
                            by="builder",
                        )
                    report.add("reviewer shutdown", True, f"REVIEWER_STOPPED ({handoff.id})")
                    return
            self.sleep(self.liveness_poll)
        report.add(
            "reviewer shutdown",
            False,
            f"no REVIEWER_STOPPED within {int(self.liveness_timeout)}s -- "
            "proceeding; the reviewer can close the request later",
        )

    def _stop_watcher(
        self,
        report: StopReport,
        expected: tuple[str, ...],
        expected_record: dict[str, Any] | None,
    ) -> None:
        if not self._require_stop_ownership(report, expected, "watcher stop"):
            return
        recorded = _read_json(self.paths.watcher_pid)
        if recorded != expected_record:
            report.add(
                "builder watcher",
                False,
                "watcher record changed during stop -- replacement watcher retained",
            )
            return
        if recorded is None:
            report.add("builder watcher", True, "no pid file -- nothing to stop")
            return
        pid = recorded.get("pid")
        if not isinstance(pid, int) or not self.processes.alive(pid):
            report.add("builder watcher", True, f"pid {pid} already gone")
        elif _WATCHER_NEEDLE not in self.processes.cmdline(pid):
            report.add(
                "builder watcher",
                False,
                f"pid {pid} belongs to another process now -- not killed, record retained",
            )
            return
        elif not self._watcher_record_matches_lock(
            recorded,
            {
                "session_id": expected[1] if expected[0] == "session" else None,
                "fencing_token": expected[2] if expected[0] == "session" else None,
            },
        ):
            report.add(
                "builder watcher",
                False,
                "live watcher record is not bound to this session/fencing token; "
                "not killed, record retained",
            )
            return
        else:
            if not self._require_stop_ownership(report, expected, "watcher termination"):
                return
            self.processes.terminate(pid)
            report.add("builder watcher", True, f"terminated pid {pid}")
        if not self._stop_owns(expected) or _read_json(self.paths.watcher_pid) != expected_record:
            report.add(
                "builder watcher",
                False,
                "watcher ownership changed before record removal -- record retained",
            )
            return
        with contextlib.suppress(OSError):
            self.paths.watcher_pid.unlink()

    def _orders_today(self, now: dt.datetime) -> int:
        count = 0
        try:
            with open(self.paths.journal, encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("event") == "order_placed" and str(
                        record.get("ts", "")
                    ).startswith(f"{now:%Y-%m-%d}"):
                        count += 1
        except OSError:
            pass
        return count

    # ==================================================================
    # STATUS
    # ==================================================================

    def _authority_status(self, now: dt.datetime) -> str:
        gate = read_gate(self.paths)
        if gate is None:
            return "BLOCKED: no gate"
        refusal = entry_gate_preflight(
            self.paths,
            processes=self.processes,
            authority_liveness_ttl_seconds=AUTHORITY_LIVENESS_TTL_SECONDS,
        )(armed=True, now=now)
        if refusal is not None:
            return f"BLOCKED: {refusal}"
        return (
            "VALID: session "
            f"{gate.get('session_id', '?')} / fence {gate.get('fencing_token', '?')}"
        )

    def _scheduler_status(self, now: dt.datetime) -> str:
        try:
            from .scheduler import SchedulerPaths, read_scheduler_record

            paths = SchedulerPaths(root=self.paths.root)
            record = read_scheduler_record(paths)
            if record is None:
                terminal = _read_json(paths.terminal)
                if terminal and terminal.get("clean_exit") is True:
                    return f"stopped cleanly at {terminal.get('at', '?')}"
                return "MISSING/UNPROVEN"
            pid = record.get("pid")
            identity = f"{record.get('session_id', '?')}:{record.get('nonce', '?')}"
            alive = type(pid) is int and self.processes.alive(pid)
            heartbeat = _read_json(paths.heartbeat) or {}
            heartbeat_at = heartbeat.get("at", "never")
            return (
                f"pid {pid} {'ALIVE' if alive else 'DEAD'} identity {identity}; "
                f"heartbeat {heartbeat_at}"
            )
        except (ImportError, OSError, ValueError) as exc:
            return f"UNREADABLE: {type(exc).__name__}: {exc}"

    def _tick_status(self, now: dt.datetime) -> str:
        try:
            from .scheduler import SchedulerPaths

            path = SchedulerPaths(root=self.paths.root).receipts_for(
                now.astimezone(dt.timezone.utc).date()
            )
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return "none recorded"
        except OSError as exc:
            return f"UNREADABLE: {exc}"
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                return "UNREADABLE: malformed tick receipt"
            failure_code = record.get("failure_code")
            details = []
            if record.get("exit_code") is not None:
                details.append(f"exit={record.get('exit_code')}")
            if failure_code:
                details.append(str(failure_code))
            suffix = f" [{', '.join(details)}]" if details else ""
            return (
                f"{record.get('outcome', 'UNKNOWN')} tick {record.get('tick_id', '?')} "
                f"at {record.get('at', '?')}{suffix}"
            )
        return "none recorded"

    def status(self) -> StatusReport:
        report = StatusReport()
        now = self.clock()

        try:
            config = (self.config_loader or self._load_config)()
            report.add("environment", f"{config.venue} ({config.host}:{config.port}) -- PAPER")
            report.add("account", config.account_id)
            broker = "listening" if self.tcp_probe(config.host, config.port) else "NOT REACHABLE"
            report.add("broker port", broker)
        except EngineError as exc:
            report.add("environment", f"CONFIG REFUSED: {exc}")

        report.add("StateDir", str(self.paths.state_dir))
        gate_for_hashes = read_gate(self.paths) or {}
        report.add("policy SHA-256", str(gate_for_hashes.get(GATE_POLICY_SHA256, "missing")))
        report.add("catalog SHA-256", str(gate_for_hashes.get(GATE_CATALOG_SHA256, "missing")))
        report.add("config SHA-256", str(gate_for_hashes.get(GATE_CONFIG_SHA256, "missing")))

        recorded = _read_json(self.paths.watcher_pid) or {}
        pid = recorded.get("pid")
        if isinstance(pid, int) and self.processes.alive(pid) and _WATCHER_NEEDLE in (
            self.processes.cmdline(pid)
        ):
            report.add("claude watcher", f"pid {pid} HEALTHY (started {recorded.get('started_at', '?')})")
        elif pid is not None:
            report.add("claude watcher", f"pid {pid} DEAD or reused -- run start-paper-day")
        else:
            report.add("claude watcher", "not running")

        reviewer_pids = sorted(
            {p for needle in _REVIEWER_NEEDLES for p in self.processes.pids_matching(needle)}
        )
        report.add(
            "grok watcher",
            f"pids {reviewer_pids} HEALTHY" if reviewer_pids else "not detected -- verifier unavailable",
        )

        verification = _read_json(self.paths.last_verification) or {}
        report.add("verifier readiness", verification.get("liveness_at", "never verified"))
        report.add("last mechanics proof", verification.get("mechanics_proof_at", "never"))

        with contextlib.suppress(Exception):
            store = self._store()
            pending = store.list(("pending",))
            claimed = store.list(("claimed",))
            report.add(
                "handoffs",
                f"{len(pending)} pending, {len(claimed)} claimed "
                f"({sum(1 for h in pending if h.to == 'builder')} inbound pending)",
            )

        gate = read_gate(self.paths)
        if gate is None:
            report.add("entry gate", "no gate file -- armed entries refuse until start-paper-day")
        else:
            report.add(
                "entry gate",
                f"{gate.get('entry_gate')} (state {gate.get('state')}, as of {gate.get('as_of')})",
            )
        report.add("entry authority", self._authority_status(now))
        report.add(
            "recovery",
            "REQUIRED: " + str(gate_for_hashes.get("recovery_reason", "unresolved"))
            if gate_for_hashes.get(GATE_RECOVERY_REQUIRED)
            else "clear",
        )
        report.add("scheduler authority", self._scheduler_status(now))
        report.add("latest tick", self._tick_status(now))
        report.add("session lock", "held" if self.paths.lock.exists() else "none")

        report.add("open positions", self._positions_line())
        report.add("orders today", str(self._orders_today(now)))
        report.add("last mark", self._last_mark_line())

        shutdown = _read_json(self.paths.last_shutdown)
        report.add(
            "last clean shutdown",
            f"{shutdown.get('at')} (clean={shutdown.get('clean')})" if shutdown else "none recorded",
        )
        return report

    def _positions_line(self) -> str:
        try:
            from .options.positions import PositionStore

            store = PositionStore(self.paths.state_dir / "positions.jsonl")
            open_positions = list(store.open_positions())
        except Exception as exc:  # noqa: BLE001 - status must not crash on a bad book
            return f"unreadable: {type(exc).__name__}: {exc}"
        if not open_positions:
            return "none"
        parts = []
        for position in open_positions:
            intent = position.intent
            parts.append(
                f"{intent.underlying} {intent.strategy_type.value} x{intent.quantity} "
                f"exp {intent.expiration.isoformat()} (reserved {position.buying_power_reserved})"
            )
        return "; ".join(parts)

    def _last_mark_line(self) -> str:
        try:
            lines = self.paths.journal.read_text(encoding="utf-8").splitlines()
        except OSError:
            return "no journal"
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("event") == "position_mark":
                return (
                    f"{record.get('ts', '?')} {record.get('underlying', '?')} "
                    f"state {record.get('state', '?')} -- {record.get('detail', '')}"
                ).strip()
        return "no mark recorded in journal"


# ---------------------------------------------------------------------------
# the consumption mechanics proof
# ---------------------------------------------------------------------------


def _collab_load(module: str, attribute: str) -> Any:
    loaded = _collabkit.load(module, attribute)
    if loaded is None:
        raise EngineError(
            f"collab-kit's {module}.{attribute} is not importable",
            hint=_collabkit.last_error() or "set KIT_DIR to the collab-kit checkout",
        )
    return loaded


def _consumption_mechanics_proof() -> tuple[bool, str]:
    """Prove require -> consume -> second-consume-refused on the shipped gate.

    Runs the *real* :class:`CollabVerifierGate` over a real collab-kit lifecycle
    in a throwaway temp directory, with this process taking the reviewer's turn
    via the exported :func:`render_response` -- the same seam the test suite
    uses. This proves the mechanics (exclusive consumption, reuse refusal, the
    six lifecycle checks) without asking the live reviewer to approve a
    synthetic trade. It is labeled a mechanics proof for exactly that reason;
    the *liveness* of the real reviewer is a separate start check.
    """
    import shutil
    from uuid import uuid4 as _uuid4

    from .errors import RefusedError
    from .options.approval import (
        ApprovalDecision,
        AuthorizedOrderSpec,
        CollabVerifierGate,
        render_response,
    )

    scratch = Path(tempfile.mkdtemp(prefix="paperday-proof-"))
    try:
        collab_paths = _collab_load("paths", "CollabPaths").at(scratch / "collab", "proof")
        collab_paths.ensure()
        store = _collab_load("store", "HandoffStore")(collab_paths)

        spec = AuthorizedOrderSpec(
            intent_id=_uuid4(),
            structure_digest="0" * 64,
            account="PROOF",
            port=7497,
            order_type="LMT",
            time_in_force="DAY",
            risk_digest="proof",
            governor_digest="proof",
            commit_sha="0" * 40,
            configuration_fingerprint="proof",
        )
        gate = CollabVerifierGate(root=Path(collab_paths.root), ledger=scratch / "ledger")

        now = dt.datetime.now(dt.timezone.utc)

        class _Packet:
            """Just enough packet for ``propose``/``require``: the spec it binds,
            the expiry it persists, and the two render hooks the handoff needs."""

            def __init__(self) -> None:
                self.spec = spec
                self.proposed_at = now
                self.expires_at = now + dt.timedelta(hours=1)

            def title(self) -> str:
                return f"MECHANICS PROOF: paper-day gate [{spec.digest[:12]}]"

            def render(self) -> str:
                return (
                    "Start-time mechanics proof of the verifier gate. Synthetic spec,\n"
                    "real lifecycle, throwaway collab. Nothing here authorizes a trade.\n"
                    f"\nSpec digest: {spec.digest}\n"
                )

        packet = _Packet()
        request_id = gate.propose(packet, now=now)  # type: ignore[arg-type]

        store.claim(request_id, by="reviewer")
        body = render_response(
            decision=ApprovalDecision.APPROVED,
            request_id=request_id,
            intent_id=spec.intent_id,
            spec_digest=spec.digest,
            approved_at=now,
            expires_at=now + dt.timedelta(hours=1),
            reasons="paper-day start mechanics proof",
        )
        store.reply(
            request_id,
            title="APPROVED: paper-day mechanics proof",
            body=body,
            sender="reviewer",
        )

        approval = gate.require(packet, now=now)  # type: ignore[arg-type]
        gate.consume(approval, now=now)
        try:
            gate.consume(approval, now=now)
        except RefusedError:
            return True, (
                "require -> consume -> reuse REFUSED, on the shipped gate over a real "
                "temp lifecycle (scripted reviewer seat)"
            )
        return False, "SECOND CONSUME DID NOT REFUSE -- single-use is broken; do not open"
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# wrapper entry points
# ---------------------------------------------------------------------------


def _controller_from_args(argv: list[str]) -> tuple[PaperDayController, list[str]]:
    timeout = 180.0
    schedule_config: Path | None = None
    schedule_config_sha256: str | None = None
    state_dir: Path | None = None
    mandate = MANDATE_MANAGE_ONLY
    policy_sha256: str | None = None
    catalog_sha256: str | None = None
    config_sha256: str | None = None
    configuration_fingerprint: str | None = None
    rest: list[str] = []
    iterator = iter(argv)
    for token in iterator:
        if token == "--timeout":
            timeout = float(next(iterator, "180"))
        elif token == "--schedule-config":
            schedule_config = Path(next(iterator, ""))
        elif token == "--schedule-config-sha256":
            schedule_config_sha256 = next(iterator, "")
        elif token == "--state-dir":
            state_dir = Path(next(iterator, ""))
        elif token == "--mandate":
            mandate = next(iterator, MANDATE_MANAGE_ONLY)
        elif token == "--policy-sha256":
            policy_sha256 = next(iterator, "")
        elif token == "--catalog-sha256":
            catalog_sha256 = next(iterator, "")
        elif token == "--config-sha256":
            config_sha256 = next(iterator, "")
        elif token == "--configuration-fingerprint":
            configuration_fingerprint = next(iterator, "")
        else:
            rest.append(token)
    paths = PaperDayPaths.default() if state_dir is None else PaperDayPaths(state_dir=state_dir)
    controller = PaperDayController(
        paths=paths,
        liveness_timeout=timeout,
        mandate=mandate,
        policy_sha256=policy_sha256,
        catalog_sha256=catalog_sha256,
        config_sha256=config_sha256,
        configuration_fingerprint=configuration_fingerprint,
    )
    if (schedule_config is None) != (schedule_config_sha256 is None):
        raise ValueError(
            "--schedule-config and --schedule-config-sha256 must be supplied together"
        )
    if schedule_config is not None:
        # The scheduler child runs from the engine directory, not from the
        # operator's shell cwd. Pin the exact absolute artifact before putting
        # it into the supervisor argv so relative wrapper inputs cannot resolve
        # to a different file (or disappear) in the child.
        schedule_config = schedule_config.expanduser().resolve()
    if schedule_config is not None and schedule_config_sha256 is not None:
        from .scheduler_bootstrap import build_scheduler_spec

        controller.scheduler = build_scheduler_spec(
            schedule_config=schedule_config,
            schedule_config_sha256=schedule_config_sha256,
            state_dir=controller.paths.state_dir,
            entry_script=Path(__file__).with_name("scheduler_main.py"),
        )
    return controller, rest


def main_start(argv: list[str] | None = None) -> int:
    try:
        controller, _ = _controller_from_args(list(argv or []))
    except EngineError as exc:
        print(f"CONFIG: {exc}")
        return exc.exit_code
    except ValueError as exc:
        print(f"CONFIG: {exc}")
        return EXIT_BLOCKED
    report = controller.start()
    print(report.render())
    return report.exit_code


def main_stop(argv: list[str] | None = None) -> int:
    controller, _ = _controller_from_args(list(argv or []))
    report = controller.stop()
    print(report.render())
    return report.exit_code


def main_status(argv: list[str] | None = None) -> int:
    controller, _ = _controller_from_args(list(argv or []))
    print(controller.status().render())
    return 0
