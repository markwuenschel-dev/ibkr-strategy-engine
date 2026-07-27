"""Name sanitization -- the kit's path-traversal boundary.

Every externally supplied name that will become a path component passes through
here: collab names from the CLI, ``/c <project>`` names arriving from Telegram,
and the title fragment baked into a handoff id. Telegram in particular is a
fully untrusted channel, so this module is a security control, not a
convenience.

The rule is allowlist-only: the output alphabet is ``[a-z0-9-]``, which cannot
express ``..``, ``/``, ``\\``, a drive letter, an NTFS alternate data stream, or
a leading ``-`` that a shell would read as a flag.
"""

from __future__ import annotations

import re

from .errors import ValidationError

MAX_NAME_LENGTH = 64

_ALLOWED = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DASHES = re.compile(r"-{2,}")

# Windows forbids these basenames outright, with or without an extension.
# A collab named "con" would create a directory that cannot be opened, so it is
# rejected on every platform to keep collabs portable between machines.
_RESERVED = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
    # Names the kit reserves for its own top-level directories inside
    # $COLLAB_HOME; a collab claiming one would collide with kit state.
    + ["logs", "outbox", "inbox", "archive", "tmp", "collabs"]
)


def slugify(raw: str, *, fallback: str = "untitled", max_length: int = MAX_NAME_LENGTH) -> str:
    """Reduce arbitrary text to ``[a-z0-9-]``, never raising.

    Use for *derived* fragments (handoff titles). For anything that must round
    trip -- a collab name the user will retype -- use :func:`validate_name`, so
    a typo is an error rather than a silently different directory.
    """
    text = (raw or "").strip().lower()
    text = _NON_ALNUM.sub("-", text)
    text = _DASHES.sub("-", text).strip("-")
    if len(text) > max_length:
        # Cut on a boundary so the tail is a whole word, not a severed one.
        text = text[:max_length].rstrip("-")
    if not text or text in _RESERVED:
        return fallback
    return text


def validate_name(raw: str, *, kind: str = "collab name") -> str:
    """Return ``raw`` unchanged if it is already a safe path component.

    Rejects rather than repairs. ``handoff foo/../bar`` must fail loudly; a
    silent rewrite to ``foo-bar`` would send the caller's handoffs somewhere
    they will never look for them.
    """
    name = (raw or "").strip()
    if not name:
        raise ValidationError(f"{kind} must not be empty")
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(
            f"{kind} {name!r} is longer than {MAX_NAME_LENGTH} characters"
        )
    if not _ALLOWED.match(name):
        raise ValidationError(
            f"invalid {kind} {name!r}",
            hint=(
                "use lowercase letters, digits and hyphens only, starting with a "
                "letter or digit (e.g. 'auth-refactor')"
            ),
        )
    if name in _RESERVED:
        raise ValidationError(
            f"{kind} {name!r} is reserved",
            hint="reserved by the kit or by Windows; pick another name",
        )
    return name


def is_valid_name(raw: str) -> bool:
    """Non-raising form of :func:`validate_name`."""
    try:
        validate_name(raw)
    except ValidationError:
        return False
    return True


def coerce_name(raw: str, *, fallback: str = "") -> str:
    """Sanitize an untrusted name for path use, or return ``fallback``.

    This is the Telegram ``/c <project>`` path. Inbound chat text is hostile by
    default, so it is slugified and then re-validated: slugify alone guarantees
    the alphabet, and validate_name additionally rejects reserved names.
    """
    candidate = slugify(raw, fallback="")
    if not candidate or not is_valid_name(candidate):
        return fallback
    return candidate
