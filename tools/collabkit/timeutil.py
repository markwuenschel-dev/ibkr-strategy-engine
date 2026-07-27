"""UTC time helpers.

Everything on disk is UTC and ISO-8601 with a trailing ``Z``. Local time never
appears in a handoff file: two agents on two machines must sort the same
timeline, and a naive local timestamp makes that impossible to reconstruct.
"""

from __future__ import annotations

import datetime as _dt

UTC = _dt.timezone.utc


def utcnow() -> _dt.datetime:
    """Timezone-aware current time in UTC."""
    return _dt.datetime.now(UTC)


def iso(when: _dt.datetime | None = None) -> str:
    """ISO-8601 in UTC with a ``Z`` suffix and second precision.

    Naive datetimes are assumed to already be UTC rather than silently
    localized -- guessing a timezone is how timestamps drift.
    """
    when = when or utcnow()
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compact(when: _dt.datetime | None = None) -> str:
    """Filename-safe stamp, e.g. ``20260727T070612Z``.

    Sorts lexicographically in timestamp order, which is what makes a plain
    ``ls`` of pending/ read as a queue.
    """
    when = when or utcnow()
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def parse_iso(text: str) -> _dt.datetime | None:
    """Best-effort ISO-8601 parse; returns None instead of raising.

    Callers are display code and sort keys -- a corrupt timestamp in one file
    must not take down ``handoff status`` for every other collab.
    """
    if not text:
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def age_seconds(text: str, *, now: _dt.datetime | None = None) -> float | None:
    """Seconds elapsed since an ISO-8601 timestamp, or None if unparseable."""
    parsed = parse_iso(text)
    if parsed is None:
        return None
    return ((now or utcnow()) - parsed).total_seconds()


def human_age(seconds: float | None) -> str:
    """Compact relative age: ``12s``, ``4m``, ``3h``, ``2d``.

    Used in list/status output where column width matters more than precision.
    """
    if seconds is None:
        return "?"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"
