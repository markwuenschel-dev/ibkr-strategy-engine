"""The driver for a pass that already knows how to run once.

``engine.options.runner.run_once`` is deliberately a single pass with an explicit
``now``. ``LEDGER.md`` G5 records the consequence: *"The unit of work exists; the
driver does not."* This module is the driver, and it is deliberately the dumbest
thing that can be correct -- it decides **when** to invoke a pass and **whether
it is still allowed to**, and it decides nothing at all about what the pass does.

Three properties are the reason it exists rather than a ``while True`` in a
script:

**It cannot outlive its session.** Every tick re-reads the paper-day session lock
and compares the ``session_id`` to its own. A scheduler whose session ended --
cleanly, by crash, or because a *different* session now holds the lock -- stops
at the next boundary rather than managing a book it no longer has a mandate
over. Checking that a lock merely *exists* is not enough: a new session
acquiring a new lock must not silently re-license a predecessor.

**It drains rather than dies.** A tick that has already invoked the engine may
have transmitted an order the broker has accepted and the journal has not yet
recorded. Killing that process strands the outcome. So shutdown requests a
quiesce, lets the running tick finish inside a bound, and only then terminates
-- recording ``STOP_DIRTY`` if the bound was reached.

**It never kills a stranger.** A PID is a name the OS reuses. Identity here is a
session id *and* a nonce carried in the child's own command line, which is
stricter than the builder watcher's needle-only check: a scheduler left over
from yesterday's session matches the script name but not the nonce, so it can be
neither adopted nor terminated by mistake.

**What this module refuses to decide.** Cadence, trading window and the command
to run are constructor arguments with **no defaults**. There is no "reasonable"
tick interval or session window hiding in here to be inherited by accident;
choosing them is policy, made one layer up and stated explicitly. The same rule
the market calendar follows.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "TickOutcome",
    "SchedulerIdentity",
    "SchedulerPaths",
    "TickReceipt",
    "SchedulerLoop",
    "SchedulerSpec",
    "adopt_or_spawn",
    "request_quiesce",
    "clear_quiesce",
    "quiesce_requested",
    "announce_ready",
    "clear_ready",
    "ready_for",
    "identity_from_record",
    "drain_and_stop",
    "read_scheduler_record",
    "read_terminal_receipt",
    "session_id_holding",
]


class TickOutcome(Enum):
    """Why a tick did what it did. Every one of these reaches a receipt.

    ``STOPPED_*`` members end the loop; the others continue it. The distinction
    matters to an operator reading the tail of a receipt file: a scheduler that
    stopped because its lease went away is healthy, and one that stopped with an
    unresolved pass is not.
    """

    RAN = "RAN"
    SKIPPED_SESSION_CLOSED = "SKIPPED_SESSION_CLOSED"
    STOPPED_LEASE_LOST = "STOPPED_LEASE_LOST"
    STOPPED_QUIESCED = "STOPPED_QUIESCED"
    STOPPED_TICK_BUDGET = "STOPPED_TICK_BUDGET"
    UNRESOLVED_LEASE_LOST_MID_TICK = "UNRESOLVED_LEASE_LOST_MID_TICK"

    @property
    def is_terminal(self) -> bool:
        """Whether this outcome ends the loop.

        Listed explicitly rather than derived from the ``STOPPED_`` prefix.
        The prefix rule looks equivalent and is not:
        ``UNRESOLVED_LEASE_LOST_MID_TICK`` must also end the loop -- a scheduler
        that lost its mandate while a pass was running has to stop *now*, not
        sleep a cadence first and discover it on the next tick. Deriving
        control flow from a name is how that distinction gets lost in a rename.
        """
        return self in _TERMINAL_OUTCOMES


#: Outcomes that end the loop. See :meth:`TickOutcome.is_terminal` for why this
#: is a list rather than a name-prefix rule.
_TERMINAL_OUTCOMES = frozenset(
    {
        TickOutcome.STOPPED_LEASE_LOST,
        TickOutcome.STOPPED_QUIESCED,
        TickOutcome.STOPPED_TICK_BUDGET,
        TickOutcome.UNRESOLVED_LEASE_LOST_MID_TICK,
    }
)


@dataclass(frozen=True)
class SchedulerIdentity:
    """Who this scheduler is, in a form the OS can be asked about.

    ``needle`` goes on the child's command line so ``cmdline(pid)`` can confirm
    identity later. It carries the nonce as well as the session id because a
    session id alone is guessable and, worse, *reusable* -- a restarted session
    that picked the same id would otherwise adopt a stale process.
    """

    session_id: str
    nonce: str

    def __post_init__(self) -> None:
        for name in ("session_id", "nonce"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"scheduler identity needs a non-empty {name}; "
                    "identity is what stops a stranger's process being adopted"
                )

    @property
    def needle(self) -> str:
        return f"--scheduler-session={self.session_id}:{self.nonce}"


@dataclass(frozen=True)
class SchedulerPaths:
    """Everything the scheduler owns on disk, under the paper-day root."""

    root: Path

    @property
    def pid(self) -> Path:
        return self.root / "scheduler.pid"

    @property
    def quiesce(self) -> Path:
        return self.root / "scheduler.quiesce"

    @property
    def heartbeat(self) -> Path:
        return self.root / "scheduler.heartbeat"

    @property
    def log(self) -> Path:
        return self.root / "scheduler.log"

    @property
    def terminal(self) -> Path:
        return self.root / "scheduler.terminal"

    @property
    def claim(self) -> Path:
        """The atomic supervisor-start claim for this paper-day session."""
        return self.root / "scheduler.claim"

    @property
    def receipts(self) -> Path:
        return self.root / "receipts"

    def receipts_for(self, day: dt.date) -> Path:
        return self.receipts / f"{day:%Y-%m-%d}-ticks.jsonl"


@dataclass(frozen=True)
class TickReceipt:
    """One durable line of evidence per tick, including the ticks that did nothing.

    Skips are recorded, not silently omitted. A receipt file that only contains
    the ticks that ran cannot distinguish "the window was closed" from "the
    scheduler was dead", which is the question an operator actually has.
    """

    tick_id: str
    at: dt.datetime
    outcome: TickOutcome
    detail: str
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    duration_seconds: float | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "v": 1,
            "tick_id": self.tick_id,
            "at": self.at.isoformat(),
            "outcome": self.outcome.value,
            "detail": self.detail,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
        }


def _append_receipt(paths: SchedulerPaths, receipt: TickReceipt) -> None:
    """Publish the complete JSONL receipt file through atomic replacement.

    A reader must see either the prior complete file or the prior file plus the
    new complete line, never a partially appended line.  Existing input is
    validated before replacement so malformed evidence fails closed without
    destroying the last readable snapshot.
    """
    path = paths.receipts_for(receipt.at.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""

    for line_number, line in enumerate(existing.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"malformed receipt JSON at {path}:{line_number}"
            ) from exc
        if not isinstance(record, dict):
            raise ValueError(
                f"receipt JSON must be an object at {path}:{line_number}"
            )

    line = json.dumps(receipt.to_record(), sort_keys=True) + "\n"
    separator = "" if not existing or existing.endswith(("\n", "\r")) else "\n"
    _atomic_write_text(path, existing + separator + line)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace *path* atomically after durable sibling-file publication."""
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
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def _atomic_write_json(path: Path, record: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(record, indent=2, sort_keys=True))


