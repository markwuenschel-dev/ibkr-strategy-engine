"""Shared fakes for the paper-day controller tests.

Everything the controller touches at a boundary -- processes, engine CLI
subcommands, the TCP probe, the clock, config -- is faked here. The collab
exchange is deliberately NOT faked: tests run the real collab-kit store over a
temp directory, with :func:`reviewer_ready_on_sleep` taking the reviewer's turn,
so the liveness round-trip in every test is the shipped lifecycle end to end.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import reviewer as reviewer_support
from engine.paperday import EngineCommandResult, PaperDayController, PaperDayPaths

NOW = dt.datetime(2026, 8, 1, 13, 0, tzinfo=dt.timezone.utc)

WATCHER_CMD = "python.exe tools/watch-for-claude-handoffs.py"
REVIEWER_CMD = "python.exe tools/watch-for-grok-handoffs.py"


@dataclass
class StubConfig:
    account_id: str = "DU1234567"
    host: str = "127.0.0.1"
    port: int = 7497
    venue: str = "TWS paper"


class FakeProcessPort:
    """An in-memory process table. PIDs are handed out sequentially from 9000."""

    def __init__(self) -> None:
        self.table: dict[int, str] = {}
        self.spawned: list[list[str]] = []
        self.terminated: list[int] = []
        self.spawn_error: Exception | None = None
        self._next = 9000

    def add(self, cmdline: str, pid: int | None = None) -> int:
        pid = pid if pid is not None else self._next
        self._next = max(self._next, pid) + 1
        self.table[pid] = cmdline
        return pid

    def kill_silently(self, pid: int) -> None:
        """Simulate a process dying without the controller's involvement."""
        self.table.pop(pid, None)

    # -- the port ---------------------------------------------------------

    def pids_matching(self, needle: str) -> list[int]:
        return [pid for pid, cmd in self.table.items() if needle in cmd]

    def cmdline(self, pid: int) -> str:
        return self.table.get(pid, "")

    def alive(self, pid: int) -> bool:
        return pid in self.table

    def spawn_detached(self, args: list[str], *, env: dict, cwd: Path, log: Path) -> int:
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawned.append([str(a) for a in args])
        return self.add(WATCHER_CMD)

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)
        self.table.pop(pid, None)


class FakeEngine:
    """Canned engine CLI results, keyed by subcommand. Records every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.results: dict[str, EngineCommandResult] = {
            "status": EngineCommandResult(0, "connected to TWS paper as DU1234567"),
            "options-positions": EngineCommandResult(
                0, "  reconciled: 1 open position(s), broker agrees"
            ),
            "options-mark": EngineCommandResult(
                0, "  MARKED  [MARK_OK] SPY x1 marked from a live two-sided book"
            ),
            "options-cancel": EngineCommandResult(0, "cancelled"),
        }

    def run(self, args: list[str], **_: Any) -> EngineCommandResult:
        self.calls.append(list(args))
        return self.results.get(args[0], EngineCommandResult(0, ""))


def reviewer_ready_on_sleep(store: Any) -> Callable[[float], None]:
    """A ``sleep`` that answers the newest handshake/shutdown request instead
    of sleeping -- the reviewer's turn, taken at poll time through the real
    store, echoing the literal token exactly as the live seat does."""

    def sleep(_seconds: float) -> None:
        for handoff in store.list(("pending",), to="reviewer"):
            body = str(handoff.body or "")
            token_match = re.search(r"literal token: (\S+)", body)
            if token_match:
                reply_body = (
                    f"Model: scripted-reviewer\nUTC: {NOW.isoformat()}\n"
                    f"{token_match.group(1)}\nREVIEWER_READY"
                )
            elif "TRADING_DAY_CLOSED" in handoff.title:
                reply_body = "REVIEWER_STOPPED"
            else:
                continue
            store.claim(handoff.id, by="reviewer")
            store.reply(
                handoff.id,
                title=f"reply: {handoff.title}",
                body=reply_body,
                sender="reviewer",
            )

    return sleep


@dataclass
class Harness:
    controller: PaperDayController
    paths: PaperDayPaths
    processes: FakeProcessPort
    engine: FakeEngine
    store: Any
    collab_root: Path
    config: StubConfig = field(default_factory=StubConfig)


def harness(
    tmp_path: Path,
    *,
    broker_up: bool = True,
    reviewer_running: bool = True,
    reviewer_answers: bool = True,
    config: StubConfig | Exception | None = None,
    liveness_timeout: float = 5.0,
) -> Harness:
    """A controller wired to fakes plus a REAL collab store in ``tmp_path``."""
    collab_root = reviewer_support.collab_at(tmp_path)
    paths_module = reviewer_support.load("paths", "CollabPaths")
    store = reviewer_support.load("store", "HandoffStore")(paths_module.at(collab_root))

    processes = FakeProcessPort()
    if reviewer_running:
        processes.add(REVIEWER_CMD)
    engine = FakeEngine()
    paths = PaperDayPaths(state_dir=tmp_path / "state")

    resolved_config = config if config is not None else StubConfig()

    def config_loader() -> Any:
        if isinstance(resolved_config, Exception):
            raise resolved_config
        return resolved_config

    controller = PaperDayController(
        paths=paths,
        processes=processes,
        engine=engine,
        tcp_probe=lambda host, port: broker_up,
        clock=lambda: NOW,
        sleep=(
            reviewer_ready_on_sleep(store)
            if reviewer_answers
            else (lambda _s: None)
        ),
        collab_root=collab_root,
        liveness_timeout=liveness_timeout,
        liveness_poll=0.0,
        consumption_proof=lambda: (True, "stubbed for speed; real proof pinned separately"),
        config_loader=config_loader,
    )
    return Harness(
        controller=controller,
        paths=paths,
        processes=processes,
        engine=engine,
        store=store,
        collab_root=collab_root,
        config=resolved_config if isinstance(resolved_config, StubConfig) else StubConfig(),
    )
