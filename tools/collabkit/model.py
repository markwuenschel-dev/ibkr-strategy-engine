"""The Handoff record -- one markdown file with YAML frontmatter.

The file on disk is the only source of truth. This class is a typed view over
it, not a cache: nothing is held in memory between commands, which is what makes
the whole system crash-safe and resumable.

One field is deliberately redundant: ``status`` is mirrored into the frontmatter
even though the containing directory already determines it. The directory wins
on read -- it is what the atomic rename actually moved. The mirrored copy exists
so an archived file, copied out of the tree and pasted into a chat, still says
what happened to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import frontmatter, seats
from .errors import ValidationError
from .timeutil import age_seconds, human_age, iso

PRIORITIES = ("low", "normal", "high", "urgent")
_PRIORITY_RANK = {name: index for index, name in enumerate(PRIORITIES)}

# Fixed leading field order so every handoff file opens the same way and
# `git diff` on one stays readable.
FIELD_ORDER = [
    "id",
    "to",
    "from",
    "title",
    "priority",
    "status",
    "created",
    "collab",
    "thread",
    "tags",
    "claimed_by",
    "claimed_at",
    "done_at",
    "note",
]

PRIORITY_MARK = {"urgent": "!!", "high": "!", "normal": " ", "low": "."}


def normalize_priority(raw: str | None, *, default: str = "normal") -> str:
    """Validate a priority, raising with the allowed set on a typo.

    Unlike seats, priorities are a closed set -- ``--priority hgh`` must fail
    rather than create an unsortable handoff nobody's filter will match.
    """
    text = (raw or "").strip().lower()
    if not text:
        return default
    if text not in _PRIORITY_RANK:
        raise ValidationError(
            f"unknown priority {raw!r}",
            hint=f"expected one of: {', '.join(PRIORITIES)}",
        )
    return text


@dataclass
class Handoff:
    """A single handoff, as read from (or about to be written to) disk."""

    id: str
    to: str
    sender: str
    title: str
    priority: str = "normal"
    status: str = "pending"
    created: str = ""
    collab: str = ""
    thread: str | None = None
    tags: list[str] = field(default_factory=list)
    claimed_by: str | None = None
    claimed_at: str | None = None
    done_at: str | None = None
    note: str | None = None
    body: str = ""
    path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_meta(
        cls,
        meta: dict[str, Any],
        body: str = "",
        *,
        path: Path | None = None,
        status: str | None = None,
    ) -> "Handoff":
        """Build from parsed frontmatter.

        Tolerant on read by design: a handoff written by a future version of the
        kit, or hand-edited by a human at 2am, still lists and shows. Unknown
        keys are preserved in ``extra`` and written back untouched, so a
        round-trip through ``claim`` never destroys a field this version does
        not understand.
        """
        known = set(FIELD_ORDER)
        tags = meta.get("tags")
        if isinstance(tags, str):
            tags = [part.strip() for part in tags.split(",") if part.strip()]
        elif isinstance(tags, (list, tuple)):
            tags = [str(item) for item in tags if item is not None]
        else:
            tags = []

        return cls(
            id=str(meta.get("id") or (path.stem if path else "")),
            to=seats.canonical(meta.get("to"), default=seats.BROADCAST),
            sender=seats.canonical(meta.get("from"), default=""),
            title=str(meta.get("title") or "(untitled)"),
            priority=str(meta.get("priority") or "normal").lower(),
            status=status or str(meta.get("status") or "pending"),
            created=str(meta.get("created") or ""),
            collab=str(meta.get("collab") or ""),
            thread=_opt_str(meta.get("thread")),
            tags=tags,
            claimed_by=_opt_str(meta.get("claimed_by")),
            claimed_at=_opt_str(meta.get("claimed_at")),
            done_at=_opt_str(meta.get("done_at")),
            note=_opt_str(meta.get("note")),
            body=body,
            path=path,
            extra={key: value for key, value in meta.items() if key not in known},
        )

    @classmethod
    def load(cls, path: Path, *, status: str | None = None) -> "Handoff":
        meta, body = frontmatter.parse_file(path)
        return cls.from_meta(meta, body, path=Path(path), status=status)

    # -- serialization ---------------------------------------------------

    def to_meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "id": self.id,
            "to": self.to,
            "from": self.sender,
            "title": self.title,
            "priority": self.priority,
            "status": self.status,
            "created": self.created or iso(),
        }
        if self.collab:
            meta["collab"] = self.collab
        if self.thread:
            meta["thread"] = self.thread
        if self.tags:
            meta["tags"] = list(self.tags)
        for key in ("claimed_by", "claimed_at", "done_at", "note"):
            value = getattr(self, key)
            if value:
                meta[key] = value
        meta.update(self.extra)
        return meta

    def render(self) -> str:
        return frontmatter.dumps(self.to_meta(), self.body, key_order=FIELD_ORDER)

    def to_json(self) -> dict[str, Any]:
        """JSON-safe dict for ``--json`` output and the watchers' event stream."""
        payload = dict(self.to_meta())
        payload["path"] = str(self.path) if self.path else None
        payload["age_seconds"] = age_seconds(self.created)
        payload["body"] = self.body
        return payload

    # -- derived ---------------------------------------------------------

    @property
    def priority_rank(self) -> int:
        """Higher is more urgent. Unknown priorities sort as ``normal``."""
        return _PRIORITY_RANK.get(self.priority, _PRIORITY_RANK["normal"])

    @property
    def age(self) -> str:
        return human_age(age_seconds(self.created))

    @property
    def mark(self) -> str:
        return PRIORITY_MARK.get(self.priority, " ")

    def sort_key(self) -> tuple[int, str]:
        """Most urgent first, then oldest first within a priority.

        Oldest-first inside a band is the whole point of a queue: a normal
        handoff must not starve because new normal handoffs keep arriving.
        """
        return (-self.priority_rank, self.created or self.id)

    def summary(self, *, width: int = 58) -> str:
        """One line for ``handoff list``."""
        from .console import ARROW, ELLIPSIS

        title = self.title if len(self.title) <= width else self.title[: width - 1] + ELLIPSIS
        return (
            f"{self.mark} {self.age:>4}  {seats.label(self.sender) or '?':<8} {ARROW} "
            f"{seats.label(self.to):<8}  {title}"
        )

    def matches(
        self,
        *,
        to: str | None = None,
        sender: str | None = None,
        priority: str | None = None,
        tag: str | None = None,
    ) -> bool:
        if to and not seats.matches(self.to, to):
            return False
        if sender and not seats.matches(self.sender, sender):
            return False
        if priority and self.priority != priority.lower():
            return False
        if tag and tag.lower() not in {item.lower() for item in self.tags}:
            return False
        return True


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
