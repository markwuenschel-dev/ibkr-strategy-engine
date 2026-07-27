"""Append-only JSONL event log.

One line per state change, per collab. This is the audit trail that answers
"who claimed what, when, and did anyone actually review it" after the fact --
the handoff files themselves only show the *current* state, and archived ones
get swept out of sight.

Writes use a single ``os.write`` to an ``O_APPEND`` descriptor. On POSIX an
append shorter than ``PIPE_BUF`` is atomic against concurrent appenders, which
is why each record is emitted as one already-encoded line rather than built up
with multiple writes. Records longer than that can in principle interleave under
heavy concurrency, so oversized fields are truncated on the way in.

Logging is strictly best-effort: a full disk or a read-only mount must never
fail a handoff transition that has already happened on disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .atomic import ensure_dir
from .timeutil import iso

MAX_FIELD_CHARS = 500
MAX_LINE_BYTES = 4000
DEFAULT_ROTATE_BYTES = 8 * 1024 * 1024


class EventLog:
    """A JSONL log file. Never raises."""

    def __init__(self, path: Path | str, *, rotate_bytes: int = DEFAULT_ROTATE_BYTES) -> None:
        self.path = Path(path)
        self.rotate_bytes = rotate_bytes
        self._enabled = os.environ.get("COLLAB_KIT_NO_LOG", "") == ""

    def write(self, event: str, subject: str = "", **fields: Any) -> None:
        if not self._enabled:
            return
        record: dict[str, Any] = {"ts": iso(), "event": event}
        if subject:
            record["id"] = subject
        for key, value in fields.items():
            if value in (None, "", [], {}):
                continue
            record[key] = _clip(value)

        try:
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            line = json.dumps({"ts": record["ts"], "event": event, "unserializable": True})

        data = (line + "\n").encode("utf-8")
        if len(data) > MAX_LINE_BYTES:
            data = (
                json.dumps(
                    {"ts": record["ts"], "event": event, "id": subject, "truncated": True}
                )
                + "\n"
            ).encode("utf-8")

        try:
            self._rotate_if_needed()
            ensure_dir(self.path.parent)
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError:
            # Best-effort by contract. Losing an audit line is strictly better
            # than failing a state transition that already succeeded.
            return

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        """Last ``limit`` records, newest last. Skips corrupt lines."""
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-limit * 2 :]:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
        return out[-limit:]

    def _rotate_if_needed(self) -> None:
        """Rotate to ``<name>.1`` past the size cap, keeping one generation.

        Bounded on purpose: unbounded growth in a directory the user never looks
        at is how a long-running collab quietly fills a disk.
        """
        try:
            if self.path.stat().st_size < self.rotate_bytes:
                return
        except OSError:
            return
        try:
            os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
        except OSError:
            return


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[: MAX_FIELD_CHARS - 1] + "..."
    if isinstance(value, (list, tuple)):
        return [_clip(item) for item in value][:20]
    return value
