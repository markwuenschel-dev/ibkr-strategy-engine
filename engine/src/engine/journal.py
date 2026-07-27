"""The durable order journal.

This module deliberately inverts the trade-off made by collab-kit's
``EventLog`` (``tools/collabkit/log.py``), and the contrast is the point.

``EventLog`` swallows ``OSError`` and returns::

    except OSError:
        # Best-effort by contract. Losing an audit line is strictly better
        # than failing a state transition that already succeeded.
        return

That is right for handoffs: the rename already happened on disk, and refusing to
continue would tempt a caller into retrying a completed transition. It is
*wrong* for orders. An order the engine cannot record is a position nobody knows
about -- not a missing log line, a missing fact about money. So here:

* a failed write raises :class:`~engine.errors.JournalError` and stops trading;
* every record is ``fsync``-ed before the caller is told it was written;
* the file is **never rotated or truncated**. It grows forever. A trading record
  that quietly discards its own history is not a record.

The format is JSON Lines: one self-contained object per line, appended. Nothing
rewrites earlier lines, so a truncated final line from a hard kill costs the last
record and nothing before it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import JournalError

SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(when: datetime | None = None) -> str:
    return (when or utc_now()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class OrderJournal:
    """Append-only, fsync'd, never-rotated record of everything order-related."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- writing ---------------------------------------------------------

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one record. Raises :class:`JournalError` if it cannot.

        Returns the record as written, so a caller can hand the same object to
        an alert without rebuilding it and risking the two disagreeing.
        """
        record: dict[str, Any] = {"v": SCHEMA_VERSION, "ts": iso(), "event": event}
        for key, value in fields.items():
            if value is not None:
                record[key] = value

        try:
            line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        except Exception as exc:
            # Deliberately broad. `default=str` calls __str__ on whatever it was
            # handed, and that can raise anything at all -- so narrowing this to
            # (TypeError, ValueError) would let an arbitrary exception escape and
            # break this module's one promise: that a journal failure arrives as
            # a JournalError the caller can act on, not as a stray traceback.
            raise JournalError(
                f"could not serialize a {event!r} record: {type(exc).__name__}: {exc}",
                hint="an order record that cannot be written must not be silently dropped",
            ) from exc

        self._append(line + "\n")
        return record

    def _append(self, text: str) -> None:
        data = text.encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                written = os.write(fd, data)
                if written != len(data):  # pragma: no cover - short write on append
                    raise JournalError(
                        f"short write to the order journal ({written}/{len(data)} bytes)"
                    )
                # The caller is about to be told this is durable. Make it true
                # before returning, not eventually.
                os.fsync(fd)
            finally:
                os.close(fd)
        except JournalError:
            raise
        except OSError as exc:
            raise JournalError(
                f"cannot write the order journal at {self.path}: {exc}",
                hint=(
                    "trading is halted: the engine will not place an order it cannot "
                    "record. Fix the path or permissions and retry."
                ),
            ) from exc

    def preflight(self) -> None:
        """Prove the journal is writable *before* connecting to the broker.

        Discovering an unwritable journal after a fill has already happened is
        the worst possible ordering, so the check is forced to the front.
        """
        self.record("preflight", note="journal writable")

    # -- reading ---------------------------------------------------------

    def __iter__(self) -> Iterator[dict[str, Any]]:
        """Yield every well-formed record, skipping a truncated final line."""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise JournalError(f"cannot read the order journal: {exc}") from exc

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                # Only the last line can plausibly be torn; anything else is
                # corruption worth seeing, but not worth crashing a read on.
                continue
            if isinstance(parsed, dict):
                yield parsed

    def records(self) -> list[dict[str, Any]]:
        return list(self)

    def orders_today(self, *, now: datetime | None = None) -> int:
        """Count orders *placed* so far in the current UTC day.

        Counted from disk rather than from an in-process counter on purpose: a
        crash-looping engine restarts its memory but not its day, and a
        per-process cap would let it place orders without bound.
        """
        today = (now or utc_now()).strftime("%Y-%m-%d")
        return sum(
            1
            for record in self
            if record.get("event") == "order_placed"
            and str(record.get("ts", "")).startswith(today)
        )

    def tail(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.records()[-limit:]
