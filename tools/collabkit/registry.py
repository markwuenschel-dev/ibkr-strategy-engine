"""``collabs.json`` -- the name -> root registry.

This is the only piece of *shared mutable* state in the kit. Everything else is
partitioned per collab, so this file is the one place two concurrent commands
can genuinely clobber each other. It therefore gets the full treatment: an
advisory lock around every read-modify-write, an atomic replace on save, and a
backup of the previous good copy before each write.

The registry is a convenience index, not the source of truth. A collab is real
because ``<root>/handoffs/`` exists on disk; the registry only records where to
look. That ordering matters for recovery: if this file is lost, ``handoff
register`` puts an entry back and nothing was destroyed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .atomic import atomic_write_json, ensure_dir, read_json
from .errors import ConflictError, NotFoundError, ValidationError
from .locking import FileLock
from .paths import CollabPaths, HomePaths
from .slug import validate_name
from .timeutil import iso

SCHEMA_VERSION = 1


@dataclass
class CollabEntry:
    """One registered collab."""

    name: str
    root: Path
    repo: str = ""
    repo_path: str = ""
    reviewer: str = ""
    created: str = ""
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, name: str, payload: Any, *, home: Path) -> "CollabEntry":
        """Parse an entry, accepting both the object form and a bare path string.

        The bare-string form (``{"demo": "/path/to/demo"}``) is what people
        hand-write when they edit this file themselves. Accepting it costs three
        lines and avoids a confusing failure on a perfectly reasonable edit.
        """
        if isinstance(payload, str):
            payload = {"root": payload}
        if not isinstance(payload, dict):
            raise ValidationError(f"registry entry {name!r} is not an object or a path")

        root = str(payload.get("root") or "").strip()
        if not root:
            raise ValidationError(f"registry entry {name!r} has no 'root'")
        resolved = Path(root).expanduser()
        if not resolved.is_absolute():
            # Relative roots are resolved against $COLLAB_HOME so the registry
            # stays portable when the whole tree is moved or synced.
            resolved = (home / resolved)

        known = {"root", "repo", "repo_path", "reviewer", "created", "notes"}
        return cls(
            name=name,
            root=resolved.resolve(),
            repo=str(payload.get("repo") or ""),
            repo_path=str(payload.get("repo_path") or ""),
            reviewer=str(payload.get("reviewer") or ""),
            created=str(payload.get("created") or ""),
            notes=str(payload.get("notes") or ""),
            extra={key: value for key, value in payload.items() if key not in known},
        )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"root": str(self.root)}
        for key in ("repo", "repo_path", "reviewer", "created", "notes"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        payload.update(self.extra)
        return payload

    @property
    def paths(self) -> CollabPaths:
        return CollabPaths.at(self.root, self.name)

    @property
    def exists(self) -> bool:
        return self.paths.exists()


class Registry:
    """Read/write access to ``$COLLAB_HOME/collabs.json``."""

    def __init__(self, home: HomePaths | None = None) -> None:
        self.home = home or HomePaths.discover()
        self.path = self.home.registry

    # -- reading ---------------------------------------------------------

    def load(self) -> dict[str, CollabEntry]:
        """Parse the registry. A missing file is an empty registry, not an error.

        A single malformed entry is skipped rather than fatal -- one bad hand
        edit should not lock you out of the other nine collabs. A malformed
        *file* is fatal, because silently continuing from an empty dict would
        make ``register`` overwrite everything.
        """
        raw = read_json(self.path, default=_MISSING)
        if raw is _MISSING:
            if self.path.exists():
                raise ValidationError(
                    f"{self.path} is not valid JSON",
                    hint="fix it by hand, or move it aside and re-run 'handoff register'",
                )
            return {}

        collabs = raw.get("collabs", raw) if isinstance(raw, dict) else {}
        if not isinstance(collabs, dict):
            raise ValidationError(f"{self.path}: 'collabs' must be an object")

        entries: dict[str, CollabEntry] = {}
        for name, payload in collabs.items():
            if name in ("version", "collabs"):
                continue
            try:
                entries[name] = CollabEntry.from_json(str(name), payload, home=self.home.root)
            except ValidationError:
                continue
        return entries

    def get(self, name: str) -> CollabEntry | None:
        return self.load().get(name)

    def require(self, name: str) -> CollabEntry:
        entry = self.get(name)
        if entry is None:
            known = sorted(self.load())
            raise NotFoundError(
                f"unknown collab {name!r}",
                hint=(
                    f"registered: {', '.join(known)}" if known
                    else "none registered yet -- try 'handoff new <name>'"
                ),
            )
        return entry

    def names(self) -> list[str]:
        return sorted(self.load())

    def __iter__(self) -> Iterator[CollabEntry]:
        for name in self.names():
            entry = self.get(name)
            if entry is not None:
                yield entry

    # -- writing ---------------------------------------------------------

    def register(
        self,
        name: str,
        root: Path | str,
        *,
        repo: str = "",
        repo_path: str = "",
        reviewer: str = "",
        notes: str = "",
        force: bool = False,
    ) -> CollabEntry:
        """Add or update an entry. Refuses to silently repoint an existing name.

        Repointing is how you lose a collab: the handoffs stay on disk but every
        command starts looking somewhere else. ``--force`` makes it explicit.
        """
        validate_name(name)
        resolved = Path(root).expanduser().resolve()

        with self._lock():
            entries = self.load()
            existing = entries.get(name)
            if existing and existing.root != resolved and not force:
                raise ConflictError(
                    f"collab {name!r} is already registered at {existing.root}",
                    hint="pass --force to repoint it, or pick a different name",
                )
            entry = CollabEntry(
                name=name,
                root=resolved,
                repo=repo or (existing.repo if existing else ""),
                repo_path=repo_path or (existing.repo_path if existing else ""),
                reviewer=reviewer or (existing.reviewer if existing else ""),
                created=(existing.created if existing else "") or iso(),
                notes=notes or (existing.notes if existing else ""),
                extra=dict(existing.extra) if existing else {},
            )
            entries[name] = entry
            self._save(entries)
        return entry

    def unregister(self, name: str) -> CollabEntry:
        """Remove an entry. Never touches the collab's files on disk."""
        with self._lock():
            entries = self.load()
            entry = entries.pop(name, None)
            if entry is None:
                raise NotFoundError(f"collab {name!r} is not registered")
            self._save(entries)
        return entry

    def prune(self) -> list[str]:
        """Drop entries whose root no longer holds a collab. Returns the names."""
        removed: list[str] = []
        with self._lock():
            entries = self.load()
            for name, entry in list(entries.items()):
                if not entry.exists:
                    removed.append(name)
                    del entries[name]
            if removed:
                self._save(entries)
        return removed

    # -- internals -------------------------------------------------------

    def _lock(self) -> FileLock:
        ensure_dir(self.home.locks)
        return FileLock(self.home.lock("registry"), timeout=15.0, purpose="registry")

    def _save(self, entries: dict[str, CollabEntry]) -> None:
        ensure_dir(self.path.parent)
        if self.path.is_file():
            # One generation of backup. Cheap insurance against a bug in this
            # file turning into a lost registry.
            try:
                backup = self.path.with_suffix(".json.bak")
                backup.write_text(
                    self.path.read_text(encoding="utf-8"), encoding="utf-8"
                )
            except OSError:
                pass
        payload = {
            "version": SCHEMA_VERSION,
            "collabs": {name: entry.to_json() for name, entry in sorted(entries.items())},
        }
        atomic_write_json(self.path, payload)


class _Missing:
    """Sentinel distinguishing 'absent' from 'present but null'."""

    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


_MISSING = _Missing()


def example_payload() -> str:
    """Contents of ``collabs.json.example``, kept in sync with the parser."""
    return json.dumps(
        {
            "version": SCHEMA_VERSION,
            "collabs": {
                "demo": {
                    "root": "/absolute/path/to/COLLAB_HOME/demo",
                    "repo": "git@github.com:you/your-repo.git",
                    "repo_path": "/absolute/path/to/COLLAB_HOME/demo/repo",
                    "reviewer": "grok",
                    "created": "2026-01-01T00:00:00Z",
                }
            },
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
