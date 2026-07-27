"""Filesystem layout: where the kit is, where your data is, what lives where.

Two roots, deliberately separate:

``KIT_DIR``      the clone -- code, templates, skill. Read-only at runtime.
``COLLAB_HOME``  your data -- registry, collabs, queues, logs. Defaults to
                 ``KIT_DIR`` (so a fresh clone just works) and is overridable by
                 env so the kit can be reinstalled or moved without touching a
                 single byte of state.

Both are env-overridable because ARCHITECTURE.md requires scripts to
"self-locate ... so it runs under any user/path". Nothing here hardcodes a
home directory, a username, or a path separator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .atomic import ensure_dir
from .errors import NotFoundError
from .slug import validate_name

ENV_KIT_DIR = "KIT_DIR"
ENV_COLLAB_HOME = "COLLAB_HOME"
ENV_HANDOFF_ROOT = "HANDOFF_ROOT"

REGISTRY_FILENAME = "collabs.json"
REGISTRY_EXAMPLE = "collabs.json.example"

# Handoff lifecycle states, in order. The tuple *is* the state machine: the
# store only permits transitions to an adjacent later state.
STATES = ("pending", "claimed", "done", "archive")


def kit_dir() -> Path:
    """Absolute path of the collab-kit clone.

    Derived from this file's location (``<kit>/tools/collabkit/paths.py``), so
    it is correct no matter where the kit was cloned or which symlink invoked
    it -- ``Path.resolve()`` follows the symlink back to the real file.
    """
    override = os.environ.get(ENV_KIT_DIR)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def collab_home() -> Path:
    """Absolute path of the data root. Defaults to :func:`kit_dir`."""
    override = os.environ.get(ENV_COLLAB_HOME)
    if override:
        return Path(override).expanduser().resolve()
    return kit_dir()


def handoff_root_env() -> Path | None:
    """``$HANDOFF_ROOT`` if set -- the single-collab scoping used by watchers."""
    override = os.environ.get(ENV_HANDOFF_ROOT)
    if not override:
        return None
    return Path(override).expanduser().resolve()


@dataclass(frozen=True)
class HomePaths:
    """Directory layout of ``$COLLAB_HOME``."""

    root: Path

    @classmethod
    def discover(cls) -> "HomePaths":
        return cls(collab_home())

    @property
    def registry(self) -> Path:
        return self.root / REGISTRY_FILENAME

    @property
    def registry_example(self) -> Path:
        return kit_dir() / REGISTRY_EXAMPLE

    @property
    def outbox(self) -> Path:
        """Agent -> phone. Files here are forwarded, then archived on delivery."""
        return self.root / "outbox"

    @property
    def outbox_archive(self) -> Path:
        return self.outbox / "archive"

    @property
    def outbox_failed(self) -> Path:
        """Messages the bridge gave up on. Kept, never deleted."""
        return self.outbox / "failed"

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def inbox_live(self) -> Path:
        """Phone -> agent, per project: ``inbox/live/<project>/from-user-*.md``."""
        return self.inbox / "live"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def locks(self) -> Path:
        return self.logs / "locks"

    @property
    def state(self) -> Path:
        """Watcher cursors and bridge offsets. Caches -- safe to delete."""
        return self.logs / "state"

    def inbox_for(self, project: str) -> Path:
        """Inbox directory for one project, name-validated.

        The name reaching here may have come from a Telegram message, so it is
        re-validated at the point of path construction rather than trusted from
        an earlier check.
        """
        return self.inbox_live / validate_name(project, kind="project name")

    def collab_root(self, name: str) -> Path:
        """Default root for a collab: ``$COLLAB_HOME/<name>``."""
        return self.root / validate_name(name)

    def ensure(self) -> "HomePaths":
        for path in (self.outbox, self.outbox_archive, self.inbox_live, self.logs, self.locks, self.state):
            ensure_dir(path)
        return self

    def lock(self, name: str) -> Path:
        return self.locks / f"{name}.lock"


@dataclass(frozen=True)
class CollabPaths:
    """Directory layout of a single collab -- the isolation unit.

    Every collab owns its handoffs, logs, locks and context. Nothing here
    reaches outside ``root``, which is what makes concurrent collabs unable to
    race or cross-contaminate.
    """

    root: Path
    name: str = ""

    @classmethod
    def at(cls, root: Path | str, name: str = "") -> "CollabPaths":
        path = Path(root).expanduser()
        # resolve() on a non-existent path is fine on 3.6+ (strict=False).
        return cls(path.resolve(), name or path.name)

    # -- handoff queues --------------------------------------------------

    @property
    def handoffs(self) -> Path:
        return self.root / "handoffs"

    @property
    def pending(self) -> Path:
        return self.handoffs / "pending"

    @property
    def claimed(self) -> Path:
        return self.handoffs / "claimed"

    @property
    def done(self) -> Path:
        return self.handoffs / "done"

    @property
    def archive(self) -> Path:
        return self.handoffs / "archive"

    def state_dir(self, state: str) -> Path:
        if state not in STATES:
            raise NotFoundError(
                f"unknown handoff state {state!r}",
                hint=f"expected one of: {', '.join(STATES)}",
            )
        return getattr(self, state)

    # -- everything else -------------------------------------------------

    @property
    def context(self) -> Path:
        return self.root / "context"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def locks(self) -> Path:
        return self.logs / "locks"

    @property
    def state(self) -> Path:
        return self.logs / "state"

    @property
    def repo(self) -> Path:
        """Where ``newproject --repo`` clones the target repository."""
        return self.root / "repo"

    @property
    def protocol(self) -> Path:
        return self.root / "PROTOCOL.md"

    @property
    def briefing(self) -> Path:
        return self.root / "REVIEWER-BRIEFING.md"

    @property
    def kickoff(self) -> Path:
        return self.root / "KICKOFF.md"

    @property
    def idea(self) -> Path:
        return self.context / "IDEA.md"

    @property
    def meta_file(self) -> Path:
        """Per-collab metadata written by ``newproject`` / ``handoff new``."""
        return self.root / "collab.json"

    @property
    def event_log(self) -> Path:
        return self.logs / "events.jsonl"

    def lock(self, name: str) -> Path:
        return self.locks / f"{name}.lock"

    # -- lifecycle -------------------------------------------------------

    def ensure(self) -> "CollabPaths":
        """Create the full skeleton. Idempotent."""
        for path in (
            self.handoffs,
            self.pending,
            self.claimed,
            self.done,
            self.archive,
            self.context,
            self.logs,
            self.locks,
            self.state,
        ):
            ensure_dir(path)
        return self

    def exists(self) -> bool:
        return self.handoffs.is_dir()

    def require(self) -> "CollabPaths":
        if not self.exists():
            raise NotFoundError(
                f"no collab at {self.root}",
                hint="run 'handoff new <name>' or 'newproject <name> --repo <git-url>' first",
            )
        return self
