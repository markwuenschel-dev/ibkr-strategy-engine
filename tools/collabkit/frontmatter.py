"""Strict, minimal YAML-subset frontmatter.

ARCHITECTURE.md fixes the wire format ("markdown files with YAML frontmatter")
and forbids third-party packages, so PyYAML is not an option. Rather than
pretend to implement YAML -- a genuinely large spec with famous footguns -- this
module implements a *documented subset* and rejects everything else with a line
number.

Supported::

    ---
    id: 20260727T070612Z-ab12cd-review-auth      # bare scalar
    title: "Review: the auth fix"                # quoted (needed for ': ')
    priority: high
    draft: false                                 # true/false -> bool
    attempts: 2                                  # int, 1.5 -> float
    thread: null                                 # null/~/empty -> None
    tags: [security, auth]                       # inline list
    reviewers:                                   # block list
      - grok
      - claude
    ---

Not supported, by design: nested mappings, multi-line scalars (``|``/``>``),
anchors, aliases, tags, multi-document streams, flow mappings. A handoff header
is metadata for routing; anything structural belongs in the markdown body.

Round-trip guarantee: ``parse(dumps(meta, body))`` returns ``meta`` with the
same keys and equal values for every type this module emits. That guarantee is
what lets the state machine rewrite a file's status without corrupting fields it
does not understand.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .errors import ValidationError

DELIMITER = "---"
_END_DELIMITERS = ("---", "...")

# A key is a bare identifier. Rejecting quoted/exotic keys keeps handoff headers
# greppable with a plain `grep '^to:'`, which several tools here rely on.
_KEY = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:(?P<rest>.*)$")
_LIST_ITEM = re.compile(r"^-\s*(?P<value>.*)$")
# Any of these force a value to be emitted quoted. The control-character class
# is load-bearing and easy to miss: a title containing a newline or a tab looks
# innocuous to the leading/trailing-whitespace checks, but emitting it bare
# splits one field across two lines and corrupts the whole document.
_NEEDS_QUOTING = re.compile(
    r"^\s|\s$|^$|[:#\[\]{}&*!|>'\"%@`,]|[\x00-\x1f\x7f]|^-\s|^[-?]$"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BOOL_TRUE = frozenset({"true", "yes", "on"})
_BOOL_FALSE = frozenset({"false", "no", "off"})
_NULLS = frozenset({"", "null", "~", "none"})


class FrontmatterError(ValidationError):
    """Frontmatter is present but does not parse under the supported subset."""


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into ``(metadata, body)``.

    A document with no frontmatter yields ``({}, text)`` -- that is a plain
    markdown file, not an error. A document that *opens* frontmatter and never
    closes it is an error: silently treating a truncated header as body text
    would let a half-written file masquerade as a valid handoff.
    """
    if not text:
        return {}, ""

    # Strip a UTF-8 BOM (escaped, not literal -- an invisible character in
    # source is a trap) and normalize CRLF, so a file written by a Windows
    # editor parses identically to one written by an agent on Linux.
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    if not lines or lines[0].strip() != DELIMITER:
        return {}, normalized

    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in _END_DELIMITERS:
            end = index
            break
    if end is None:
        raise FrontmatterError(
            "unterminated frontmatter: opening '---' has no closing '---'",
            hint="every handoff file must close its header with a line containing only ---",
        )

    meta = _parse_block(lines[1:end], line_offset=2)
    body = "\n".join(lines[end + 1 :])
    return meta, body.lstrip("\n")


def parse_file(path: Any) -> tuple[dict[str, Any], str]:
    """:func:`parse` a file on disk, tolerating undecodable bytes."""
    from pathlib import Path

    return parse(Path(path).read_text(encoding="utf-8", errors="replace"))


def _parse_block(lines: list[str], *, line_offset: int) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    pending_list_key: str | None = None
    # Keys that opened a bare `key:` line. Only these may collapse to None when
    # they end up empty -- an explicit `tags: []` must stay an empty list.
    opened_bare: set[str] = set()

    for offset, raw_line in enumerate(lines):
        lineno = line_offset + offset
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        # A block list continues the previous key until a new key appears.
        if pending_list_key is not None and raw_line[:1] in (" ", "\t", "-"):
            item = _LIST_ITEM.match(stripped)
            if item:
                meta[pending_list_key].append(_scalar(item.group("value"), lineno))
                continue

        match = _KEY.match(stripped)
        if not match:
            raise FrontmatterError(
                f"line {lineno}: expected 'key: value', got {stripped!r}",
                hint="frontmatter supports flat key/value pairs and lists only",
            )

        key = match.group("key")
        rest = match.group("rest").strip()
        if key in meta:
            raise FrontmatterError(f"line {lineno}: duplicate key {key!r}")

        if rest == "":
            # Either an explicit null or the head of a block list; the next
            # lines decide. Start as a list and collapse to None if empty.
            meta[key] = []
            pending_list_key = key
            opened_bare.add(key)
            continue

        pending_list_key = None
        meta[key] = _value(rest, lineno)

    # A key that opened a block list but received no items was really a null.
    for key in opened_bare:
        if meta.get(key) == []:
            meta[key] = None
    return meta


