"""The ``handoff`` / ``collab-handoff`` command line.

Two executables, one implementation, differing only in how they find the collab
they operate on:

``handoff <name> <cmd>``   registry-aware. Resolves ``<name>`` through
                           ``collabs.json`` and also owns the cross-project
                           commands (``status``, ``new``, ``register``, ...).
``collab-handoff <cmd>``   root-scoped via ``$HANDOFF_ROOT``. No registry, no
                           name argument. This is what the watchers and any
                           in-repo automation call, because it works on a collab
                           that was never registered.

The grammar ``handoff <name> <cmd>`` puts a *value* where argparse expects a
subcommand, so dispatch is done by hand: peek at the first token, and if it is
not a known global command, treat it as a collab name. This is a deliberate
trade -- the CLI shape in ARCHITECTURE.md is the contract, and bending it to
suit argparse would be the tail wagging the dog.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__, console, seats
from .errors import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    CollabKitError,
    NotFoundError,
    UsageError,
)
from .model import PRIORITIES, Handoff
from .paths import STATES, CollabPaths, HomePaths, handoff_root_env
from .registry import Registry
from .slug import validate_name
from .store import HandoffStore
from .timeutil import human_age

GLOBAL_COMMANDS = (
    "status",
    "collabs",
    "new",
    "register",
    "unregister",
    "prune",
    "doctor",
    "version",
    "help",
)

COLLAB_COMMANDS = (
    "create",
    "list",
    "claim",
    "show",
    "done",
    "reply",
    "archive",
    "path",
    "counts",
    "log",
)

_ALIASES = {
    "ls": "list",
    "complete": "done",
    "close": "done",
    "finish": "done",
    "open": "show",
    "cat": "show",
    "take": "claim",
    "new-handoff": "create",
    "send": "create",
    "-h": "help",
    "--help": "help",
    "-V": "version",
    "--version": "version",
}


# ==========================================================================
# usage
# ==========================================================================

_USAGE_REGISTRY = """\
handoff -- file-based handoffs between a builder and an independent reviewer

  handoff <collab> <command> [options]      operate on one collab
  handoff <global-command> [options]        operate across all collabs

Per-collab commands
  create   --to <seat> --title "..." [--priority low|normal|high|urgent]
           [--file <path>|--body "..."|-] [--tag <t>]... [--thread <id>]
  list     [--pending|--claimed|--done|--archive|--all] [--to <seat>]
           [--from <seat>] [--priority <p>] [--tag <t>] [--json]
  claim    <id> [--as <seat>]
  show     <id> [--body-only] [--json]
  done     <id> [--note "..."] [--as <seat>]
  reply    <id> --title "..." [--file <path>|--body "..."|-] [--keep-open]
  archive  [<id> | --all-done]
  path     [--repo|--pending]                print a path for scripting
  counts                                     one line per state
  log      [-n <count>]                      recent events

Global commands
  status       [--json] [--stale <hours>]    what is outstanding, everywhere
  collabs      [--json]                      registered collabs
  new          <name> [--root <path>] [--reviewer claude|grok]
  register     <name> --root <path> [--force]
  unregister   <name>
  prune                                      drop registry entries with no collab
  doctor       [--json]                      health check
  version

Ids may be abbreviated to any unambiguous prefix.
Environment: COLLAB_HOME (data root), HANDOFF_ROOT (single-collab scope).
"""

_USAGE_ROOT = """\
collab-handoff -- single-collab handoff CLI (scoped by $HANDOFF_ROOT)

  HANDOFF_ROOT=$COLLAB_HOME/<name> collab-handoff <command> [options]
  collab-handoff --root <path> <command> [options]

