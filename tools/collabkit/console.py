"""Terminal output helpers.

Every CLI in this kit prints through here so that colour, quiet mode and stream
choice behave identically across ``handoff``, the watchers and the bridge.

Rules:

* Colour only when stdout is a TTY and ``NO_COLOR`` is unset (the no-color.org
  convention). Watcher output is routinely piped into an agent session, and
  escape codes there are noise at best.
* Diagnostics go to stderr, data goes to stdout. That is what makes
  ``handoff foo list --json | jq`` work while progress chatter stays visible.
* Output is UTF-8 where the stream can be persuaded to accept it, and falls
  back to ASCII glyphs where it cannot. This is not cosmetic: on Windows,
  ``sys.stderr`` defaults to the ``backslashreplace`` error handler over a
  legacy code page, so an unguarded ``✓`` reaches the user as the literal text
  ``\\u2713``. Decorative characters therefore go through :func:`glyph`, which
  resolves once at import against the *effective* stream encoding.
"""

from __future__ import annotations

import os
import sys
from typing import Any, TextIO

_RESET = "\033[0m"
_STYLES = {
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}


_GLYPHS_UNICODE = {
    "ok": "✓",
    "dot": "·",
    "dash": "—",
    "ellipsis": "…",
    "arrow": "→",
    "rule": "─",
}
_GLYPHS_ASCII = {
    "ok": "+",
    "dot": "-",
    "dash": "--",
    "ellipsis": "...",
    "arrow": "->",
    "rule": "-",
}


def _make_utf8(stream: TextIO) -> None:
    """Ask a stream to speak UTF-8. Silently gives up if it cannot.

    ``reconfigure`` exists on 3.7+ TextIOWrapper but not on the StringIO a test
    harness or a capturing wrapper may substitute, so failure is expected and
    handled rather than guarded by a type check.
    """
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError, LookupError):
        pass


def _probe_unicode() -> bool:
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            return False
        try:
            "".join(_GLYPHS_UNICODE.values()).encode(encoding)
        except (LookupError, UnicodeEncodeError):
            return False
    return True


_make_utf8(sys.stdout)
_make_utf8(sys.stderr)
_UNICODE_OK = os.environ.get("COLLAB_KIT_ASCII", "") == "" and _probe_unicode()
_GLYPH = _GLYPHS_UNICODE if _UNICODE_OK else _GLYPHS_ASCII


def glyph(name: str) -> str:
    """A decorative character that the current streams can actually render."""
    return _GLYPH.get(name, "")


OK_MARK = glyph("ok")
DOT = glyph("dot")
DASH = glyph("dash")
ELLIPSIS = glyph("ellipsis")
ARROW = glyph("arrow")


def _stream_supports_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("COLLAB_KIT_FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def color(text: str, style: str, *, stream: TextIO | None = None) -> str:
    """Wrap ``text`` in an ANSI style when the target stream can render it."""
    stream = stream or sys.stdout
    if not _stream_supports_color(stream):
        return text
    prefix = _STYLES.get(style)
    return f"{prefix}{text}{_RESET}" if prefix else text


def _emit(stream: TextIO, text: str) -> None:
    try:
        stream.write(text + "\n")
        stream.flush()
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write(text.encode(encoding, "replace").decode(encoding, "replace") + "\n")
        stream.flush()
    except (BrokenPipeError, ValueError):
        # `handoff list | head` closes the pipe early; that is not an error.
        pass


def out(*parts: Any) -> None:
    """Data line -> stdout."""
    _emit(sys.stdout, " ".join(str(part) for part in parts))


def info(message: str) -> None:
    """Progress/diagnostic line -> stderr."""
    _emit(sys.stderr, message)


def ok(message: str) -> None:
    _emit(sys.stderr, f"{color(OK_MARK, 'green', stream=sys.stderr)} {message}")


def warn(message: str) -> None:
    _emit(sys.stderr, f"{color('warn:', 'yellow', stream=sys.stderr)} {message}")


def error(message: str) -> None:
    _emit(sys.stderr, f"{color('error:', 'red', stream=sys.stderr)} {message}")


def hint(message: str) -> None:
    _emit(sys.stderr, f"  {color(message, 'dim', stream=sys.stderr)}")


def rule(title: str = "", width: int = 64) -> None:
    bar = glyph("rule")
    if title:
        pad = max(0, width - len(title) - 3)
        _emit(sys.stderr, color(f"{bar * 2} {title} " + bar * pad, "dim", stream=sys.stderr))
    else:
        _emit(sys.stderr, color(bar * width, "dim", stream=sys.stderr))


def json_out(payload: Any) -> None:
    """Machine-readable stdout. Always a single line for easy piping."""
    import json

    _emit(sys.stdout, json.dumps(payload, ensure_ascii=False, default=str))