def _claim_start(paths: SchedulerPaths, identity: SchedulerIdentity) -> bool:
    """Claim the right to spawn before the read-then-spawn window.

    A PID record is published only after the child handshake, so it cannot
    serialize two simultaneous supervisors that both observe "no PID". The
    exclusive claim is that serialization point. An unreadable or foreign
    claim is not silently removed; callers must prove it is stale through a
    clean terminal receipt before reusing it.
    """

    existing = _read_json(paths.claim)
    if existing is not None:
        if (
            existing.get("session_id") == identity.session_id
            and existing.get("nonce") == identity.nonce
            and _clean_exit_proven(paths, identity)
        ):
            with contextlib.suppress(OSError):
                paths.claim.unlink()
        else:
            return False

    paths.root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "v": 1,
            "session_id": identity.session_id,
            "nonce": identity.nonce,
        },
        sort_keys=True,
    ).encode("utf-8")
    try:
        handle = os.open(paths.claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        with contextlib.suppress(OSError):
            paths.claim.unlink()
        raise
    return True


def _release_start_claim(paths: SchedulerPaths, identity: SchedulerIdentity) -> None:
    record = _read_json(paths.claim)
    if record is None:
        return
    if record.get("session_id") == identity.session_id and record.get("nonce") == identity.nonce:
        with contextlib.suppress(OSError):
            paths.claim.unlink()


def _write_terminal_receipt(
    paths: SchedulerPaths,
    identity: SchedulerIdentity,
    receipt: TickReceipt,
) -> None:
    """Publish the final process state only after the final tick is durable."""
    _atomic_write_json(
        paths.terminal,
        {
            "v": 1,
            "session_id": identity.session_id,
            "nonce": identity.nonce,
            "tick_id": receipt.tick_id,
            "outcome": receipt.outcome.value,
            "at": receipt.at.isoformat(),
            "clean_exit": receipt.outcome is not TickOutcome.UNRESOLVED_LEASE_LOST_MID_TICK,
        },
    )


def read_terminal_receipt(paths: SchedulerPaths) -> dict[str, Any] | None:
    """Read a durable clean-exit marker, if one exists."""
    record = _read_json(paths.terminal)
    if record is None or record.get("v") != 1:
        return None
    if not isinstance(record.get("session_id"), str) or not isinstance(
        record.get("nonce"), str
    ):
        return None
    return record


def _clean_exit_proven(paths: SchedulerPaths, identity: SchedulerIdentity) -> bool:
    record = read_terminal_receipt(paths)
    return bool(
        record
        and record.get("session_id") == identity.session_id
        and record.get("nonce") == identity.nonce
        and record.get("clean_exit") is True
    )


def session_id_holding(lock: Path) -> str | None:
    """The session id in the paper-day lock, or None if there is no usable lock.

    A malformed or truncated lock reads as *no* lock. That is the fail-closed
    direction: the scheduler stops, rather than continuing against a session it
    cannot identify.
    """
    record = _read_json(lock)
    if record is None:
        return None
    session_id = record.get("session_id")
    return session_id if isinstance(session_id, str) and session_id.strip() else None


def read_scheduler_record(paths: SchedulerPaths) -> dict[str, Any] | None:
    return _read_json(paths.pid)


def identity_from_record(record: dict[str, Any] | None) -> SchedulerIdentity | None:
    """Rebuild the identity a running scheduler was started with.

    Stop needs this: it must prove the recorded PID is *our* scheduler before
    terminating it, and the only durable statement of which scheduler that was
    is the record itself. A record missing either half yields ``None``, and the
    caller must then refuse to terminate anything -- an unidentifiable process
    is a stranger by default.
    """
    if not record:
        return None
    session_id = record.get("session_id")
    nonce = record.get("nonce")
    if not isinstance(session_id, str) or not isinstance(nonce, str):
        return None
    if not session_id.strip() or not nonce.strip():
        return None
    return SchedulerIdentity(session_id=session_id, nonce=nonce)


def request_quiesce(paths: SchedulerPaths, *, reason: str, now: dt.datetime) -> None:
    """Publish the stop request as one durable, parseable state transition.

    The scheduler reads this file at the top of every tick.  A direct write can
    leave a truncated JSON document visible to the child, which is equivalent
    to a lost stop request.  Use the same atomic publication discipline as the
    heartbeat, PID record, and terminal receipt.
    """
    _atomic_write_json(
        paths.quiesce,
        {"v": 1, "reason": reason, "at": now.isoformat()},
    )


def clear_quiesce(paths: SchedulerPaths) -> None:
    with contextlib.suppress(OSError):
        paths.quiesce.unlink()


def announce_ready(
    paths: SchedulerPaths, identity: SchedulerIdentity, *, now: dt.datetime
) -> None:
    """The child's half of the startup handshake.

    Called by the scheduler process once it has loaded its policy and is about
    to take its first tick. The supervisor waits for this before calling the day
    started, which is what turns "we spawned something" into "a scheduler for
    *this* session is running" -- a spawned PID that is merely alive proves
    neither that it read this session's gate nor that it is ours.
    """
    _atomic_write_json(
        paths.heartbeat,
        {
            "session_id": identity.session_id,
            "nonce": identity.nonce,
            "at": now.isoformat(),
        },
    )


def clear_ready(paths: SchedulerPaths) -> None:
    with contextlib.suppress(OSError):
        paths.heartbeat.unlink()


def ready_for(paths: SchedulerPaths, identity: SchedulerIdentity) -> bool:
    """Whether the heartbeat on disk was written by *this* identity.

    A heartbeat from a previous session is not a handshake; it is litter. The
    nonce is what tells them apart.
    """
    record = _read_json(paths.heartbeat)
    if record is None:
        return False
    return (
        record.get("session_id") == identity.session_id
        and record.get("nonce") == identity.nonce
    )


def quiesce_requested(paths: SchedulerPaths) -> bool:
    return paths.quiesce.exists()


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


@dataclass
class SchedulerLoop:
    """One scheduler process: tick, check, invoke, record, sleep.

    Single flight is structural rather than defended: ``run`` is synchronous, so
    a tick cannot begin while the previous one is still in the engine. The thing
    that *does* need defending is a second scheduler **process**, which
    :func:`adopt_or_spawn` prevents by identity.

    Every collaborator is injected with a real default only where the default is
    not a policy choice. ``cadence_seconds``, ``is_open`` and ``command`` have no
    defaults at all -- see the module docstring.
    """

    identity: SchedulerIdentity
    paths: SchedulerPaths
    lock: Path
    cadence_seconds: float
    is_open: Callable[[dt.datetime], bool]
    command: tuple[str, ...]
    engine: Any
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)
    sleep: Callable[[float], None] = None  # type: ignore[assignment]
    monotonic: Callable[[], float] = None  # type: ignore[assignment]
    command_timeout: float = 300.0
    receipts: list[TickReceipt] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.cadence_seconds <= 0:
            raise ValueError(
                "cadence_seconds must be positive; a non-positive cadence is a "
                "busy loop against the broker, not a schedule"
            )
        if not self.command:
            raise ValueError(
                "command must name the engine subcommand to run; the scheduler "
                "does not choose what a pass does"
            )
        if self.sleep is None:
            import time

            self.sleep = time.sleep
        if self.monotonic is None:
            import time

            self.monotonic = time.monotonic

    # -- one tick ---------------------------------------------------------

    def _record(self, receipt: TickReceipt) -> TickReceipt:
        self.receipts.append(receipt)
        _append_receipt(self.paths, receipt)
        return receipt

    def _tick_id(self, now: dt.datetime, index: int) -> str:
        return f"{self.identity.session_id}-{now:%Y%m%dT%H%M%S}-{index:04d}"

    def _lease_held(self) -> bool:
        return session_id_holding(self.lock) == self.identity.session_id

    def tick(self, index: int) -> TickReceipt:
        """Run exactly one tick and return its receipt.

        The lease is checked twice on purpose. Before the pass, because a
        scheduler without a mandate must not open a broker connection at all.
        After the pass, because a session that ended *while the engine was
        running* leaves an outcome nobody is now watching for -- that is
        recorded as ``UNRESOLVED_LEASE_LOST_MID_TICK`` rather than being
        smoothed into a normal success.
        """
        now = self.clock()
        tick_id = self._tick_id(now, index)

        if not self._lease_held():
            return self._record(
                TickReceipt(
                    tick_id=tick_id,
                    at=now,
                    outcome=TickOutcome.STOPPED_LEASE_LOST,
                    detail=(
                        f"session {self.identity.session_id} no longer holds "
                        f"{self.lock}; stopping without running a pass"
                    ),
                )
            )

        if quiesce_requested(self.paths):
            return self._record(
                TickReceipt(
                    tick_id=tick_id,
                    at=now,
                    outcome=TickOutcome.STOPPED_QUIESCED,
                    detail="quiesce requested before this tick started; no pass run",
                )
            )

        if not self.is_open(now):
            return self._record(
                TickReceipt(
                    tick_id=tick_id,
                    at=now,
                    outcome=TickOutcome.SKIPPED_SESSION_CLOSED,
                    detail=f"no trading session at {now.isoformat()}",
                )
            )

        started = self.monotonic()
        result = self.engine.run(list(self.command), timeout=self.command_timeout)
        elapsed = round(self.monotonic() - started, 3)

        if not self._lease_held():
            return self._record(
                TickReceipt(
                    tick_id=tick_id,
                    at=now,
                    outcome=TickOutcome.UNRESOLVED_LEASE_LOST_MID_TICK,
                    detail=(
                        "the session lease was lost while the pass was running; "
                        "the pass may have transmitted -- reconcile against the "
                        "broker before starting another session"
                    ),
                    command=tuple(self.command),
                    exit_code=getattr(result, "code", None),
                    duration_seconds=elapsed,
                )
            )

        return self._record(
            TickReceipt(
                tick_id=tick_id,
                at=now,
                outcome=TickOutcome.RAN,
                detail=f"pass exited {getattr(result, 'code', None)}",
                command=tuple(self.command),
                exit_code=getattr(result, "code", None),
                duration_seconds=elapsed,
            )
        )

    # -- the loop ---------------------------------------------------------

    def run(self, *, max_ticks: int | None = None) -> list[TickReceipt]:
        """Tick until the lease goes, a quiesce is requested, or the budget runs out.

        ``max_ticks`` is a test and operator bound, not a schedule. It exists so
        a simulated day terminates; ``None`` means run until something stops it.

        Readiness is announced before the first tick, not after it: the
        supervisor is blocking on the handshake, and a scheduler that waited
        until it had done some work would deadlock its own startup.
        """
        announce_ready(self.paths, self.identity, now=self.clock())
        index = 0
        while True:
            if max_ticks is not None and index >= max_ticks:
                now = self.clock()
                receipt = self._record(
                    TickReceipt(
                        tick_id=self._tick_id(now, index),
                        at=now,
                        outcome=TickOutcome.STOPPED_TICK_BUDGET,
                        detail=f"reached the {max_ticks}-tick budget",
                    )
                )
                _write_terminal_receipt(self.paths, self.identity, receipt)
                _release_start_claim(self.paths, self.identity)
                return self.receipts

            receipt = self.tick(index)
            index += 1
            if receipt.outcome.is_terminal:
                _write_terminal_receipt(self.paths, self.identity, receipt)
                _release_start_claim(self.paths, self.identity)
                return self.receipts
            self.sleep(self.cadence_seconds)


