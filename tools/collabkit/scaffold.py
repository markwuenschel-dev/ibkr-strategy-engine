"""Scaffolding a new collab: directories, templates, registry entry.

``bin/newproject`` owns the parts bash is good at -- argument parsing, prompting
on a TTY, ``git clone``. This module owns the parts it is not: name validation,
strict template rendering, and an atomic registry write. Splitting it that way
means the risky operations run through the same tested code paths as the rest
of the kit rather than a second, shell-shaped implementation of them.

Also runnable directly, which is what ``newproject`` invokes::

    python3 tools/collabkit/scaffold.py --name demo --root /path --repo-url ...
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # pragma: no cover - direct-script invocation
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collabkit import console, render  # noqa: E402
from collabkit.atomic import atomic_write_json, read_text  # noqa: E402
from collabkit.errors import (  # noqa: E402
    EXIT_ERROR,
    EXIT_OK,
    CollabKitError,
    ValidationError,
)
from collabkit.paths import CollabPaths, HomePaths, kit_dir  # noqa: E402
from collabkit.registry import Registry  # noqa: E402
from collabkit.slug import validate_name  # noqa: E402
from collabkit.timeutil import iso  # noqa: E402

TEMPLATE_DIRNAME = "collab-kit"

GUARDRAILS_TODO = """\
> **NOT YET WRITTEN.** This project has no guardrails. Write them before the
> first review, and delete this block.
>
> Guardrails are the invariants that must never break in *this* codebase --
> the ones where a regression is not a bug report but an incident. They are
> written fresh per project and are never inherited from another one.
"""

IDEA_TODO = "_(not yet written -- describe what this project is for)_"


def templates_dir() -> Path:
    """Where the per-project markdown templates live."""
    return kit_dir() / "tools" / TEMPLATE_DIRNAME


def template_values(
    *,
    name: str,
    root: Path,
    repo_url: str,
    repo_path: Path,
    reviewer: str,
    builder: str,
    guardrails: str,
    idea: str,
    home: HomePaths,
) -> dict[str, str]:
    """The complete placeholder set. Rendering is strict, so this must be total.

    Values are strings only: ``render`` is a single-pass substitution, and a
    non-string here would stringify in a way nobody chose.
    """
    return {
        "COLLAB_NAME": name,
        "COLLAB_ROOT": str(root),
        "REPO_URL": repo_url or "(none)",
        "REPO_PATH": str(repo_path),
        "REVIEWER": reviewer or "reviewer",
        "BUILDER": builder or "claude",
        "CREATED": iso(),
        "KIT_DIR": str(kit_dir()),
        "COLLAB_HOME": str(home.root),
        "GUARDRAILS": guardrails.strip() or GUARDRAILS_TODO,
        "IDEA": idea.strip() or IDEA_TODO,
    }


def scaffold(
    *,
    name: str,
    root: Path | None = None,
    repo_url: str = "",
    repo_path: Path | None = None,
    reviewer: str = "",
    builder: str = "claude",
    guardrails: str = "",
    idea: str = "",
    force: bool = False,
    register: bool = True,
) -> dict[str, Any]:
    """Create + register a collab and render its templates.

    Idempotent in the way that matters: re-running never overwrites an existing
    PROTOCOL.md unless ``force`` is set, because those files accumulate the
    project's hard-won ground rules and silently regenerating over them would
    discard exactly the content this system exists to protect.
    """
    validate_name(name)
    home = HomePaths.discover().ensure()
    collab_root = Path(root).expanduser().resolve() if root else home.collab_root(name)
    paths = CollabPaths.at(collab_root, name)
    existed = paths.exists()
    paths.ensure()

    clone_path = Path(repo_path).expanduser().resolve() if repo_path else paths.repo
    values = template_values(
        name=name,
        root=collab_root,
        repo_url=repo_url,
        repo_path=clone_path,
        reviewer=reviewer,
        builder=builder,
        guardrails=guardrails,
        idea=idea,
        home=home,
    )

    source = templates_dir()
    if not source.is_dir():
        raise ValidationError(
            f"template directory missing: {source}",
            hint="the kit clone looks incomplete; re-clone it",
        )
    written = render.render_tree(source, collab_root, values, overwrite=force)

    atomic_write_json(
        paths.meta_file,
        {
            "name": name,
            "root": str(collab_root),
            "repo": repo_url,
            "repo_path": str(clone_path),
            "reviewer": reviewer,
            "builder": builder,
            "created": values["CREATED"],
            "kit_version": _kit_version(),
            "guardrails_written": bool(guardrails.strip()),
        },
    )

    entry = None
    if register:
        entry = Registry(home).register(
            name,
            collab_root,
            repo=repo_url,
            repo_path=str(clone_path),
            reviewer=reviewer,
            force=True,
        )

    return {
        "name": name,
        "root": str(collab_root),
        "repo_path": str(clone_path),
        "repo_url": repo_url,
        "reviewer": reviewer,
        "builder": builder,
        "existed": existed,
        "rendered": [str(path) for path in written],
        "registered": bool(entry),
        "guardrails_written": bool(guardrails.strip()),
        "kickoff": str(paths.kickoff),
        "protocol": str(paths.protocol),
    }


def bootstrap_block(collab_root: Path) -> str:
    """The paste-into-your-agent block: the rendered KICKOFF.md.

    Reading the rendered file rather than re-deriving the text guarantees the
    printed instructions and the on-disk instructions cannot drift apart.
    """
    kickoff = CollabPaths.at(collab_root).kickoff
    text = read_text(kickoff, default="")
    if text.strip():
        return text
    return (
        f"KICKOFF.md was not rendered at {kickoff}.\n"
        "Re-run newproject with --force, or write it by hand."
    )


def _kit_version() -> str:
    try:
        from collabkit import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scaffold.py",
        description="Create, render and register a collab. Called by bin/newproject.",
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--root", default="")
    parser.add_argument("--repo-url", default="")
    parser.add_argument("--repo-path", default="")
    parser.add_argument("--reviewer", default="")
    parser.add_argument("--builder", default="claude")
    parser.add_argument("--guardrails-file", default="", help="path to the guardrails markdown")
    parser.add_argument("--idea", default="")
    parser.add_argument("--idea-file", default="")
    parser.add_argument("--force", action="store_true", help="overwrite rendered templates")
    parser.add_argument("--no-register", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--print-kickoff", action="store_true", help="print the bootstrap block on success"
    )
    args = parser.parse_args(argv)

    try:
        guardrails = _read_optional(args.guardrails_file)
        idea = args.idea or _read_optional(args.idea_file)
        result = scaffold(
            name=args.name,
            root=Path(args.root) if args.root else None,
            repo_url=args.repo_url,
            repo_path=Path(args.repo_path) if args.repo_path else None,
            reviewer=args.reviewer,
            builder=args.builder,
            guardrails=guardrails,
            idea=idea,
            force=args.force,
            register=not args.no_register,
        )
    except CollabKitError as exc:
        console.error(exc.message)
        if exc.hint:
            console.hint(exc.hint)
        return exc.exit_code
    except OSError as exc:
        console.error(f"filesystem error while scaffolding: {exc}")
        return EXIT_ERROR

    if args.json:
        console.json_out(result)
    else:
        console.ok(f"collab {result['name']} ready at {result['root']}")
        for path in result["rendered"]:
            console.info(f"  rendered {os.path.basename(path)}")
        if not result["guardrails_written"]:
            console.warn("no guardrails were written -- PROTOCOL.md contains a TODO block")
            console.hint("guardrails are per-project and must never be inherited; write them now")

    if args.print_kickoff:
        console.out("")
        console.out(bootstrap_block(Path(result["root"])))
    return EXIT_OK


def _read_optional(path: str) -> str:
    if not path:
        return ""
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ValidationError(f"file not found: {file_path}")
    return file_path.read_text(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
