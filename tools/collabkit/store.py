"""The handoff state machine: ``pending/ -> claimed/ -> done/ -> archive/``.

The directory a file sits in *is* its state. There is no index, no database and
no daemon holding state in memory -- which is exactly what makes the system
crash-safe and resumable, and it is why every transition here is a single
``os.rename``.

Why the transition is rename-under-a-lock, and not rename alone
---------------------------------------------------------------
``os.rename`` moves the file atomically -- a reader never sees a handoff in two
states or in none. That part is solid on every platform.

What it does **not** reliably provide is *exclusion*, and this was measured
rather than assumed. On Windows, eight threads in one process calling
``os.rename`` on the same source concurrently all returned success:

    same-thread, three calls in a row      -> OK, FileNotFoundError, FileNotFoundError
    threads serialized by a mutex          -> OK, FileNotFoundError, FileNotFoundError
    threads released from a barrier        -> OK, OK, OK          <-- no exclusion
    separate processes                     -> 1 x OK, 7 x FileNotFoundError

So "the loser gets ENOENT" holds across processes -- the normal deployment, two
agent sessions -- but silently fails for concurrent threads in one process. A
safety property that holds only in the configuration you happened to test is not
a safety property.

``O_EXCL`` file creation *was* measured exclusive in both shapes (1 winner, 7
``FileExistsError``, threads and processes alike), so the claim race is decided
by :class:`~collabkit.locking.FileLock` -- which is built on ``O_EXCL`` -- and
the rename is performed underneath it, after re-checking the source still
exists. The lock is per-handoff, so unrelated handoffs never serialize.

If you are tempted to delete the lock and go back to a bare rename: run
``tests/test_store.py``'s concurrent-claim test on Windows first.

Bookkeeping (``claimed_by``, timestamps) is written *after* the rename. If the
process dies in between, the file is in ``claimed/`` without an owner stamp --
visibly odd, trivially repairable, and never a double-claim. The opposite order
would be: stamp an owner, then fail to move it, and leave a handoff that lies
about who has it.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import time
from pathlib import Path
from typing import Iterable, Iterator

from . import ids, seats
from .atomic import atomic_write_text, ensure_dir
from .errors import CollabKitError, ConflictError, NotFoundError, ValidationError
from .locking import FAST_MAX_POLL_INTERVAL, FAST_POLL_INTERVAL, FileLock
from .log import EventLog
from .model import Handoff, normalize_priority
from .paths import STATES, CollabPaths
from .timeutil import iso

# A transition is a rename: microseconds. A 10s wait is therefore never normal
# contention -- it means a process died holding the lock, which the TTL clears.
TRANSITION_LOCK_TIMEOUT = 10.0
TRANSITION_LOCK_TTL = 60.0

# How long to keep retrying a rename that is blocked by an open reader (a
# watcher mid-parse, an editor, an AV scanner). Readers hold the file for
# microseconds; anything past this is a genuine problem worth reporting.
RENAME_RETRY_SECONDS = 3.0

# Forward-only, and only to an adjacent-or-later state. pending -> done is
# allowed because a builder who resolves something without claiming it should
# not have to perform a two-step ritual to say so; pending -> archive is not,
# because skipping done would hide work that was never actually reviewed.
_ALLOWED_TRANSITIONS = {
    "pending": {"claimed", "done"},
    "claimed": {"done"},
    "done": {"archive"},
    "archive": set(),
}


class HandoffStore:
    """CRUD + lifecycle over one collab's ``handoffs/`` tree."""

    def __init__(self, paths: CollabPaths, *, collab: str = "") -> None:
        self.paths = paths
        self.collab = collab or paths.name
        self.events = EventLog(paths.event_log)

    # -- creation --------------------------------------------------------

    def create(
        self,
        *,
        to: str,
        sender: str,
        title: str,
        body: str = "",
        priority: str = "normal",
        tags: Iterable[str] | None = None,
        thread: str | None = None,
    ) -> Handoff:
        """Write a new handoff into ``pending/``.

        Raises ValidationError on empty title/recipient rather than creating a
        handoff nobody can route -- the watchers filter on ``to:``, so a blank
        recipient is a message that is never delivered.
        """
        if not (title or "").strip():
            raise ValidationError("a handoff needs a --title")
        recipient = seats.canonical(to)
        if not recipient:
            raise ValidationError(
                "a handoff needs a --to recipient",
                hint=f"e.g. --to reviewer (aliases: {', '.join(sorted(set(seats.SEATS)))})",
            )

        self.paths.ensure()
        handoff = Handoff(
            id=ids.new_id(title),
            to=recipient,
            sender=seats.canonical(sender, default=seats.counterpart(recipient)),
            title=title.strip(),
            priority=normalize_priority(priority),
            status="pending",
            created=iso(),
            collab=self.collab,
            thread=thread or None,
            tags=[str(tag).strip() for tag in (tags or []) if str(tag).strip()],
            body=body or "",
        )

        path = self.paths.pending / f"{handoff.id}.md"
        for _attempt in range(5):
            try:
                _create_exclusive(path, handoff.render())
                break
            except FileExistsError:
                # Astronomically unlikely (same second + same 24 bits), but a
                # silent overwrite would destroy someone's request, so remint.
                handoff.id = ids.new_id(title)
                path = self.paths.pending / f"{handoff.id}.md"
        else:  # pragma: no cover - would require 5 consecutive collisions
            raise ConflictError("could not mint a unique handoff id after 5 attempts")

        handoff.path = path
        self.events.write("create", handoff.id, to=handoff.to, sender=handoff.sender,
                          priority=handoff.priority, title=handoff.title)
        return handoff

    def reply(
        self,
        parent_id: str,
        *,
        title: str,
        body: str = "",
        sender: str = "",
        priority: str | None = None,
        tags: Iterable[str] | None = None,
        close_parent: bool = True,
    ) -> tuple[Handoff, Handoff | None]:
        """Answer a handoff: new handoff back to its sender, parent marked done.

        Returns ``(reply, closed_parent_or_None)``. Threading uses the parent's
        thread id when it has one, so a five-turn exchange shares a single
        thread rather than forming a chain that has to be walked.
        """
        parent = self.find(parent_id)
        responder = seats.canonical(sender) or parent.to
        reply = self.create(
            to=parent.sender or seats.counterpart(responder),
            sender=responder,
            title=title,
            body=body,
            priority=priority or parent.priority,
            tags=tags if tags is not None else parent.tags,
            thread=parent.thread or parent.id,
        )
        closed = None
        if close_parent and parent.status in ("pending", "claimed"):
            with contextlib.suppress(ConflictError, NotFoundError):
                closed = self.complete(parent.id, note=f"answered by {reply.id}", by=responder)
        return reply, closed

    # -- reading ---------------------------------------------------------

    def iter_state(self, state: str) -> Iterator[Handoff]:
        """Yield every handoff in one state directory.

        Unparseable files are skipped, not fatal: one corrupt handoff must not
        make ``handoff list`` unusable for the rest of the queue.
        """
        directory = self.paths.state_dir(state)
        if not directory.is_dir():
            return
        pattern = "**/*.md" if state == "archive" else "*.md"
        for path in sorted(directory.glob(pattern)):
            if path.name.startswith("."):
                continue  # in-flight atomic-write temp file
            try:
                yield Handoff.load(path, status=state)
            except (OSError, ValidationError):
                continue

    def list(
        self,
        states: Iterable[str] = ("pending",),
        *,
        to: str | None = None,
        sender: str | None = None,
        priority: str | None = None,
        tag: str | None = None,
    ) -> list[Handoff]:
        wanted = [state for state in states if state in STATES]
        found = [
            handoff
            for state in wanted
            for handoff in self.iter_state(state)
            if handoff.matches(to=to, sender=sender, priority=priority, tag=tag)
        ]
        found.sort(key=Handoff.sort_key)
        return found

    def find(self, handoff_id: str, *, states: Iterable[str] = STATES) -> Handoff:
        """Resolve an exact id or an unambiguous prefix.

        Prefix resolution exists because these ids are long and typed by hand;
        ambiguity is an error rather than a coin flip, since picking the wrong
        handoff silently is far worse than making the caller type four more
        characters.
        """
        needle = (handoff_id or "").strip()
        if not needle:
            raise ValidationError("a handoff id is required")

        # Exact hit first: a full id must never be treated as a prefix.
        for state in states:
            path = self.paths.state_dir(state)
            candidate = path / f"{needle}.md"
            if state == "archive":
                year, month = ids.archive_partition(needle)
                candidate = path / year / month / f"{needle}.md"
            if candidate.is_file():
                return Handoff.load(candidate, status=state)

        matches = [
            handoff
            for state in states
            for handoff in self.iter_state(state)
            if handoff.id.startswith(needle)
        ]
        if not matches:
            raise NotFoundError(
                f"no handoff matching {needle!r} in {self.paths.root}",
                hint="run 'handoff <name> list --all' to see what exists",
            )
        if len(matches) > 1:
            listing = "\n  ".join(f"{item.id}  [{item.status}]  {item.title}" for item in matches[:8])
            raise ConflictError(
                f"{needle!r} is ambiguous -- {len(matches)} handoffs match:\n  {listing}",
                hint="use more characters of the id",
            )
        return matches[0]

    def counts(self) -> dict[str, int]:
        return {state: sum(1 for _ in self.iter_state(state)) for state in STATES}

    def oldest_pending(self) -> Handoff | None:
        pending = self.list(("pending",))
        return pending[0] if pending else None

    # -- transitions -----------------------------------------------------

    def claim(self, handoff_id: str, *, by: str = "") -> Handoff:
        """Move ``pending/ -> claimed/``. Exactly one caller can win."""
        handoff = self.find(handoff_id, states=("pending", "claimed", "done", "archive"))
        if handoff.status != "pending":
            raise ConflictError(
                f"{handoff.id} is already {handoff.status}"
                + (f" (claimed by {handoff.claimed_by})" if handoff.claimed_by else ""),
                hint="only pending handoffs can be claimed",
            )
        claimer = seats.canonical(by) or handoff.to
        moved = self._transition(handoff, "claimed")
        moved.claimed_by = claimer
        moved.claimed_at = iso()
        self._rewrite(moved)
        self.events.write("claim", moved.id, by=claimer)
        return moved

    def complete(self, handoff_id: str, *, note: str = "", by: str = "") -> Handoff:
        """Move ``pending|claimed -> done/``."""
        handoff = self.find(handoff_id, states=("pending", "claimed", "done", "archive"))
        if handoff.status == "done":
            raise ConflictError(f"{handoff.id} is already done")
        if handoff.status == "archive":
            raise ConflictError(f"{handoff.id} is archived and cannot be reopened")
        moved = self._transition(handoff, "done")
        moved.done_at = iso()
        if note:
            moved.note = note
        if by and not moved.claimed_by:
            moved.claimed_by = seats.canonical(by)
        self._rewrite(moved)
        self.events.write("done", moved.id, by=seats.canonical(by) or moved.claimed_by or "", note=note)
        return moved

    def archive(self, handoff_id: str) -> Handoff:
        """Move ``done/ -> archive/YYYY/MM/``."""
        handoff = self.find(handoff_id, states=("done", "archive", "claimed", "pending"))
        if handoff.status == "archive":
            raise ConflictError(f"{handoff.id} is already archived")
        if handoff.status != "done":
            raise ConflictError(
                f"{handoff.id} is {handoff.status}; only done handoffs can be archived",
                hint=f"run 'done {handoff.id}' first",
            )
        moved = self._transition(handoff, "archive")
        self._rewrite(moved)
        self.events.write("archive", moved.id)
        return moved

    def archive_all_done(self) -> list[Handoff]:
        """Sweep every finished handoff into the archive. Best-effort per item.

        One failure (a file another process is mid-move on) must not abort the
        sweep of the other thirty.
        """
        archived: list[Handoff] = []
        for handoff in list(self.iter_state("done")):
            try:
                archived.append(self.archive(handoff.id))
            except (ConflictError, NotFoundError, OSError):
                continue
        return archived

    # -- internals -------------------------------------------------------

    def _destination(self, handoff: Handoff, target: str) -> Path:
        directory = self.paths.state_dir(target)
        if target == "archive":
            year, month = ids.archive_partition(handoff.id)
            directory = directory / year / month
        return directory / f"{handoff.id}.md"

    def _transition(self, handoff: Handoff, target: str) -> Handoff:
        """The exclusive move. Everything else in this class is bookkeeping.

        See the module docstring for why this holds a lock rather than trusting
        ``os.rename`` to decide the race on its own.
        """
        if target not in _ALLOWED_TRANSITIONS.get(handoff.status, set()):
            raise ConflictError(
                f"cannot move {handoff.id} from {handoff.status} to {target}"
            )
        source = handoff.path
        if source is None:
            raise NotFoundError(f"{handoff.id} has no path on disk")

        destination = self._destination(handoff, target)
        ensure_dir(destination.parent)
        ensure_dir(self.paths.locks)

        lost = ConflictError(
            f"{handoff.id} was already moved out of {handoff.status}/ by someone else",
            hint="re-run 'list' to see its current state",
        )

        with FileLock(
            self.paths.lock(_lock_name(handoff.id)),
            timeout=TRANSITION_LOCK_TIMEOUT,
            ttl=TRANSITION_LOCK_TTL,
            purpose=f"handoff {handoff.id}",
            poll_interval=FAST_POLL_INTERVAL,
            max_poll_interval=FAST_MAX_POLL_INTERVAL,
        ):
            # Re-check *inside* the lock. Between find() and here, another seat
            # may have claimed it -- and that check is only meaningful once we
            # hold the lock that stops it changing again underneath us.
            if not source.is_file():
                raise lost
            try:
                _rename_with_retry(source, destination)
            except FileNotFoundError:
                raise lost from None
            except FileExistsError:
                # Windows rename refuses an existing destination; POSIX would
                # have clobbered it. Refusing on both is the safe reading.
                raise ConflictError(
                    f"{handoff.id} already exists in {target}/",
                    hint="resolve the duplicate by hand before retrying",
                ) from None
            except OSError as exc:
                raise CollabKitError(
                    f"could not move {handoff.id} into {target}/: {exc}",
                    hint=(
                        "something is holding the file open. Stop any editor or "
                        "backup/AV scanner on the collab directory and retry."
                    ),
                ) from None

        handoff.path = destination
        handoff.status = target
        return handoff

    def _rewrite(self, handoff: Handoff) -> None:
        """Persist bookkeeping fields in place.

        Failure here is logged and swallowed: the rename already happened and is
        the authoritative state change. Turning a cosmetic write failure into a
        command failure would tempt the caller to retry a transition that has
        already succeeded.
        """
        if handoff.path is None:
            return
        try:
            atomic_write_text(handoff.path, handoff.render())
        except OSError as exc:  # pragma: no cover - disk-full / permissions
            self.events.write("rewrite-failed", handoff.id, error=str(exc))


