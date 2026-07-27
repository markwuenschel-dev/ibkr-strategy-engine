"""Seats: who a handoff is *to* and *from*.

ARCHITECTURE.md defines exactly two agent seats plus the human:

``builder``   the builder/coordinator -- drives the work and talks to you.
``reviewer``  the independent reviewer -- reads the actual diff.
``user``      you, over the phone bridge.

The kit is agent-agnostic, so the seat is a *role*, not a product. But people
and scripts naturally write the product name -- ``--to grok``, ``--to claude``
-- and a watcher that only matched the canonical spelling would silently drop
those handoffs on the floor. So every write normalizes to the canonical role and
every read matches through the alias table.

Aliases are role-neutral where they can be, with one pragmatic exception: the
watcher scripts named in ARCHITECTURE.md are ``watch-for-claude-handoffs.py``
(builder side) and ``watch-for-grok-handoffs.py`` (reviewer side), so ``claude``
maps to builder and ``grok`` maps to reviewer. Override per collab with
``COLLAB_SEAT_ALIASES`` when you run the pairing the other way round.
"""

from __future__ import annotations

import os

BUILDER = "builder"
REVIEWER = "reviewer"
USER = "user"
BROADCAST = "all"

SEATS = (BUILDER, REVIEWER, USER)

_BASE_ALIASES = {
    BUILDER: BUILDER,
    "build": BUILDER,
    "coordinator": BUILDER,
    "coord": BUILDER,
    "claude": BUILDER,
    "dev": BUILDER,
    "b": BUILDER,
    REVIEWER: REVIEWER,
    "review": REVIEWER,
    "reviewer-agent": REVIEWER,
    "critic": REVIEWER,
    "grok": REVIEWER,
    "r": REVIEWER,
    USER: USER,
    "human": USER,
    "me": USER,
    "you": USER,
    "phone": USER,
    "telegram": USER,
    BROADCAST: BROADCAST,
    "any": BROADCAST,
    "both": BROADCAST,
    "*": BROADCAST,
}

ENV_ALIASES = "COLLAB_SEAT_ALIASES"


def _alias_table() -> dict[str, str]:
    """Alias map, extended by ``COLLAB_SEAT_ALIASES=name=seat,name=seat``.

    Lets a project that runs Claude as the *reviewer* say so, instead of
    fighting the default mapping.
    """
    table = dict(_BASE_ALIASES)
    raw = os.environ.get(ENV_ALIASES, "")
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        alias, _, target = pair.partition("=")
        alias = alias.strip().lower()
        target = target.strip().lower()
        if alias and target in SEATS + (BROADCAST,):
            table[alias] = target
    return table


def canonical(raw: str | None, *, default: str | None = None) -> str:
    """Map any spelling of a seat to its canonical name.

    Unknown values pass through lowercased rather than raising: a project may
    legitimately invent a third seat (``qa``), and refusing to route its mail
    would be worse than routing it under its own name.
    """
    text = (raw or "").strip().lower()
    if not text:
        return default or ""
    return _alias_table().get(text, text)


def matches(target: str | None, seat: str) -> bool:
    """Whether a handoff addressed to ``target`` belongs to ``seat``.

    ``all`` on either side matches everything -- that is how a broadcast reaches
    both watchers, and how ``--to all`` on a filter means "show me everything".
    """
    left = canonical(target)
    right = canonical(seat)
    if not left or not right:
        return False
    if BROADCAST in (left, right):
        return True
    return left == right


def counterpart(seat: str) -> str:
    """The other agent seat. ``user`` has no counterpart and returns itself."""
    seat = canonical(seat)
    if seat == BUILDER:
        return REVIEWER
    if seat == REVIEWER:
        return BUILDER
    return seat


def label(seat: str) -> str:
    """Human-facing name for output columns."""
    seat = canonical(seat)
    return {BUILDER: "builder", REVIEWER: "reviewer", USER: "you", BROADCAST: "all"}.get(
        seat, seat or "?"
    )