def _value(raw: str, lineno: int) -> Any:
    if raw.startswith("[") and raw.endswith("]"):
        return _inline_list(raw[1:-1], lineno)
    if raw.startswith("{"):
        raise FrontmatterError(
            f"line {lineno}: inline mappings are not supported",
            hint="flatten the value or move the structure into the body",
        )
    return _scalar(raw, lineno)


def _inline_list(inner: str, lineno: int) -> list[Any]:
    items: list[Any] = []
    for part in _split_commas(inner):
        part = part.strip()
        if part:
            items.append(_scalar(part, lineno))
    return items


def _split_commas(text: str) -> list[str]:
    """Split on commas that are not inside quotes."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for char in text:
        if quote:
            buf.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            buf.append(char)
            continue
        if char == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    parts.append("".join(buf))
    return parts


def _scalar(raw: str, lineno: int) -> Any:
    text = raw.strip()
    if not text:
        return None

    if text[0] in ("'", '"'):
        return _quoted(text, lineno)

    # Only a whitespace-preceded '#' opens a comment, as in YAML proper --
    # otherwise an id like `abc#1` would lose its tail.
    hash_at = _comment_index(text)
    if hash_at is not None:
        text = text[:hash_at].strip()
        if not text:
            return None

    lowered = text.lower()
    if lowered in _NULLS:
        return None
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False

    try:
        return int(text)
    except ValueError:
        pass
    try:
        # Guard against float("nan") / float("inf") sneaking in: they are not
        # JSON-serializable and would break --json output downstream.
        number = float(text)
        if number == number and number not in (float("inf"), float("-inf")):
            return number
    except ValueError:
        pass
    return text


def _comment_index(text: str) -> int | None:
    for index, char in enumerate(text):
        if char == "#" and (index == 0 or text[index - 1].isspace()):
            return index
    return None


def _quoted(text: str, lineno: int) -> str:
    quote = text[0]
    if len(text) < 2 or not text.endswith(quote):
        raise FrontmatterError(f"line {lineno}: unterminated {quote} quoted string")
    inner = text[1:-1]
    if quote == "'":
        # YAML single quotes: only '' is an escape.
        return inner.replace("''", "'")
    return _unescape_double(inner, lineno)


def _unescape_double(inner: str, lineno: int) -> str:
    out: list[str] = []
    index = 0
    simple = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0"}
    while index < len(inner):
        char = inner[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(inner):
            raise FrontmatterError(f"line {lineno}: string ends with a dangling backslash")
        esc = inner[index]
        if esc in simple:
            out.append(simple[esc])
            index += 1
            continue
        if esc == "u":
            hexits = inner[index + 1 : index + 5]
            if len(hexits) != 4:
                raise FrontmatterError(f"line {lineno}: truncated \\u escape")
            try:
                out.append(chr(int(hexits, 16)))
            except ValueError:
                raise FrontmatterError(f"line {lineno}: bad \\u escape {hexits!r}") from None
            index += 5
            continue
        raise FrontmatterError(f"line {lineno}: unknown escape '\\{esc}'")
    return "".join(out)


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------


def dumps(meta: Mapping[str, Any], body: str = "", *, key_order: list[str] | None = None) -> str:
    """Render ``meta`` + ``body`` back to a frontmatter document.

    ``key_order`` pins the leading keys so every handoff file opens with the
    same fields in the same order; unlisted keys follow in insertion order.
    Stable field order is what makes ``git diff`` on a handoff readable.
    """
    ordered: list[str] = []
    for key in key_order or []:
        if key in meta:
            ordered.append(key)
    ordered.extend(key for key in meta if key not in ordered)

    lines = [DELIMITER]
    for key in ordered:
        lines.extend(_emit(key, meta[key]))
    lines.append(DELIMITER)

    text = "\n".join(lines) + "\n"
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if body:
        text += "\n" + body + "\n"
    return text


def _emit(key: str, value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        if not value:
            return [f"{key}: []"]
        return [f"{key}:"] + [f"  - {_render_scalar(item)}" for item in value]
    return [f"{key}: {_render_scalar(value)}"]


def _render_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if _NEEDS_QUOTING.search(text) or text.lower() in _NULLS | _BOOL_TRUE | _BOOL_FALSE:
        return _quote(text)
    # A bare value that would parse back as a number must be quoted, or a title
    # of "2024" round-trips into the integer 2024.
    try:
        int(text)
        return _quote(text)
    except ValueError:
        pass
    try:
        float(text)
        return _quote(text)
    except ValueError:
        pass
    return text


def _quote(text: str) -> str:
    """Double-quote a value, escaping everything that would not survive a line.

    The backslash substitution must come first, or every escape introduced
    afterwards would itself be re-escaped.
    """
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    # Remaining control characters have no short escape; \uXXXX round-trips
    # through _unescape_double and keeps the file one-field-per-line.
    escaped = _CONTROL.sub(lambda m: f"\\u{ord(m.group(0)):04x}", escaped)
    return f'"{escaped}"'