def _rename_with_retry(source: Path, destination: Path) -> None:
    """``os.rename``, retrying while the source is transiently locked.

    This is not a rare-crash workaround; it is the normal case on Windows. Both
    watchers poll ``pending/`` every couple of seconds and *open every file* to
    parse it, and Windows refuses to rename a file that anyone has open --
    ``PermissionError`` / WinError 32. So a claim issued at the wrong instant
    would fail with an opaque sharing violation, and the user would see a
    handoff that "sometimes cannot be claimed".

    A reader holds the file for microseconds, so a short backoff makes the
    collision vanish. ``FileNotFoundError`` and ``FileExistsError`` are *not*
    retried: those are real answers about who won the race, and retrying them
    would turn a decided race into a stall.
    """
    deadline = time.monotonic() + RENAME_RETRY_SECONDS
    delay = 0.002
    while True:
        try:
            os.rename(source, destination)
            return
        except (FileNotFoundError, FileExistsError):
            raise
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.05)


def _lock_name(handoff_id: str) -> str:
    """A filesystem-safe lock filename for one handoff.

    Well-formed ids are already ``[0-9a-zA-Z-]`` and are used verbatim so the
    lock file is recognisable in ``logs/locks/``. Anything else -- a hand-edited
    or hostile ``id:`` field -- is hashed, because that string would otherwise
    be concatenated into a path.
    """
    if ids.is_valid(handoff_id):
        return f"handoff-{handoff_id}"
    digest = hashlib.sha256((handoff_id or "").encode("utf-8")).hexdigest()[:32]
    return f"handoff-x{digest}"


def _create_exclusive(path: Path, text: str) -> None:
    """Create ``path`` with ``text``, failing if it already exists.

    Two properties are needed at once: refuse to clobber an existing id, and
    never let a reader observe a partial file. A hard link from a fully-written
    temp file gives both -- the name appears already complete. Filesystems
    without hard links (some network mounts, FAT) fall back to reserving the
    name with ``O_EXCL`` and then replacing it, which briefly exposes an empty
    file; watchers tolerate that by refusing to mark unparseable files as seen.
    """
    ensure_dir(path.parent)
    temp = path.parent / f".{path.name}.{os.getpid()}.new"
    try:
        atomic_write_text(temp, text)
        try:
            os.link(temp, path)
        except FileExistsError:
            raise
        except (OSError, AttributeError, NotImplementedError):
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            atomic_write_text(path, text)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()
