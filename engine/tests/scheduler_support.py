"""Fakes for driving a whole simulated scheduler run without a clock or a broker.

Deliberately separate from ``paperday_support``. That module's
``FakeProcessPort.spawn_detached`` always returns the *builder watcher's*
command line, which is right for its own matrix and useless here: the scheduler's
whole identity check is that the child's command line carries this session's
nonce. A fake that cannot express a wrong command line cannot test the guard
that reads it.

Nothing here sleeps, and nothing here reads a real clock. ``FakeClock`` advances
only when a test or the loop's own ``sleep`` says so, so a simulated trading day
runs in microseconds and always the same way.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

from engine.runtime import EngineCommandResult
from engine.scheduler import SchedulerIdentity, SchedulerPaths, announce_ready

SESSION_ID = "paperday-20260813-abcdef12"
NONCE = "9f3c1a20"
OTHER_SESSION_ID = "paperday-20260814-11112222"

#: A fixed instant inside any session a test declares open. Tests that care
#: about the window drive ``is_open`` directly rather than moving this.
NOW = dt.datetime(2026, 8, 13, 14, 0, tzinfo=dt.timezone.utc)


class FakeClock:
    """A clock that only moves when something asks it to.

    Wall time and monotonic time advance together, so a test that drives the
    loop's ``sleep`` also moves the drain deadline -- which is what makes the
    ``STOP_DIRTY`` bound reachable without waiting for it.
    """

    def __init__(self, start: dt.datetime = NOW) -> None:
        self.now = start
        self.monotonic_seconds = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + dt.timedelta(seconds=seconds)
        self.monotonic_seconds += seconds

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)


class FakeProcesses:
    """An in-memory process table whose spawned command lines are the real args.

    ``spawn_detached`` records ``" ".join(args)`` as the command line precisely
    so a test can assert that the nonce reached it -- and so a test can plant a
    process whose command line is *almost* right and prove it is refused.
    """

    def __init__(
        self,
        *,
        announce_paths: SchedulerPaths | None = None,
        announces_ready: bool = True,
        after_announce: Callable[[int], None] | None = None,
    ) -> None:
        self.table: dict[int, str] = {}
        self.spawned: list[list[str]] = []
        self.terminated: list[int] = []
        self.spawn_error: Exception | None = None
        #: When set, a spawned child completes the startup handshake, learning
        #: who it is from its own argv -- which is exactly how the real child
        #: will learn it. Set ``announces_ready=False`` to model a scheduler
        #: that starts and never reports in.
        self.announce_paths = announce_paths
        self.announces_ready = announces_ready
        self.after_announce = after_announce
        self._next = 9000

    def add(self, cmdline: str, pid: int | None = None) -> int:
        pid = pid if pid is not None else self._next
        self._next = max(self._next, pid) + 1
        self.table[pid] = cmdline
        return pid

    def kill_silently(self, pid: int) -> None:
        self.table.pop(pid, None)

    # -- the port ---------------------------------------------------------

    def pids_matching(self, needle: str) -> list[int]:
        return [pid for pid, cmd in self.table.items() if needle in cmd]

    def cmdline(self, pid: int) -> str:
        return self.table.get(pid, "")

    def alive(self, pid: int) -> bool:
        return pid in self.table

    def spawn_detached(
        self, args: list[str], *, env: dict, cwd: Path, log: Path
    ) -> int:
        if self.spawn_error is not None:
            raise self.spawn_error
        rendered = [str(a) for a in args]
        self.spawned.append(rendered)
        pid = self.add(" ".join(rendered))
        if self.announce_paths is not None and self.announces_ready:
            for token in rendered:
                if token.startswith("--scheduler-session="):
                    session_id, _, nonce = token.split("=", 1)[1].partition(":")
                    announce_ready(
                        self.announce_paths,
                        SchedulerIdentity(session_id=session_id, nonce=nonce),
                        now=NOW,
                    )
            if self.after_announce is not None:
                self.after_announce(pid)
        return pid

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)
        self.table.pop(pid, None)


class FakeEngine:
    """Records every pass, and can act on the world while a pass is 'running'.

    ``on_run`` is the seam that makes the mid-tick fencing case testable: a pass
    that deletes the session lock while it runs is exactly the race where an
    order may already be at the broker.
    """

    def __init__(
        self,
        *,
        code: int = 0,
        stdout: str = "TRANSMITTED      0 order(s)",
        on_run: Callable[[], None] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.code = code
        self.stdout = stdout
        self.on_run = on_run

    def run(self, args: list[str], **_: Any) -> EngineCommandResult:
        self.calls.append(list(args))
        if self.on_run is not None:
            self.on_run()
        return EngineCommandResult(self.code, self.stdout)


def write_lock(paths: SchedulerPaths, session_id: str = SESSION_ID) -> Path:
    """Plant a paper-day session lock the scheduler will accept."""
    lock = paths.root / "session.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        '{"session_id": "%s", "controller_pid": 4242}' % session_id, encoding="utf-8"
    )
    return lock


def identity(session_id: str = SESSION_ID, nonce: str = NONCE) -> SchedulerIdentity:
    return SchedulerIdentity(session_id=session_id, nonce=nonce)


def paths_for(tmp_path: Path) -> SchedulerPaths:
    return SchedulerPaths(root=tmp_path / "paperday")


def always_open(_instant: dt.datetime) -> bool:
    return True


def always_closed(_instant: dt.datetime) -> bool:
    return False


def read_receipts(paths: SchedulerPaths, day: dt.date = NOW.date()) -> list[dict]:
    """Every receipt actually on disk, in order."""
    import json

    path = paths.receipts_for(day)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_terminal(
    paths: SchedulerPaths,
    scheduler_identity: SchedulerIdentity,
    *,
    clean_exit: bool = True,
    outcome: str = "STOPPED_QUIESCED",
) -> None:
    """Plant the durable child-exit marker used by drain tests."""
    paths.terminal.write_text(
        json.dumps(
            {
                "v": 1,
                "session_id": scheduler_identity.session_id,
                "nonce": scheduler_identity.nonce,
                "tick_id": "fixture-terminal",
                "outcome": outcome,
                "at": NOW.isoformat(),
                "clean_exit": clean_exit,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