Commands
  create   --to <seat> --title "..." [--priority low|normal|high|urgent]
           [--file <path>|--body "..."|-] [--tag <t>]... [--thread <id>]
  list     [--pending|--claimed|--done|--archive|--all] [--to <seat>] [--json]
  claim    <id> [--as <seat>]
  show     <id> [--body-only] [--json]
  done     <id> [--note "..."] [--as <seat>]
  reply    <id> --title "..." [--file <path>|--body "..."|-] [--keep-open]
  archive  [<id> | --all-done]
  path     [--repo|--pending]
  counts
  log      [-n <count>]

Ids may be abbreviated to any unambiguous prefix.
"""


# ==========================================================================
# entry points
# ==========================================================================


def main(argv: Sequence[str] | None = None) -> int:
    """``handoff`` -- registry-aware front door."""
    return _run(list(sys.argv[1:] if argv is None else argv), registry_mode=True)


def main_root(argv: Sequence[str] | None = None) -> int:
    """``collab-handoff`` -- ``$HANDOFF_ROOT``-scoped."""
    return _run(list(sys.argv[1:] if argv is None else argv), registry_mode=False)


def _run(argv: list[str], *, registry_mode: bool) -> int:
    usage = _USAGE_REGISTRY if registry_mode else _USAGE_ROOT
    try:
        if not argv:
            console.out(usage.rstrip())
            return EXIT_USAGE

        head = _ALIASES.get(argv[0], argv[0])

        if head == "help":
            console.out(usage.rstrip())
            return EXIT_OK
        if head == "version":
            console.out(f"collab-kit {__version__}")
            return EXIT_OK

        if registry_mode and head in GLOBAL_COMMANDS:
            return _dispatch_global(head, argv[1:])

        if registry_mode:
            name = argv[0]
            if name.startswith("-"):
                raise UsageError(
                    f"unknown option {name!r}",
                    hint="usage: handoff <collab> <command> ...  (try 'handoff help')",
                )
            validate_name(name)
            rest = argv[1:]
            if not rest:
                # `handoff <name>` with no verb is the common typo; show that
                # collab's queue rather than an error, which is what the caller
                # almost always wanted.
                rest = ["list"]
            return _dispatch_collab(_ALIASES.get(rest[0], rest[0]), rest[1:], name=name)

        return _dispatch_collab(head, argv[1:], name=None)

    except CollabKitError as exc:
        console.error(exc.message)
        if exc.hint:
            console.hint(exc.hint)
        return exc.exit_code
    except BrokenPipeError:  # pragma: no cover - `| head`
        return EXIT_OK
    except KeyboardInterrupt:  # pragma: no cover
        console.info("interrupted")
        return 130
    except Exception as exc:  # pragma: no cover - unexpected
        console.error(f"unexpected failure: {exc.__class__.__name__}: {exc}")
        if os.environ.get("COLLAB_KIT_TRACE"):
            raise
        console.hint("re-run with COLLAB_KIT_TRACE=1 for a traceback")
        return EXIT_ERROR


# ==========================================================================
# collab resolution
# ==========================================================================


def _resolve_store(name: str | None, *, root_override: str | None = None) -> HandoffStore:
    """Find the collab a command should operate on.

    Precedence: an explicit ``--root``, then ``$HANDOFF_ROOT``, then the
    registry. Explicit beats ambient beats configured -- the usual ordering, and
    the one that makes a watcher's env var predictable.
    """
    if root_override:
        paths = CollabPaths.at(root_override).require()
        return HandoffStore(paths, collab=paths.name)

    if name:
        entry = Registry().require(name)
        paths = CollabPaths.at(entry.root, entry.name)
        if not paths.exists():
            raise NotFoundError(
                f"collab {name!r} is registered at {entry.root} but has no handoffs/ there",
                hint="the directory may have moved; re-run 'handoff register' with --root",
            )
        return HandoffStore(paths, collab=entry.name)

    env_root = handoff_root_env()
    if env_root is None:
        raise UsageError(
            "no collab selected",
            hint="set HANDOFF_ROOT=$COLLAB_HOME/<name>, or pass --root <path>",
        )
    paths = CollabPaths.at(env_root).require()
    return HandoffStore(paths, collab=paths.name)


# ==========================================================================
# per-collab commands
# ==========================================================================


def _dispatch_collab(command: str, argv: list[str], *, name: str | None) -> int:
    if command not in COLLAB_COMMANDS:
        raise UsageError(
            f"unknown command {command!r}",
            hint=f"expected one of: {', '.join(COLLAB_COMMANDS)}",
        )
    handler: Callable[[list[str], str | None], int] = globals()[f"_cmd_{command}"]
    return handler(argv, name)


def _base_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description, add_help=True)
    parser.add_argument(
        "--root",
        help="operate on the collab at this path (overrides $HANDOFF_ROOT and the registry)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def _cmd_create(argv: list[str], name: str | None) -> int:
    parser = _base_parser("handoff create", "Create a handoff in pending/.")
    parser.add_argument("--to", required=True, help="recipient seat (builder|reviewer|...)")
    parser.add_argument("--from", dest="sender", default="", help="sender seat")
    parser.add_argument("--title", required=True)
    parser.add_argument("--priority", default="normal", choices=list(PRIORITIES))
    parser.add_argument("--file", help="read the body from this file ('-' for stdin)")
    parser.add_argument("--body", help="body text given inline")
    parser.add_argument("--tag", action="append", default=[], dest="tags")
    parser.add_argument("--thread", help="id of the handoff this continues")
    parser.add_argument("body_stdin", nargs="?", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    store = _resolve_store(name, root_override=args.root)
    handoff = store.create(
        to=args.to,
        sender=args.sender or seats.counterpart(args.to),
        title=args.title,
        body=_read_body(args),
        priority=args.priority,
        tags=args.tags,
        thread=args.thread,
    )
    if args.json:
        console.json_out(handoff.to_json())
    else:
        console.ok(f"created {handoff.id}")
        console.info(f"  to {seats.label(handoff.to)} - {handoff.priority} - {handoff.title}")
        console.out(handoff.id)
    return EXIT_OK


def _cmd_list(argv: list[str], name: str | None) -> int:
    parser = _base_parser("handoff list", "List handoffs.")
    for state in STATES:
        parser.add_argument(f"--{state}", action="store_true", help=f"include {state}/")
    parser.add_argument("--all", action="store_true", help="every state")
    parser.add_argument("--to", help="only handoffs addressed to this seat")
    parser.add_argument("--from", dest="sender", help="only handoffs from this seat")
    parser.add_argument("--priority", choices=list(PRIORITIES))
    parser.add_argument("--tag")
    parser.add_argument("--ids", action="store_true", help="print bare ids, one per line")
    args = parser.parse_args(argv)

    selected = [state for state in STATES if getattr(args, state)]
    if args.all:
        selected = list(STATES)
    if not selected:
        selected = ["pending"]

    store = _resolve_store(name, root_override=args.root)
    found = store.list(
        selected, to=args.to, sender=args.sender, priority=args.priority, tag=args.tag
    )

    if args.json:
        console.json_out([handoff.to_json() for handoff in found])
        return EXIT_OK
    if args.ids:
        for handoff in found:
            console.out(handoff.id)
        return EXIT_OK
    if not found:
        console.info(f"no handoffs in {'/'.join(selected)}")
        return EXIT_OK

    show_state = len(selected) > 1
    for handoff in found:
        prefix = f"[{handoff.status:<7}] " if show_state else ""
        console.out(f"{prefix}{handoff.id}")
        console.out(f"          {handoff.summary()}")
    console.info(f"{len(found)} handoff(s) in {', '.join(selected)}")
    return EXIT_OK


def _cmd_claim(argv: list[str], name: str | None) -> int:
    parser = _base_parser("handoff claim", "Claim a pending handoff (atomic).")
    parser.add_argument("id")
    parser.add_argument("--as", dest="seat", default="", help="seat doing the claiming")
    args = parser.parse_args(argv)

    store = _resolve_store(name, root_override=args.root)
    handoff = store.claim(args.id, by=args.seat)
    if args.json:
        console.json_out(handoff.to_json())
    else:
        console.ok(f"claimed {handoff.id} as {seats.label(handoff.claimed_by or '')}")
        _print_handoff(handoff)
    return EXIT_OK


def _cmd_show(argv: list[str], name: str | None) -> int:
    parser = _base_parser("handoff show", "Print a handoff.")
    parser.add_argument("id")
    parser.add_argument("--body-only", action="store_true")
    args = parser.parse_args(argv)

    store = _resolve_store(name, root_override=args.root)
    handoff = store.find(args.id)
    if args.json:
        console.json_out(handoff.to_json())
    elif args.body_only:
        console.out(handoff.body.rstrip())
    else:
        _print_handoff(handoff, full=True)
    return EXIT_OK


def _cmd_done(argv: list[str], name: str | None) -> int:
    parser = _base_parser("handoff done", "Mark a handoff done.")
    parser.add_argument("id")
    parser.add_argument("--note", default="", help="one-line outcome recorded on the handoff")
    parser.add_argument("--as", dest="seat", default="")
    args = parser.parse_args(argv)

    store = _resolve_store(name, root_override=args.root)
    handoff = store.complete(args.id, note=args.note, by=args.seat)
    if args.json:
        console.json_out(handoff.to_json())
    else:
        console.ok(f"done {handoff.id}")
    return EXIT_OK


def _cmd_reply(argv: list[str], name: str | None) -> int:
    parser = _base_parser("handoff reply", "Answer a handoff and close it.")
    parser.add_argument("id")
    parser.add_argument("--title", required=True)
    parser.add_argument("--file", help="body file ('-' for stdin)")
    parser.add_argument("--body")
    parser.add_argument("--priority", choices=list(PRIORITIES))
    parser.add_argument("--tag", action="append", default=None, dest="tags")
    parser.add_argument("--as", dest="seat", default="")
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="do not mark the parent done (default is to close it)",
    )
    args = parser.parse_args(argv)

    store = _resolve_store(name, root_override=args.root)
    reply, closed = store.reply(
        args.id,
        title=args.title,
        body=_read_body(args),
        sender=args.seat,
        priority=args.priority,
        tags=args.tags,
        close_parent=not args.keep_open,
    )
    if args.json:
        console.json_out(
            {"reply": reply.to_json(), "closed": closed.to_json() if closed else None}
        )
    else:
        console.ok(f"replied {reply.id} (thread {reply.thread})")
        if closed:
            console.info(f"  closed parent {closed.id}")
        console.out(reply.id)
    return EXIT_OK


def _cmd_archive(argv: list[str], name: str | None) -> int:
    parser = _base_parser("handoff archive", "Move done handoffs into archive/.")
    parser.add_argument("id", nargs="?")
    parser.add_argument("--all-done", action="store_true")
    args = parser.parse_args(argv)

    if not args.id and not args.all_done:
        raise UsageError("give an id, or --all-done to sweep every finished handoff")

    store = _resolve_store(name, root_override=args.root)
    if args.all_done:
        archived = store.archive_all_done()
        if args.json:
            console.json_out([handoff.id for handoff in archived])
        else:
            console.ok(f"archived {len(archived)} handoff(s)")
        return EXIT_OK

    handoff = store.archive(args.id)
    if args.json:
        console.json_out(handoff.to_json())
    else:
        console.ok(f"archived {handoff.id}")
    return EXIT_OK


def _cmd_path(argv: list[str], name: str | None) -> int:
    parser = _base_parser("handoff path", "Print a path, for scripting.")
    parser.add_argument("--repo", action="store_true")
    parser.add_argument("--pending", action="store_true")
    parser.add_argument("--logs", action="store_true")
    args = parser.parse_args(argv)

    store = _resolve_store(name, root_override=args.root)
    paths = store.paths
    target = paths.root
    if args.repo:
        target = paths.repo
    elif args.pending:
        target = paths.pending
    elif args.logs:
        target = paths.logs
    if args.json:
        console.json_out({"path": str(target)})
    else:
        console.out(str(target))
    return EXIT_OK


def _cmd_counts(argv: list[str], name: str | None) -> int:
    parser = _base_parser("handoff counts", "Count handoffs per state.")
    args = parser.parse_args(argv)

    store = _resolve_store(name, root_override=args.root)
    counts = store.counts()
    if args.json:
        console.json_out(counts)
    else:
        for state in STATES:
            console.out(f"{state:<8} {counts[state]}")
    return EXIT_OK


def _cmd_log(argv: list[str], name: str | None) -> int:
    parser = _base_parser("handoff log", "Recent lifecycle events.")
    parser.add_argument("-n", "--count", type=int, default=20)
    args = parser.parse_args(argv)

    store = _resolve_store(name, root_override=args.root)
    records = store.events.tail(max(1, args.count))
    if args.json:
        console.json_out(records)
        return EXIT_OK
    if not records:
        console.info("no events recorded yet")
        return EXIT_OK
    for record in records:
        subject = record.get("id", "")
        extra = " ".join(
            f"{key}={value}"
            for key, value in sorted(record.items())
            if key not in ("ts", "event", "id")
        )
        console.out(f"{record.get('ts', '?'):<21} {record.get('event', '?'):<8} {subject} {extra}".rstrip())
    return EXIT_OK


# ==========================================================================
# global commands
# ==========================================================================


def _dispatch_global(command: str, argv: list[str]) -> int:
    handler: Callable[[list[str]], int] = globals()[f"_g_{command}"]
    return handler(argv)


def _g_collabs(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="handoff collabs")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    entries = list(Registry())
    if args.json:
        console.json_out(
            [
                {
                    "name": entry.name,
                    "root": str(entry.root),
                    "repo": entry.repo,
                    "reviewer": entry.reviewer,
                    "created": entry.created,
                    "exists": entry.exists,
                }
                for entry in entries
            ]
        )
        return EXIT_OK
    if not entries:
        console.info("no collabs registered -- try 'handoff new <name>'")
        return EXIT_OK
    for entry in entries:
        flag = "" if entry.exists else console.color("  [missing]", "red")
        console.out(f"{entry.name:<20} {entry.root}{flag}")
    return EXIT_OK


def _g_status(argv: list[str]) -> int:
    """Cross-project overview: what is outstanding, and where."""
    parser = argparse.ArgumentParser(prog="handoff status")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--name", help="limit to one collab")
    parser.add_argument(
        "--stale",
        type=float,
        default=24.0,
        help="flag pending handoffs older than this many hours (default 24)",
    )
    args = parser.parse_args(argv)

    registry = Registry()
    entries = [registry.require(args.name)] if args.name else list(registry)
    report: list[dict[str, Any]] = []
    stale_seconds = max(0.0, args.stale) * 3600.0

    for entry in entries:
        if not entry.exists:
            report.append({"collab": entry.name, "root": str(entry.root), "missing": True})
            continue
        store = HandoffStore(CollabPaths.at(entry.root, entry.name), collab=entry.name)
        pending = store.list(("pending",))
        counts = store.counts()
        stale = [
            handoff
            for handoff in pending
            if (handoff.to_json().get("age_seconds") or 0) >= stale_seconds
        ]
        report.append(
            {
                "collab": entry.name,
                "root": str(entry.root),
                "missing": False,
                "counts": counts,
                "stale": len(stale),
                "waiting_on": sorted({seats.label(handoff.to) for handoff in pending}),
                "oldest": pending[0].to_json() if pending else None,
                "pending": [handoff.to_json() for handoff in pending[:5]],
            }
        )

    if args.json:
        console.json_out(report)
        return EXIT_OK

    if not report:
        console.info("no collabs registered -- try 'handoff new <name>'")
        return EXIT_OK

    total_pending = 0
    for item in report:
        if item.get("missing"):
            console.out(f"{item['collab']:<18} {console.color('MISSING', 'red')}  {item['root']}")
            continue
        counts = item["counts"]
        total_pending += counts["pending"]
        headline = (
            f"{item['collab']:<18} "
            f"pending {counts['pending']:<3} claimed {counts['claimed']:<3} "
            f"done {counts['done']:<3}"
        )
        if counts["pending"]:
            headline += f"  -> waiting on {', '.join(item['waiting_on'])}"
        if item["stale"]:
            headline += console.color(f"  [{item['stale']} stale]", "yellow")
        console.out(headline)
        oldest = item.get("oldest")
        if oldest:
            age = human_age(oldest.get("age_seconds"))
            console.out(f"{'':<18}   oldest {age:>4} - {oldest.get('title', '')[:52]}")
    console.info(f"{total_pending} pending across {len(report)} collab(s)")
    return EXIT_OK


def _g_new(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="handoff new")
    parser.add_argument("name")
    parser.add_argument("--root", help="where to create it (default $COLLAB_HOME/<name>)")
    parser.add_argument("--reviewer", default="", help="which agent takes the reviewer seat")
    parser.add_argument("--repo", default="", help="record the target repo url")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    name = validate_name(args.name)
    home = HomePaths.discover().ensure()
    root = Path(args.root).expanduser().resolve() if args.root else home.collab_root(name)

    paths = CollabPaths.at(root, name)
    existed = paths.exists()
    paths.ensure()
    entry = Registry(home).register(
        name, root, repo=args.repo, reviewer=args.reviewer, force=True
    )

    if args.json:
        console.json_out({"name": name, "root": str(root), "created": not existed})
        return EXIT_OK
    console.ok(f"{'registered existing' if existed else 'scaffolded'} collab {name!r} at {root}")
    console.info("  next: newproject renders PROTOCOL.md/KICKOFF.md; this only makes the queues")
    console.out(str(entry.root))
    return EXIT_OK


def _g_register(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="handoff register")
    parser.add_argument("name")
    parser.add_argument("--root", required=True)
    parser.add_argument("--repo", default="")
    parser.add_argument("--reviewer", default="")
    parser.add_argument("--force", action="store_true", help="repoint an existing name")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if not CollabPaths.at(root).exists():
        raise NotFoundError(
            f"{root} does not look like a collab (no handoffs/ directory)",
            hint="run 'handoff new <name> --root <path>' to scaffold one first",
        )
    entry = Registry().register(
        args.name, root, repo=args.repo, reviewer=args.reviewer, force=args.force
    )
    if args.json:
        console.json_out({"name": entry.name, "root": str(entry.root)})
    else:
        console.ok(f"registered {entry.name} -> {entry.root}")
    return EXIT_OK


def _g_unregister(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="handoff unregister")
    parser.add_argument("name")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    entry = Registry().unregister(args.name)
    if args.json:
        console.json_out({"name": entry.name, "root": str(entry.root)})
    else:
        console.ok(f"unregistered {entry.name} (files at {entry.root} were left alone)")
    return EXIT_OK


def _g_prune(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="handoff prune")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    removed = Registry().prune()
    if args.json:
        console.json_out({"removed": removed})
    elif removed:
        console.ok(f"pruned {len(removed)}: {', '.join(removed)}")
    else:
        console.info("nothing to prune -- every registered collab exists")
    return EXIT_OK


def _g_doctor(argv: list[str]) -> int:
    """Health check. Exit non-zero if anything is actually broken.

    Distinguishes problems from observations: a missing optional Telegram token
    is fine, a registered collab whose directory vanished is not.
    """
    parser = argparse.ArgumentParser(prog="handoff doctor")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from .paths import kit_dir

    home = HomePaths.discover()
    checks: list[dict[str, Any]] = []

    def check(label: str, ok_: bool, detail: str = "", *, fatal: bool = True) -> None:
        checks.append({"check": label, "ok": bool(ok_), "detail": detail, "fatal": fatal})

    check("python", sys.version_info >= (3, 14), f"{sys.version.split()[0]}")
    check("kit dir", kit_dir().is_dir(), str(kit_dir()))
    check("collab home", home.root.is_dir(), str(home.root))
    check(
        "collab home writable",
        os.access(home.root, os.W_OK) if home.root.is_dir() else False,
        str(home.root),
    )

    try:
        entries = list(Registry(home))
        check("registry", True, f"{len(entries)} collab(s) at {home.registry}")
        for entry in entries:
            check(f"collab:{entry.name}", entry.exists, str(entry.root))
    except CollabKitError as exc:
        entries = []
        check("registry", False, exc.message)

    check(
        "telegram token",
        bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        "TELEGRAM_BOT_TOKEN unset -- the phone bridge is optional",
        fatal=False,
    )

    failures = [item for item in checks if not item["ok"] and item["fatal"]]
    if args.json:
        console.json_out({"checks": checks, "ok": not failures})
        return EXIT_OK if not failures else EXIT_ERROR

    for item in checks:
        if item["ok"]:
            mark = console.color("ok  ", "green", stream=sys.stdout)
        elif item["fatal"]:
            mark = console.color("FAIL", "red", stream=sys.stdout)
        else:
            mark = console.color("note", "yellow", stream=sys.stdout)
        console.out(f"{mark} {item['check']:<22} {item['detail']}")
    return EXIT_OK if not failures else EXIT_ERROR


# ==========================================================================
# helpers
# ==========================================================================


def _read_body(args: argparse.Namespace) -> str:
    """Body from ``--body``, ``--file``, ``--file -``, or a bare ``-``.

    Reading stdin only when explicitly asked for is deliberate: a create that
    blocks on an empty pipe looks exactly like a hung CLI, and these commands
    run inside agent sessions where nobody is watching to press Ctrl-D.
    """
    inline = getattr(args, "body", None)
    source = getattr(args, "file", None)
    positional = getattr(args, "body_stdin", None)

    if inline is not None and source:
        raise UsageError("pass either --body or --file, not both")
    if positional is not None and positional != "-":
        # A stray positional almost always means a quoting mistake. Silently
        # dropping it would create a handoff with an empty body and no warning.
        raise UsageError(
            f"unexpected argument {positional!r}",
            hint='give the body with --body "..." or --file <path>, or "-" to read stdin',
        )
    if inline is not None:
        return inline
    if source in ("-",) or positional == "-":
        return sys.stdin.read()
    if source:
        path = Path(source).expanduser()
        if not path.is_file():
            raise NotFoundError(f"body file not found: {path}")
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _print_handoff(handoff: Handoff, *, full: bool = False) -> None:
    console.out(f"id       {handoff.id}")
    console.out(f"state    {handoff.status}")
    console.out(f"to       {seats.label(handoff.to)}")
    console.out(f"from     {seats.label(handoff.sender)}")
    console.out(f"priority {handoff.priority}")
    console.out(f"created  {handoff.created}  ({handoff.age} ago)")
    if handoff.thread:
        console.out(f"thread   {handoff.thread}")
    if handoff.tags:
        console.out(f"tags     {', '.join(handoff.tags)}")
    if handoff.claimed_by:
        console.out(f"claimed  {seats.label(handoff.claimed_by)} at {handoff.claimed_at}")
    if handoff.note:
        console.out(f"note     {handoff.note}")
    console.out(f"title    {handoff.title}")
    if full and handoff.body.strip():
        console.out("")
        console.out(handoff.body.rstrip())
