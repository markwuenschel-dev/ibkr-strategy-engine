"""Shared operational process primitives -- a leaf module, by design.

The daily session controller (:mod:`engine.paperday`) is not the only thing that
has to start a detached watcher, ask the OS what a PID's command line actually
is, shell out to one engine CLI command with the state directory pinned, or poke
a TCP port. Anything else that needs those -- a scheduler, a doctor, an operator
script -- must be able to reach them **without** importing ``paperday``, which
would be circular the moment ``paperday`` wanted to use the same scheduler.

So these live here instead, and this module imports nothing from the rest of the
package: stdlib only. Keeping it a leaf is the whole point -- the moment it
imports ``paperday``, ``options``, or ``cli``, the cycle it exists to prevent is
back.

``paperday`` re-exports every name defined here, so existing callers and the
operational test matrix keep working unchanged.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "SubprocessProcessPort",
    "EngineCommandResult",
    "EngineCommandRunner",
    "default_tcp_probe",
]


def _engine_dir() -> Path:
    """The ``engine/`` directory, located from this file -- never from cwd.

    The state directory defaulting to ``Path.cwd()/.engine`` has already
    produced one split-brain book (2026-07-31: a doctor run from the repo root
    invented a fresh empty ``.engine``). The controller refuses to repeat that:
    every path it derives is anchored to the installed package location.
    """
    return Path(__file__).resolve().parents[2]


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

    def run(self, args: list[str], *, timeout: float | None = 300.0) -> EngineCommandResult:
        """Run one command, or leave a declared persistent worker alive.

        ``options-cycle`` is the supervised paper-day worker rather than a
        one-shot tick.  Passing ``timeout=None`` to ``subprocess.run`` is the
        explicit runtime contract for that command; every legacy command keeps
        the finite timeout default.
        """
        env = {**os.environ, "IBKR_STATE_DIR": str(self.state_dir)}
        command = [
            sys.executable,
            "-c",
            "import sys; from engine.cli import main; sys.exit(main(sys.argv[1:]))",
            *args,
        ]
        run_kwargs: dict[str, object] = {
            "capture_output": True,
            "text": True,
            "env": env,
            "cwd": str(_engine_dir()),
            "check": False,
        }
        if timeout is not None:
            run_kwargs["timeout"] = timeout
        completed = subprocess.run(  # noqa: S603
            command,
            **run_kwargs,
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
