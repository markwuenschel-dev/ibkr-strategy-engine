"""Shared front end for the three watcher scripts.

``watch-for-claude-handoffs.py``, ``watch-for-grok-handoffs.py`` and
``watch-all-handoffs.py`` differ in exactly three ways: which seat they answer
to, whether they cover one collab or every registered one, and whether they fan
out to notifications. Everything else -- argument parsing, rendering, signal
handling, exit codes -- lives here so the three stay in lockstep.

Rendering is tuned for the actual consumer: an agent session tailing the
process. Each event is a self-contained block that states what arrived, from
whom, and the exact command to act on it. An agent should never have to guess
the next call.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import console, notify, seats
from .errors import EXIT_ERROR, EXIT_OK, EXIT_USAGE, CollabKitError, UsageError
from .paths import CollabPaths, HomePaths, handoff_root_env
from .watch import (
    DEFAULT_INTERVAL,
    WatchEvent,
    Watcher,
    default_state_path,
    targets_from_registry,
    targets_from_root,
    uptime_banner,
)

MAX_BODY_LINES = 40
MAX_BODY_CHARS = 4000


def build_parser(prog: str, description: str, *, single: bool) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    if single:
        parser.add_argument(
            "--root",
            help="collab root to watch (default: $HANDOFF_ROOT)",
        )
        parser.add_argument("--name", default="", help="label to show for this collab")
    else:
        parser.add_argument(
            "--only",
            action="append",
            default=[],
            help="limit to these collabs (repeatable)",
        )
    parser.add_argument(
        "--seat",
        default="",
        help="override which seat's mail to surface (builder|reviewer|all)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"seconds between scans (default {DEFAULT_INTERVAL:g})",
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON object per event")
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    parser.add_argument(
        "--max-polls", type=int, default=None, help="stop after N scans (for tests)"
    )
    parser.add_argument(
        "--announce-backlog",
        action="store_true",
        help="also surface handoffs that were already pending at startup",
    )
    parser.add_argument("--notify", action="store_true", help="fire desktop notifications")
    parser.add_argument("--phone", action="store_true", help="also queue to the Telegram outbox")
    parser.add_argument("--no-messages", action="store_true", help="ignore inbound phone messages")
    parser.add_argument("--quiet", action="store_true", help="suppress the startup banner")
    parser.add_argument(
        "--state",
        help="path to this watcher's seen-set (default: under $COLLAB_HOME/logs/state)",
    )
    return parser


def run(
    argv: Sequence[str] | None,
    *,
    prog: str,
    description: str,
    seat: str,
    single: bool,
    scope_label: str,
    default_notify: bool = False,
) -> int:
    """Parse args, build a watcher, run it. Returns a process exit code."""
    parser = build_parser(prog, description, single=single)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        home = HomePaths.discover()
        active_seat = seats.canonical(args.seat) or seat

        if single:
            root = Path(args.root).expanduser() if args.root else handoff_root_env()
            if root is None:
                raise UsageError(
                    "no collab selected",
                    hint=(
                        "run as: HANDOFF_ROOT=$COLLAB_HOME/<name> "
                        f"python3 {prog}   (or pass --root <path>)"
                    ),
                )
            CollabPaths.at(root).require()
            targets = targets_from_root(root, args.name)
            scope = targets[0].name
        else:
            targets = targets_from_registry(home)
            only = {name for name in getattr(args, "only", [])}
            if only:
                targets = [target for target in targets if target.name in only]
                missing = only - {target.name for target in targets}
                if missing:
                    console.warn(f"not registered (or missing on disk): {', '.join(sorted(missing))}")
            scope = scope_label

        if not targets:
            console.warn("nothing to watch -- no collabs registered")
            console.hint("create one with: newproject <name> --repo <git-url>")
            return EXIT_OK

        state_path = (
            Path(args.state).expanduser()
            if args.state
            else default_state_path(active_seat, scope, home)
        )

        emitter = _Emitter(
            as_json=args.json,
            desktop=args.notify or default_notify,
            phone=args.phone,
            home=home,
            show_collab=not single,
        )
        watcher = Watcher(
            targets,
            seat=active_seat,
            state_path=state_path,
            interval=args.interval,
            include_messages=not args.no_messages,
            home=home,
            on_event=emitter,
            announce_backlog=args.announce_backlog,
        )
        watcher.install_signal_handlers()

        if not args.quiet and not args.json:
            console.info(uptime_banner(active_seat, targets, watcher.interval))
            console.info(f"state: {state_path}")

        count = watcher.run(once=args.once, max_polls=args.max_polls)
        if not args.json and not args.quiet:
            console.info(f"stopped after surfacing {count} item(s)")
        return EXIT_OK

    except UsageError as exc:
        console.error(exc.message)
        if exc.hint:
            console.hint(exc.hint)
        return EXIT_USAGE
    except CollabKitError as exc:
        console.error(exc.message)
        if exc.hint:
            console.hint(exc.hint)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_OK
    except Exception as exc:  # pragma: no cover
        console.error(f"watcher failed: {exc.__class__.__name__}: {exc}")
        return EXIT_ERROR


class _Emitter:
    """Renders one event. Kept as a class so state (json mode) stays explicit."""

    def __init__(
        self,
        *,
        as_json: bool,
        desktop: bool,
        phone: bool,
        home: HomePaths,
        show_collab: bool,
    ) -> None:
        self.as_json = as_json
        self.desktop = desktop
        self.phone = phone
        self.home = home
        self.show_collab = show_collab

    def __call__(self, event: WatchEvent) -> None:
        if self.as_json:
            console.json_out(event.to_json())
        else:
            self._render(event)
        if self.desktop or self.phone:
            urgent = bool(event.handoff and event.handoff.priority == "urgent")
            notify.announce(
                self._notification_title(event),
                event.title or event.body[:120],
                project=event.collab,
                home=self.home,
                desktop=self.desktop,
                phone=self.phone,
                urgent=urgent,
            )

    def _notification_title(self, event: WatchEvent) -> str:
        if event.kind == "message":
            return f"[{event.collab}] message from you"
        priority = event.handoff.priority if event.handoff else "normal"
        return f"[{event.collab}] {priority} handoff"

    def _render(self, event: WatchEvent) -> None:
        tag = event.collab if self.show_collab else ""
        if event.kind == "message":
            console.rule(f"MESSAGE from you{f' {console.DOT} {tag}' if tag else ''}")
            console.out(_clip(event.body))
            console.info(f"(file: {event.path})")
            console.rule()
            return

        handoff = event.handoff
        if handoff is None:  # pragma: no cover - defensive
            return
        header = f"HANDOFF {console.DOT} {handoff.priority.upper()}"
        if tag:
            header += f" {console.DOT} {tag}"
        console.rule(header)
        console.out(
            f"from     {seats.label(handoff.sender)} {console.ARROW} {seats.label(handoff.to)}"
        )
        console.out(f"title    {handoff.title}")
        console.out(f"id       {handoff.id}")
        if handoff.tags:
            console.out(f"tags     {', '.join(handoff.tags)}")
        if handoff.thread:
            console.out(f"thread   {handoff.thread}")
        body = _clip(handoff.body)
        if body.strip():
            console.out("")
            console.out(body)
        console.out("")
        # The exact next command, so the agent never has to guess the syntax.
        # Single-collab watchers run with $HANDOFF_ROOT set, where the working
        # CLI is `collab-handoff` (no name argument); the cross-collab watcher
        # has no such scope, so it must name the collab. Printing the wrong one
        # would hand the agent a command that fails on first use.
        prefix = f"handoff {event.collab}" if self.show_collab else "collab-handoff"
        console.out(f"  claim:  {prefix} claim {handoff.id}")
        console.out(f'  reply:  {prefix} reply {handoff.id} --title "..." --file reply.md')
        console.rule()


def _clip(text: str) -> str:
    """Bound a body so one enormous handoff cannot flood a session's context."""
    body = (text or "").rstrip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n... (truncated -- read the file for the rest)"
    lines = body.split("\n")
    if len(lines) > MAX_BODY_LINES:
        remaining = len(lines) - MAX_BODY_LINES
        lines = lines[:MAX_BODY_LINES] + [f"... ({remaining} more lines -- read the file)"]
    return "\n".join(lines)