# ---------------------------------------------------------------------------
# supervision -- what the paper-day controller calls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchedulerSpec:
    """The policy a caller must state to get a scheduler at all.

    There is no default instance. A paper day without one of these simply runs
    no scheduler, which is exactly the behaviour before this module existed.
    """

    cadence_seconds: float
    command: tuple[str, ...]
    entry_script: Path
    #: Explicit arguments consumed by the scheduler entrypoint itself. The
    #: engine command remains separate: these are policy/bootstrap inputs, not
    #: options-run arguments.
    entry_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.cadence_seconds <= 0:
            raise ValueError("cadence_seconds must be positive")
        if not self.command:
            raise ValueError("command must name the engine subcommand to run")
        if not self.entry_script.exists():
            raise ValueError(
                f"scheduler entry script {self.entry_script} does not exist. "
                "NOTE: this repository ships no production scheduler entrypoint "
                "yet -- see the module docstring. Until one exists, unattended "
                "operation is not available, and a spec pointing at nothing "
                "would fail later as an unexplained readiness timeout instead "
                "of here."
            )
        if any(not isinstance(token, str) or not token for token in self.entry_args):
            raise ValueError("entry_args must contain only non-empty strings")


def adopt_or_spawn(
    *,
    processes: Any,
    paths: SchedulerPaths,
    identity: SchedulerIdentity,
    spec: SchedulerSpec,
    cwd: Path,
    env: dict[str, str],
    clock: Callable[[], dt.datetime],
    sleep: Callable[[float], None],
    python: str,
    monotonic: Callable[[], float],
    ready_timeout: float,
    ready_poll: float = 0.5,
) -> tuple[int | None, str]:
    """Adopt a live scheduler for *this* identity, or start one.

    Mirrors the builder watcher's adopt-or-spawn, with one difference that
    matters: the recorded process is adopted only when its command line carries
    this session's nonce. A scheduler from a previous session runs the same
    script and would pass a script-name check.

    Returns ``(pid_or_None, detail)``. A spawn failure is reported, never
    raised -- an absent scheduler degrades a day, it does not invalidate the
    book.
    """
    recorded = read_scheduler_record(paths)
    pid = (recorded or {}).get("pid")
    if isinstance(pid, int) and processes.alive(pid):
        if identity.needle in processes.cmdline(pid):
            return pid, f"already running, pid {pid}"
        return None, (
            f"pid {pid} is another process (not this session's scheduler) -- "
            "stale record discarded, not terminated"
        )

    clear_quiesce(paths)
    clear_ready(paths)
    with contextlib.suppress(OSError):
        paths.terminal.unlink()
    if not _claim_start(paths, identity):
        return None, (
            "another scheduler startup owns the atomic claim for this session; "
            "not spawning a second scheduler"
        )
    args = [
        python,
        str(spec.entry_script),
        *spec.entry_args,
        identity.needle,
        f"--cadence-seconds={spec.cadence_seconds:g}",
        "--",
        *spec.command,
    ]
    try:
        new_pid = processes.spawn_detached(
            args, env=env, cwd=cwd, log=paths.log
        )
    except Exception as exc:  # noqa: BLE001 - a scheduler that will not start degrades the day
        _release_start_claim(paths, identity)
        return None, f"could not start: {type(exc).__name__}: {exc}"

    sleep(1.0)
    if not processes.alive(new_pid):
        _release_start_claim(paths, identity)
        return None, f"pid {new_pid} exited immediately -- see {paths.log}"

    # The handshake. Liveness alone would accept a process that started, failed
    # to load its policy, and is about to exit -- and, worse, one belonging to
    # another session. Waiting for a heartbeat carrying this nonce is what makes
    # "started" mean something.
    deadline = monotonic() + ready_timeout
    while not ready_for(paths, identity):
        if monotonic() >= deadline:
            _release_start_claim(paths, identity)
            return None, (
                f"pid {new_pid} did not announce readiness within {ready_timeout:g}s "
                f"-- see {paths.log}; not recorded as this session's scheduler"
            )
        if not processes.alive(new_pid):
            _release_start_claim(paths, identity)
            return None, f"pid {new_pid} exited before announcing readiness -- see {paths.log}"
        sleep(ready_poll)

    # Readiness is a claim from the child, not a reservation on its PID. The
    # child may have exited and the OS may have reused the number while the
    # heartbeat was being observed, so fence the exact process immediately
    # before publishing scheduler.pid.
    if not processes.alive(new_pid):
        _release_start_claim(paths, identity)
        return None, (
            f"pid {new_pid} exited after announcing readiness -- "
            f"not recorded as this session's scheduler; see {paths.log}"
        )
    if identity.needle not in processes.cmdline(new_pid):
        _release_start_claim(paths, identity)
        return None, (
            f"pid {new_pid} changed identity after announcing readiness -- "
            f"not recorded as this session's scheduler; see {paths.log}"
        )

    _atomic_write_json(
        paths.pid,
        {
            "pid": new_pid,
            "started_at": clock().isoformat(),
            "needle": identity.needle,
            "session_id": identity.session_id,
            "nonce": identity.nonce,
        },
    )
    return new_pid, f"started, pid {new_pid}"


