"""``.env`` loading. The file is a *source*; the environment stays the authority.

Secrets for this repo live in a git-ignored ``.env`` at the project root, and
two separate entry points need them: ``tools/telegram-bridge.py`` wants
``TELEGRAM_BOT_TOKEN``, and the engine wants ``IBKR_*``. Both read
``os.environ``, so something has to put the file's contents there first. This is
that something.

Three decisions worth stating, because each has a plausible-looking opposite:

**A real environment variable always wins.** ``load`` fills gaps only. An
explicit ``$env:TELEGRAM_BOT_TOKEN`` in the shell, or a secret injected by CI,
must not be silently overridden by a stale file someone forgot on disk -- that
failure mode is invisible and produces "but I *set* it" bug reports. Pass
``override=True`` to invert this, and mean it.

**A ``#`` inside a value is literal.** Most dotenv parsers strip trailing
comments from unquoted values. This file holds API tokens; a token quietly
truncated at a ``#`` would present as an authentication failure with no visible
cause, which is a far worse outcome than requiring quotes around an unusual
value. Quote anything with trailing whitespace; everything after ``=`` is
otherwise taken verbatim.

**No third-party dependency.** collab-kit is stdlib-only and stays that way
(see ``ARCHITECTURE.md``); ``python-dotenv`` would be the first crack in that.
The grammar below is small enough that owning it is cheaper than depending on it.

Values are never logged, printed, or included in any error message. Everything
this module reports back -- loaded keys, skipped keys, parse problems -- carries
names and line numbers only, so a caller can be verbose without leaking.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

FILENAME = ".env"

# Explicit override, mostly for tests and for installs whose secrets live
# outside any checkout. Read from the real environment, never from a .env --
# a file cannot relocate its own lookup.
ENV_FILE = "COLLAB_ENV_FILE"

# POSIX name grammar. Windows is laxer, but a key that only works on one
# platform is a trap, so the strict rule applies everywhere.
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}


@dataclass(frozen=True)
class DotenvResult:
    """What a load did. Names and counts only -- never values."""

    path: Path | None = None
    loaded: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    insecure: bool = False

    @property
    def found(self) -> bool:
        return self.path is not None

    def describe(self) -> str:
        """One line for a diagnostic surface such as ``engine doctor``."""
        if self.path is None:
            return "no .env found"
        parts = [f"{len(self.loaded)} loaded from {self.path}"]
        if self.skipped:
            parts.append(f"{len(self.skipped)} already in the environment")
        if self.problems:
            parts.append(f"{len(self.problems)} unparsable line(s)")
        if self.insecure:
            parts.append("READABLE BY OTHER USERS")
        return ", ".join(parts)


_LAST: DotenvResult = DotenvResult()
_DONE = False


def find_file(start: Path | None = None) -> Path | None:
    """The nearest ``.env`` at or above ``start`` (default: the cwd).

    Walking upward is what lets one file at the repo root serve both callers:
    the bridge runs from the root, the engine runs from ``engine/``. A nearer
    file wins, so ``engine/.env`` can override the root during development
    without editing the shared one.
    """
    override = os.environ.get(ENV_FILE, "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None

    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / FILENAME
        if candidate.is_file():
            return candidate
    return None


def parse(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse dotenv text into ``(values, problems)``.

    ``problems`` are human-readable and carry a line number and, where it is
    safe, the offending key -- never a value.
    """
    values: dict[str, str] = {}
    problems: list[str] = []

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        # `export FOO=bar` is what people paste out of shell instructions --
        # including the hint this repo's own bridge prints.
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export") :].lstrip()

        key, separator, value = line.partition("=")
        if not separator:
            problems.append(f"line {number}: no '=' found")
            continue

        key = key.strip()
        if not _KEY.match(key):
            problems.append(f"line {number}: {key!r} is not a valid variable name")
            continue

        values[key] = _unquote(value.strip())

    return values, problems


def _unquote(value: str) -> str:
    """Strip matching surrounding quotes; decode escapes only inside them."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        return _unescape(inner) if value[0] == '"' else inner
    return value


def _unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            following = value[index + 1]
            if following in _ESCAPES:
                out.append(_ESCAPES[following])
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _is_world_readable(path: Path) -> bool:
    """True when a POSIX mode grants group or other read. Always False on Windows.

    Windows uses ACLs that ``st_mode`` does not describe, so a mode check there
    would report confident nonsense. Returning False is the honest answer: this
    module did not check, and it says so by not claiming a problem.
    """
    if os.name != "posix":
        return False
    try:
        mode = path.stat().st_mode
    except OSError:  # pragma: no cover - raced deletion
        return False
    return bool(mode & (stat.S_IRGRP | stat.S_IROTH))


def load(
    path: Path | None = None,
    *,
    environ: dict[str, str] | None = None,
    search_from: Path | None = None,
    override: bool = False,
    reload: bool = False,
) -> DotenvResult:
    """Apply a ``.env`` to the process environment. Safe to call more than once.

    Repeat calls return the first result rather than re-reading, so a second
    caller (``engine doctor`` reporting what ``engine`` already loaded) sees what
    actually happened instead of an empty second pass. ``reload=True`` forces the
    work; tests want it, production does not.
    """
    global _LAST, _DONE

    explicit = path is not None or environ is not None or search_from is not None
    if _DONE and not reload and not explicit:
        return _LAST

    target = os.environ if environ is None else environ
    resolved = path if path is not None else find_file(search_from)

    if resolved is None:
        result = DotenvResult()
    else:
        try:
            text = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            result = DotenvResult(problems=(f"cannot read {resolved}: {exc.strerror}",))
        except UnicodeDecodeError:
            result = DotenvResult(path=resolved, problems=(f"{resolved} is not valid UTF-8",))
        else:
            values, problems = parse(text)
            loaded, skipped = [], []
            for key, value in values.items():
                if key in target and not override:
                    skipped.append(key)
                    continue
                target[key] = value
                loaded.append(key)
            result = DotenvResult(
                path=resolved,
                loaded=tuple(loaded),
                skipped=tuple(skipped),
                problems=tuple(problems),
                insecure=_is_world_readable(resolved),
            )

    if not explicit:
        _LAST, _DONE = result, True
    return result


# Alias: `from collabkit.dotenv import load_dotenv` reads better at a call site
# than `dotenv.load`, and matches what people expect the function to be called.
load_dotenv = load


def last_result() -> DotenvResult:
    """What the most recent implicit :func:`load` did. For reporting surfaces."""
    return _LAST


def reset_for_tests() -> None:
    """Forget the cached result. Only tests should need this."""
    global _LAST, _DONE
    _LAST, _DONE = DotenvResult(), False
