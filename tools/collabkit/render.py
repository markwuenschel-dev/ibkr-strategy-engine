"""``{{PLACEHOLDER}}`` template rendering for the per-project scaffold.

Not a template *language*. ``newproject`` renders four markdown files; anything
richer would mean the templates start encoding logic, and the templates are
meant to be edited by hand after generation.

Rendering is **strict**: an unresolved ``{{PLACEHOLDER}}`` raises. A scaffolded
PROTOCOL.md that still says ``{{GUARDRAILS}}`` is worse than a failed
newproject, because the agent reads it as if it were finished -- and
ARCHITECTURE.md is explicit that guardrails are "written fresh per project,
never inherited".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

from .atomic import atomic_write_text
from .errors import ValidationError

PLACEHOLDER = re.compile(r"\{\{\s*([A-Z][A-Z0-9_]*)\s*\}\}")
TEMPLATE_SUFFIX = ".tmpl"


def render(text: str, values: Mapping[str, str], *, allow_missing: bool = False) -> str:
    """Substitute every ``{{NAME}}``.

    Substitution is single-pass: a value that itself contains ``{{X}}`` is
    emitted literally rather than re-expanded, so user-supplied guardrail text
    can never inject a placeholder.
    """
    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return str(values[key])
        missing.append(key)
        return match.group(0)

    result = PLACEHOLDER.sub(substitute, text)
    if missing and not allow_missing:
        unique = sorted(set(missing))
        raise ValidationError(
            f"template has unresolved placeholders: {', '.join(unique)}",
            hint="pass a value for each, or re-run with --allow-missing",
        )
    return result


def placeholders(text: str) -> set[str]:
    """Every placeholder name in ``text`` -- used to validate a template set."""
    return {match.group(1) for match in PLACEHOLDER.finditer(text)}


def render_file(
    source: Path,
    destination: Path,
    values: Mapping[str, str],
    *,
    overwrite: bool = False,
    allow_missing: bool = False,
) -> Path:
    """Render one template file. Refuses to clobber unless told to.

    Never overwriting by default matters on re-scaffold: PROTOCOL.md is a file
    the human and the agents edit, and regenerating over those edits would
    silently discard the project's accumulated ground rules.
    """
    if destination.exists() and not overwrite:
        raise ValidationError(
            f"{destination} already exists",
            hint="pass --force to overwrite it",
        )
    text = source.read_text(encoding="utf-8")
    atomic_write_text(destination, render(text, values, allow_missing=allow_missing))
    return destination


def render_tree(
    source_dir: Path,
    destination_dir: Path,
    values: Mapping[str, str],
    *,
    overwrite: bool = False,
    allow_missing: bool = False,
) -> list[Path]:
    """Render a whole template directory, preserving its structure.

    ``foo.md.tmpl`` becomes ``foo.md``; files without the suffix are copied
    verbatim. Returns the destinations written, in sorted order.
    """
    if not source_dir.is_dir():
        raise ValidationError(f"template directory not found: {source_dir}")

    written: list[Path] = []
    for path in sorted(source_dir.rglob("*")):
        if path.is_dir() or path.name.startswith("."):
            continue
        relative = path.relative_to(source_dir)
        if path.name.endswith(TEMPLATE_SUFFIX):
            relative = relative.with_name(path.name[: -len(TEMPLATE_SUFFIX)])
        target = destination_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            continue
        text = path.read_text(encoding="utf-8")
        atomic_write_text(target, render(text, values, allow_missing=allow_missing))
        written.append(target)
    return written