def drain_and_stop(
    *,
    processes: Any,
    paths: SchedulerPaths,
    identity: SchedulerIdentity,
    now: dt.datetime,
    drain_timeout: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    poll_seconds: float = 0.5,
) -> tuple[bool, str]:
    """Ask the scheduler to stop, wait for it, and only then force the issue.

    Returns ``(clean, detail)``. ``clean`` is False when the drain bound was
    reached and the process had to be terminated mid-tick -- the ``STOP_DIRTY``
    case, where a pass may have transmitted an order whose outcome nothing
    recorded. That is surfaced, never smoothed over.

    A recorded PID whose command line does not carry this identity is **not**
    terminated. It belongs to somebody else.
    """
    recorded = read_scheduler_record(paths)
    if recorded is None:
        if paths.pid.exists():
            request_quiesce(paths, reason="paper-day stop", now=now)
            return False, (
                "STOP_DIRTY: scheduler.pid exists but is unreadable or malformed; "
                "the scheduler's final state is unaccounted for, so reconcile "
                "against the broker"
            )
        return True, "no scheduler pid file -- nothing to stop"

    pid = recorded.get("pid")
    request_quiesce(paths, reason="paper-day stop", now=now)

    if type(pid) is not int or pid <= 0 or not processes.alive(pid):
        record_identity = identity_from_record(recorded)
        if record_identity == identity and _clean_exit_proven(paths, identity):
            with contextlib.suppress(OSError):
                paths.pid.unlink()
            return True, f"pid {pid} already gone after a durable clean exit"
        return False, (
            f"STOP_DIRTY: pid {pid!r} is missing, invalid, or already gone "
            "without a durable clean-exit receipt; the scheduler's final tick "
            "is unaccounted for, so reconcile against the broker"
        )

    if identity.needle not in processes.cmdline(pid):
        with contextlib.suppress(OSError):
            paths.pid.unlink()
        return False, (
            f"STOP_DIRTY: pid {pid} belongs to another process now -- not killed, "
            "record discarded. This session's scheduler could not be located, so "
            "its shutdown is unproven; the quiesce flag is set and any live tick "
            "will stop at its next boundary, but reconcile before the next session"
        )

    deadline = monotonic() + drain_timeout
    while monotonic() < deadline:
        if not processes.alive(pid):
            if _clean_exit_proven(paths, identity):
                with contextlib.suppress(OSError):
                    paths.pid.unlink()
                return True, f"pid {pid} drained and exited cleanly"
            return False, (
                f"STOP_DIRTY: pid {pid} exited without a durable clean-exit "
                "receipt; its final tick is unaccounted for, so reconcile "
                "against the broker"
            )
        sleep(poll_seconds)

    # Revalidate immediately before killing. The identity check above happened
    # up to `drain_timeout` ago; in that window the scheduler may have exited
    # and the OS handed its number to something else. Terminating on the
    # strength of the earlier check is the stale-PID failure with a delay in it.
    if identity.needle not in processes.cmdline(pid):
        with contextlib.suppress(OSError):
            paths.pid.unlink()
        return False, (
            f"STOP_DIRTY: pid {pid} stopped matching this scheduler during the "
            f"{drain_timeout:g}s drain -- not terminated. It exited on its own or "
            "the number was reused; either way its final tick is unaccounted for"
        )

    processes.terminate(pid)
    with contextlib.suppress(OSError):
        paths.pid.unlink()
    return False, (
        f"STOP_DIRTY: pid {pid} did not finish its tick within {drain_timeout:g}s "
        "and was terminated -- a pass may have transmitted; reconcile against the "
        "broker before the next session"
    )
