"""Handoff identifiers.

Format::

    20260727T070612Z-a1b2c3-review-the-auth-fix
    |--------------| |----| |------------------|
     UTC timestamp   entropy  slugified title

Three properties are load-bearing:

* **Lexicographic order == chronological order.** ``ls pending/`` reads as the
  queue, and sorting never needs to open a file.
* **Collision-free without coordination.** Two agents on two machines create
  handoffs in the same second with no shared lock; 24 bits of entropy makes a
  same-second collision negligible, and the store's ``O_EXCL`` create catches
  the residual case rather than silently overwriting.
* **Readable.** The title fragment means a bare id in a chat message still tells
  a human what it was about.
"""

from __future__ import annotations

import re
import secrets

from .slug import slugify
from .timeutil import compact

ENTROPY_BYTES = 3
TITLE_SLUG_MAX = 48

_ID_RE = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)-(?P<rand>[0-9a-f]{6})(?:-(?P<slug>[a-z0-9-]*))?$")


def new_id(title: str = "", *, when: object = None) -> str:
    """Mint an id for a new handoff."""
    stamp = compact(when)  # type: ignore[arg-type]
    entropy = secrets.token_hex(ENTROPY_BYTES)
    fragment = slugify(title, fallback="", max_length=TITLE_SLUG_MAX)
    return f"{stamp}-{entropy}-{fragment}" if fragment else f"{stamp}-{entropy}"


def is_valid(candidate: str) -> bool:
    """Whether ``candidate`` is a well-formed handoff id.

    Also the filename-safety check: the pattern admits only ``[0-9a-zA-Z-]``,
    so an id read back out of a file can be joined to a path directly.
    """
    return bool(_ID_RE.match(candidate or ""))


def timestamp_of(handoff_id: str) -> str:
    """The ``20260727T070612Z`` prefix, or ``""`` if the id is malformed."""
    match = _ID_RE.match(handoff_id or "")
    return match.group("stamp") if match else ""


def archive_partition(handoff_id: str) -> tuple[str, str]:
    """``(YYYY, MM)`` for the archive layout, defaulting to ``0000/00``.

    Archives are partitioned by month so a long-running collab does not end up
    with one directory holding tens of thousands of entries.
    """
    stamp = timestamp_of(handoff_id)
    if len(stamp) >= 6:
        return stamp[:4], stamp[4:6]
    return "0000", "00"
