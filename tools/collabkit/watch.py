"""The polling engine behind every watcher script.

ARCHITECTURE.md: "Each side runs a persistent monitor that tails its
``pending/`` directory and surfaces new handoffs in-session." All three watchers
(``watch-for-claude-handoffs.py``, ``watch-for-grok-handoffs.py``,
``watch-all-handoffs.py``) are thin front ends over this module -- they differ
only in which seat they answer to and how many collabs they cover.

Why polling and not inotify/FSEvents/ReadDirectoryChangesW
----------------------------------------------------------
Three OS-specific APIs, none in the stdlib, all with different edge cases around
network mounts and rename-into-directory -- which is *precisely* the event this
kit generates on every transition. A 2-second stat loop over a directory that
holds tens of files costs nothing measurable and behaves identically on Linux,
macOS, Git Bash and a synced drive. The cheap correct thing beats the clever
thing here.

Delivery semantics: **at-least-once**. The seen-set is persisted after each
successful emit, so a crash mid-poll can re-announce a handoff. A duplicate
notification is a minor annoyance; a dropped review request is the failure mode
this whole system exists to prevent, so the trade is made deliberately in that
direction.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import seats
from .atomic import atomic_write_json, read_json
from .frontmatter import parse_file
from .model import Handoff
from .paths import CollabPaths, HomePaths
from .timeutil import iso, utcnow

DEFAULT_INTERVAL = 2.0
MIN_INTERVAL = 0.2
SEEN_LIMIT = 5000


@dataclass
class WatchTarget:
    """One collab this watcher is responsible for."""

    name: str
    paths: CollabPaths

    @classmethod
    def at(cls, root: Path | str, name: str = "") -> "WatchTarget":
        paths = CollabPaths.at(root, name)
        return cls(paths.name, paths)


@dataclass
class WatchEvent:
    """Something new that the seat should see."""

    kind: str  # "handoff" | "message"
    collab: str
    path: Path
    key: str
    handoff: Handoff | None = None
    title: str = ""
    body: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "collab": self.collab,
            "path": str(self.path),
            "key": self.key,
            "title": self.title,
            "body": self.body,
            "handoff": self.handoff.to_json() if self.handoff else None,
            "meta": self.meta,
            "observed": iso(),
        }


class SeenSet:
    """Bounded, crash-safe record of what has already been announced.

    Bounded because this file is written every poll for the lifetime of a
    session; unbounded growth would eventually make each write slower than the
    poll interval it lives inside.
    """

    def __init__(self, path: Path, *, limit: int = SEEN_LIMIT) -> None:
        self.path = Path(path)
        self.limit = limit
        self._entries: dict[str, str] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        data = read_json(self.path, default=None)
        if isinstance(data, dict):
            seen = data.get("seen")
            if isinstance(seen, dict):
                self._entries = {str(k): str(v) for k, v in seen.items()}
            elif isinstance(seen, list):  # tolerate an older/simpler shape
                self._entries = {str(k): "" for k in seen}

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def add(self, key: str) -> None:
        if key not in self._entries:
            self._entries[key] = iso()
            self._dirty = True

    def flush(self) -> None:
        if not self._dirty:
            return
        if len(self._entries) > self.limit:
            # dict preserves insertion order, so this drops the oldest.
            excess = len(self._entries) - self.limit
            for key in list(self._entries)[:excess]:
                del self._entries[key]
        try:
            atomic_write_json(
                self.path, {"updated": iso(), "seen": self._entries}, indent=0
            )
            self._dirty = False
        except OSError:
            # A cache, not state. Losing it costs duplicate announcements.
            self._dirty = False

    def prime(self, keys: Iterable[str]) -> None:
        """Mark existing items as seen without announcing them.

        Used on first start so attaching a watcher to a collab with 40 open
        handoffs does not dump all 40 into the session as if they were new.
        """
        for key in keys:
            self._entries.setdefault(key, iso())
        self._dirty = True


class Watcher:
    """Polls one or more collabs for work addressed to ``seat``."""

    def __init__(
        self,
        targets: Sequence[WatchTarget],
        *,
        seat: str,
        state_path: Path,
        interval: float = DEFAULT_INTERVAL,
        include_messages: bool = True,
        home: HomePaths | None = None,
        on_event: Callable[[WatchEvent], None] | None = None,
        announce_backlog: bool = False,
    ) -> None:
        self.targets = list(targets)
        self.seat = seats.canonical(seat, default=seats.BROADCAST)
        self.interval = max(MIN_INTERVAL, float(interval))
        self.include_messages = include_messages
        self.home = home or HomePaths.discover()
        self.seen = SeenSet(state_path)
        self.on_event = on_event
        self._stop = False
        self._primed = state_path.exists() or announce_backlog

    # -- lifecycle -------------------------------------------------------

    def install_signal_handlers(self) -> None:
        """Exit cleanly on Ctrl-C / SIGTERM so the seen-set is flushed."""
        def handle(_signum: int, _frame: Any) -> None:
            self._stop = True

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass

    def stop(self) -> None:
        self._stop = True

    def run(self, *, once: bool = False, max_polls: int | None = None) -> int:
        """Poll until stopped. Returns the number of events emitted."""
        emitted = 0
        polls = 0
        while not self._stop:
            try:
                for event in self.poll():
                    emitted += 1
                    if self.on_event:
                        try:
                            self.on_event(event)
                        except Exception:
                            # A failing notifier must not kill a watcher that
                            # has been running for hours. The event is already
                            # recorded as seen; losing one render is survivable,
                            # losing the watcher is not.
                            pass
            except OSError:
                # A collab directory can be deleted underneath a long-running
                # watcher. Keep going; the next poll will simply find nothing.
                pass
            polls += 1
            if once or (max_polls is not None and polls >= max_polls):
                break
            self._sleep(self.interval)
        self.seen.flush()
        return emitted

    def _sleep(self, seconds: float) -> None:
        """Sleep in slices so Ctrl-C is responsive at any interval."""
        deadline = time.monotonic() + seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    # -- scanning --------------------------------------------------------

    def _ensure_primed(self) -> None:
        """Adopt the existing backlog silently on a cold start.

        Attaching a watcher to a collab with forty open handoffs must not dump
        all forty into the session as if they had just arrived. Done here rather
        than in ``run()`` so that calling ``poll()`` directly -- which tests and
        embedders do -- behaves identically.
        """
        if self._primed:
            return
        self._primed = True
        self.seen.prime(key for key, _ in self._scan())
        self.seen.flush()

    def poll(self) -> list[WatchEvent]:
        """One scan. Returns newly-seen events, most urgent first."""
        self._ensure_primed()
        events: list[WatchEvent] = []
        for key, produce in self._scan():
            if key in self.seen:
                continue
            event = produce()
            if event is None:
                # Unparseable or mid-write. Deliberately NOT marked seen, so it
                # is retried on the next poll once the writer finishes.
                continue
            events.append(event)

        events.sort(key=_event_sort_key)
        for event in events:
            self.seen.add(event.key)
        self.seen.flush()
        return events

    def _scan(self) -> list[tuple[str, Callable[[], WatchEvent | None]]]:
        found: list[tuple[str, Callable[[], WatchEvent | None]]] = []
        for target in self.targets:
            found.extend(self._scan_handoffs(target))
            if self.include_messages:
                found.extend(self._scan_messages(target))
        return found

    def _scan_handoffs(
        self, target: WatchTarget
    ) -> list[tuple[str, Callable[[], WatchEvent | None]]]:
        pending = target.paths.pending
        if not pending.is_dir():
            return []
        out: list[tuple[str, Callable[[], WatchEvent | None]]] = []
        try:
            names = sorted(pending.glob("*.md"))
        except OSError:
            return []
        for path in names:
            if path.name.startswith("."):
                continue  # atomic-write temp file
            key = f"handoff:{target.name}:{path.stem}"
            out.append((key, _handoff_loader(self, target, path, key)))
        return out

    def _scan_messages(
        self, target: WatchTarget
    ) -> list[tuple[str, Callable[[], WatchEvent | None]]]:
        """Messages from the phone, delivered per project by the bridge."""
        if self.seat not in (seats.BUILDER, seats.BROADCAST):
            # Only the seat talking to the human surfaces inbound chat; the
            # reviewer answering the user's messages would be a second voice.
            return []
        try:
            directory = self.home.inbox_for(target.name)
        except Exception:
            return []
        if not directory.is_dir():
            return []
        out: list[tuple[str, Callable[[], WatchEvent | None]]] = []
        try:
            names = sorted(directory.glob("from-user-*.md"))
        except OSError:
            return []
        for path in names:
            if path.name.startswith("."):
                continue
            key = f"message:{target.name}:{path.name}"
            out.append((key, _message_loader(target, path, key)))
        return out


def _handoff_loader(
    watcher: "Watcher", target: WatchTarget, path: Path, key: str
) -> Callable[[], WatchEvent | None]:
    def load() -> WatchEvent | None:
        try:
            handoff = Handoff.load(path, status="pending")
        except Exception:
            return None  # mid-write or corrupt; retried next poll
        if not handoff.id:
            return None
        if not seats.matches(handoff.to, watcher.seat):
            # Addressed to the other seat. Mark it seen anyway so we do not
            # re-stat and re-parse it every two seconds forever.
            watcher.seen.add(key)
            return None
        return WatchEvent(
            kind="handoff",
            collab=target.name,
            path=path,
            key=key,
            handoff=handoff,
            title=handoff.title,
            body=handoff.body,
        )

    return load


def _message_loader(
    target: WatchTarget, path: Path, key: str
) -> Callable[[], WatchEvent | None]:
    def load() -> WatchEvent | None:
        try:
            meta, body = parse_file(path)
        except Exception:
            return None
        text = (body or "").strip()
        if not text:
            return None
        return WatchEvent(
            kind="message",
            collab=target.name,
            path=path,
            key=key,
            title=f"message from {meta.get('from', 'you')}",
            body=text,
            meta={str(k): v for k, v in meta.items()},
        )

    return load


def _event_sort_key(event: WatchEvent) -> tuple[int, int, str]:
    """Human messages first, then handoffs by priority, then oldest first.

    The human interrupting takes precedence over any agent-to-agent traffic --
    that is the whole point of the phone bridge.
    """
    if event.kind == "message":
        return (0, 0, event.key)
    rank = event.handoff.priority_rank if event.handoff else 1
    return (1, -rank, event.handoff.created if event.handoff else event.key)


# --------------------------------------------------------------------------
# target discovery
# --------------------------------------------------------------------------


def targets_from_root(root: Path | str, name: str = "") -> list[WatchTarget]:
    return [WatchTarget.at(root, name)]


def targets_from_registry(home: HomePaths | None = None, *, only_existing: bool = True):
    """Every registered collab -- the ``watch-all-handoffs.py`` fan-out."""
    from .registry import Registry

    registry = Registry(home)
    out: list[WatchTarget] = []
    for entry in registry:
        if only_existing and not entry.exists:
            continue
        out.append(WatchTarget(entry.name, entry.paths))
    return out


def default_state_path(seat: str, scope: str, home: HomePaths | None = None) -> Path:
    """Where a watcher keeps its cursor.

    Keyed by seat *and* scope so a builder watcher and a reviewer watcher on the
    same collab -- and the same seat across two collabs -- never share a
    seen-set. Sharing one would let one watcher's announcement suppress
    another's.
    """
    home = home or HomePaths.discover()
    from .slug import slugify

    stem = f"{slugify(seat, fallback='seat')}-{slugify(scope, fallback='all')}"
    return home.state / f"watch-{stem}.json"


def uptime_banner(seat: str, targets: Sequence[WatchTarget], interval: float) -> str:
    """One-line startup summary, so a silent watcher is distinguishable from a
    dead one."""
    names = ", ".join(target.name for target in targets) or "(none)"
    started = utcnow().strftime("%H:%M:%S")
    return (
        f"watching {len(targets)} collab(s) [{names}] as '{seats.label(seat)}' "
        f"every {interval:g}s -- started {started}Z"
    )
