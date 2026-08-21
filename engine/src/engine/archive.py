"""Archive-before-mutation: a reusable primitive, not tied to any one caller.

``docs/paper-day-recovery/open-questions.md`` ("Prerequisite work with no
substrate on main"): "No archive primitive exists anywhere in the engine" --
confirmed by grep (0 hits for ``archive``/``shutil.copy``/``shutil.move``
under ``engine/src``) before this module was written. Nothing in the engine
today preserves a copy of durable state before overwriting it: not
``gate.json``, not ``session.lock``, not ``scheduler.pid``/``scheduler.claim``,
not the execution-outbox state file.

This module fills that one gap only. It has no opinion about *when* to
archive or what happens after -- callers (e.g. a future recovery verb) decide
that. What it guarantees:

* the archived bytes are byte-identical to the source at the moment of the
  call (read once, hashed, written once);
* the archive is a **copy**, never a move -- the source is untouched, so the
  caller can still fail *after* archiving and *before* mutating without
  having destroyed anything;
* the write is atomic (private temp file, fssynced, then published with
  ``os.replace``) so a crash mid-archive never leaves a torn file that later
  code could mistake for a genuine archived record;
* concurrent callers archiving the same source never collide, even when they
  share the same ``now`` -- every archive filename carries a per-call unique
  suffix, not just a timestamp;
* a missing source is refused loudly (:class:`ArchiveError`), never treated
  as "nothing to archive, carry on" -- silently skipping the archive of a
  file that turns out not to exist is exactly the failure mode this
  primitive exists to close off.

This module does not read or write any paper-day state itself and imports
nothing from :mod:`engine.paperday`. It is a leaf utility.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["ArchiveError", "ArchiveManifest", "archive_before_mutation"]


class ArchiveError(RuntimeError):
    """The source could not be archived. The caller must not mutate it."""


@dataclass(frozen=True)
class ArchiveManifest:
    """A durable record of one archive-before-mutation call."""

    original_path: Path
    archive_path: Path
    manifest_path: Path
    sha256: str
    reason: str
    archived_at: dt.datetime

    def to_record(self) -> dict[str, Any]:
        return {
            "original_path": str(self.original_path),
            "archive_path": str(self.archive_path),
            "sha256": self.sha256,
            "reason": self.reason,
            "archived_at": self.archived_at.isoformat(),
        }


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        handle, name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(name)
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def archive_before_mutation(
    source: Path,
    *,
    archive_dir: Path,
    reason: str,
    now: dt.datetime,
) -> ArchiveManifest:
    """Copy ``source``'s current bytes into ``archive_dir`` before it is mutated.

    Safe to call before any mutation of ``gate.json``, ``session.lock``,
    ``scheduler.pid``/``scheduler.claim``, or execution-outbox state.

    Never moves or deletes ``source`` -- that decision belongs to the caller,
    made separately, after this returns successfully. Refuses (raises
    :class:`ArchiveError`) rather than silently doing nothing when ``source``
    cannot be read, so a caller that gates mutation on this call never
    mistakes "there was nothing to archive" for "archiving happened".

    Returns an :class:`ArchiveManifest` describing what was archived, and has
    already persisted that manifest to disk next to the archive (JSON,
    written the same atomic way) so the evidence outlives the caller's
    process.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("archive reason must be non-empty")

    try:
        content = source.read_bytes()
    except OSError as exc:
        raise ArchiveError(
            f"cannot archive {source}: source is unreadable or missing ({exc})"
        ) from exc

    digest = hashlib.sha256(content).hexdigest()
    timestamp = now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    # A timestamp alone cannot disambiguate two concurrent callers sharing
    # the same `now` (the common case in tests, and a real possibility for
    # two lanes racing to archive gate.json within the same instant), so
    # every call gets its own unguessable suffix regardless of clock
    # resolution.
    unique = uuid.uuid4().hex[:12]
    archive_name = f"{source.name}.{timestamp}.{unique}.archive"
    archive_path = Path(archive_dir) / archive_name
    manifest_path = Path(archive_dir) / f"{archive_name}.manifest.json"

    _atomic_write_bytes(archive_path, content)

    manifest = ArchiveManifest(
        original_path=source,
        archive_path=archive_path,
        manifest_path=manifest_path,
        sha256=digest,
        reason=reason,
        archived_at=now,
    )
    _atomic_write_bytes(
        manifest_path,
        json.dumps(manifest.to_record(), indent=2, sort_keys=True).encode("utf-8"),
    )
    return manifest
