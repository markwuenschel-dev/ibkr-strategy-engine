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
import errno
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import _collabkit
from .errors import EngineError

__all__ = [
    "PaperDayPaths",
    "Check",
    "StartReport",
    "StopReport",
    "StatusReport",
    "PaperDayController",
    "entry_gate_preflight",
    "READY",
    "DEGRADED",
    "BLOCKED",
    "STOPPED",
    "GATE_OPEN",
    "GATE_PROOF_ONLY",
    "GATE_CLOSED",
    "main_start",
    "main_stop",
    "main_status",
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


def _engine_dir() -> Path:
    """The ``engine/`` directory, located from this file -- never from cwd.

    The state directory defaulting to ``Path.cwd()/.engine`` has already
    produced one split-brain book (2026-07-31: a doctor run from the repo root
    invented a fresh empty ``.engine``). The controller refuses to repeat that:
    every path it derives is anchored to the installed package location.
    """
    return Path(__file__).resolve().parents[2]


def _repo_root() -> Path:
    return _engine_dir().parent


@dataclass(frozen=True)
class PaperDayPaths:
    """Every file the controller owns, in one place."""

    state_dir: Path

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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


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
# ---------------------------------------------------------------------------


class SubprocessProcessPort:
    """Real process management, Windows-flavoured.

    ``cmdline`` matters as much as liveness: a PID alone can be reused by the
    OS, and killing or trusting a stranger's process because it inherited a
    number is exactly the stale-PID failure this controller exists to prevent.
    """

    def pids_matching(self, needle: str) -> list[int]:
        script = (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.CommandLine -like '*{needle}*' }} | "
            "Select-Object -ExpandProperty ProcessId"
        )
        out = self._powershell(script)
        return [int(token) for token in out.split() if token.strip().isdigit()]

    def cmdline(self, pid: int) -> str:
        script = (
            f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").CommandLine"
        )
        return self._powershell(script).strip()

    def alive(self, pid: int) -> bool:
        return bool(self.cmdline(pid))

    def spawn_detached(
        self, args: list[str], *, env: dict[str, str], cwd: Path, log: Path
    ) -> int:
        log.parent.mkdir(parents=True, exist_ok=True)
        detached = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        with open(log, "ab") as stream:
            process = subprocess.Popen(  # noqa: S603 - args are module-controlled
                args,
                stdout=stream,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                cwd=str(cwd),
                creationflags=detached if os.name == "nt" else 0,
            )
        return int(process.pid)

    def terminate(self, pid: int) -> None:
        if os.name == "nt":
            subprocess.run(  # noqa: S603
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:  # pragma: no cover - non-Windows fallback
            with contextlib.suppress(OSError):
                os.kill(int(pid), 15)

    def _powershell(self, script: str) -> str:
        completed = subprocess.run(  # noqa: S603
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout or ""


@dataclass
class EngineCommandResult:
    code: int
    stdout: str


class EngineCommandRunner:
    """Runs one engine CLI command in a subprocess with the state dir pinned.

    A subprocess rather than an in-process call so each command owns its broker
    connection and its failure exits cleanly; ``IBKR_STATE_DIR`` is pinned
    explicitly so the command operates on the same book regardless of cwd.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    def run(self, args: list[str], *, timeout: float = 300.0) -> EngineCommandResult:
        env = {**os.environ, "IBKR_STATE_DIR": str(self.state_dir)}
        completed = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-c",
                "import sys; from engine.cli import main; sys.exit(main(sys.argv[1:]))",
                *args,
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(_engine_dir()),
            timeout=timeout,
            check=False,
        )
        return EngineCommandResult(
            code=completed.returncode,
            stdout=(completed.stdout or "") + (completed.stderr or ""),
        )


def default_tcp_probe(host: str, port: int, *, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# the enforced entry gate
# ---------------------------------------------------------------------------


def read_gate(paths: PaperDayPaths) -> dict[str, Any] | None:
    return _read_json(paths.gate)


def write_gate(paths: PaperDayPaths, *, entry_gate: str, state: str, session_id: str,
               now: dt.datetime) -> None:
    _write_json(
        paths.gate,
        {
            "entry_gate": entry_gate,
            "state": state,
            "session_id": session_id,
            "as_of": now.isoformat(),
        },
    )


def entry_gate_preflight(paths: PaperDayPaths | None = None) -> Callable[..., str | None]:
    """The runner ``entry_preflight`` that makes the session gate real.

    Refusal semantics (the preflight runs before a verification proposal is
    filed, so refusing here prevents both proposals and orders):

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

    def preflight(*, intent: Any = None, snapshot: Any = None, market_data: Any = None,
                  policy: Any = None, now: Any = None, armed: bool = False) -> str | None:
        gate = read_gate(resolved)
        if gate is None:
            if armed:
                return (
                    "no paper-day session gate exists; armed opening entries require "
                    "PAPER_DAY_READY -- run bin\\start-paper-day.ps1"
                )
            return None
        entry_gate = str(gate.get("entry_gate", GATE_CLOSED))
        state = str(gate.get("state", "?"))
        if entry_gate == GATE_CLOSED:
            return (
                f"the paper-day entry gate is CLOSED (session state {state}); "
                "no new entry proposals or orders until the next start-paper-day"
            )
        if entry_gate == GATE_OPEN:
            if armed and not resolved.lock.exists():
                return (
                    "the paper-day gate says OPEN but no session lock exists -- "
                    "treating as a crashed session; armed entries refuse until "
                    "start-paper-day runs again"
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

    def __post_init__(self) -> None:
        if self.engine is None:
            self.engine = EngineCommandRunner(self.paths.state_dir)

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
            severity="degrading",
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
                severity="degrading",
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

        # -- 13. decide, write the gate, done ------------------------------
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
        write_gate(
            self.paths,
            entry_gate=entry_gate,
            state=report.state,
            session_id=report.session_id,
            now=now,
        )

    def _acquire_lock(self, report: StartReport, now: dt.datetime) -> str | None:
        """Returns "fresh", "already", or None (blocked)."""
        self.paths.root.mkdir(parents=True, exist_ok=True)
        existing = _read_json(self.paths.lock)
        if existing is not None:
            recorded = _read_json(self.paths.watcher_pid) or {}
            pid = recorded.get("pid")
            watcher_alive = (
                isinstance(pid, int)
                and self.processes.alive(pid)
                and _WATCHER_NEEDLE in self.processes.cmdline(pid)
            )
            if watcher_alive:
                report.session_id = str(existing.get("session_id", report.session_id))
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
            with contextlib.suppress(OSError):
                self.paths.lock.unlink()
        payload = json.dumps(
            {
                "session_id": report.session_id,
                "started_at": now.isoformat(),
                "controller_pid": os.getpid(),
            },
            indent=2,
        )
        try:
            handle = os.open(self.paths.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                report.add(
                    "session lock",
                    False,
                    "another start acquired the lock concurrently; re-run to verify",
                )
                return None
            raise
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
        report.add("session lock", True, f"acquired for {report.session_id}")
        return "fresh"

    def _ensure_watcher(self, report: StartReport) -> None:
        recorded = _read_json(self.paths.watcher_pid) or {}
        pid = recorded.get("pid")
        if isinstance(pid, int) and self.processes.alive(pid):
            if _WATCHER_NEEDLE in self.processes.cmdline(pid):
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
            {"pid": new_pid, "started_at": self.clock().isoformat(), "needle": _WATCHER_NEEDLE},
        )
        report.watcher_pid = new_pid
        report.add("builder watcher", True, f"started, pid {new_pid}")

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
        session_id = str((lock or {}).get("session_id", "unknown-session"))

        # -- 1. gate first: no new proposals from this instant --------------
        write_gate(
            self.paths, entry_gate=GATE_CLOSED, state=STOPPED, session_id=session_id, now=now
        )
        report.add("entry gate", True, "CLOSED before anything else")
        if lock is None:
            report.add("session lock", True, "no active session -- verifying stopped state")

        # -- 2. working entry orders ----------------------------------------
        self._cancel_working_entries(report)

        # -- 3. outstanding handoffs ----------------------------------------
        pending_reviews = self._settle_handoffs(report, now)

        # -- 4. reconcile and mark ------------------------------------------
        recon = self.engine.run(["options-positions"])
        report.add(
            "final reconcile",
            recon.code == 0 and "broker agrees" in recon.stdout,
            "broker agrees" if "broker agrees" in recon.stdout
            else f"exit {recon.code} -- resolve before the next session",
        )
        mark = self.engine.run(["options-mark"])
        marked = mark.code == 0 and bool(re.search(r"MARKED|COMMISSION_INCOMPLETE", mark.stdout))
        report.add(
            "final mark",
            True,
            "positions marked" if marked
            else "marking refused (normal outside market hours) -- last good mark stands",
        )

        # -- 5. session summary ---------------------------------------------
        summary_path = self._write_summary(report, now, session_id, pending_reviews)
        report.add("session summary", True, str(summary_path))

        # -- 6. ask the reviewer to stop ------------------------------------
        self._reviewer_shutdown(report, now)

        # -- 7. builder watcher ---------------------------------------------
        self._stop_watcher(report)

        # -- 8. clear only what is ours and valid ---------------------------
        if self.paths.lock.exists():
            with contextlib.suppress(OSError):
                self.paths.lock.unlink()
            report.add("session lock", True, "released")
        _write_json(
            self.paths.last_shutdown,
            {"at": now.isoformat(), "clean": report.clean, "session_id": session_id},
        )
        return report

    def _cancel_working_entries(self, report: StopReport) -> None:
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
        try:
            store = self._store()
        except EngineError as exc:
            report.add("outstanding handoffs", False, str(exc))
            return 0
        settled = 0
        for handoff in store.list(("pending", "claimed"), sender="builder", tag="verification"):
            with contextlib.suppress(Exception):
                if handoff.status == "pending":
                    store.claim(handoff.id, by="builder")
                store.complete(
                    handoff.id,
                    note=f"SESSION_CLOSED unanswered at paper-day stop {now.isoformat()}",
                    by="builder",
                )
                settled += 1
        acknowledged = 0
        for handoff in store.list(("pending", "claimed"), to="builder"):
            with contextlib.suppress(Exception):
                if handoff.status == "pending":
                    store.claim(handoff.id, by="builder")
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
                if handoff.thread not in (request.thread, request.id):
                    continue
                if "REVIEWER_STOPPED" in str(handoff.body or ""):
                    with contextlib.suppress(Exception):
                        if handoff.status == "pending":
                            store.claim(handoff.id, by="builder")
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

    def _stop_watcher(self, report: StopReport) -> None:
        recorded = _read_json(self.paths.watcher_pid)
        if recorded is None:
            report.add("builder watcher", True, "no pid file -- nothing to stop")
            return
        pid = recorded.get("pid")
        if not isinstance(pid, int) or not self.processes.alive(pid):
            report.add("builder watcher", True, f"pid {pid} already gone")
        elif _WATCHER_NEEDLE not in self.processes.cmdline(pid):
            report.add(
                "builder watcher",
                True,
                f"pid {pid} belongs to another process now -- not killed, record discarded",
            )
        else:
            self.processes.terminate(pid)
            report.add("builder watcher", True, f"terminated pid {pid}")
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
    rest: list[str] = []
    iterator = iter(argv)
    for token in iterator:
        if token == "--timeout":
            timeout = float(next(iterator, "180"))
        else:
            rest.append(token)
    controller = PaperDayController(liveness_timeout=timeout)
    return controller, rest


def main_start(argv: list[str] | None = None) -> int:
    controller, _ = _controller_from_args(list(argv or []))
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
