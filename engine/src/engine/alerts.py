"""Fills and failures out to your phone, via collab-kit's outbox.

The engine does not know Telegram exists. It writes a markdown file into
``$COLLAB_HOME/outbox/`` and ``tools/telegram-bridge.py`` forwards it. That is
collab-kit's whole design -- the bridge is an adapter over a file protocol -- and
it means swapping Telegram for Slack later touches nothing in this package.

**Why import collab-kit rather than write the file directly.** The bridge globs
``outbox/*.md`` and filters only names starting with ``.``
(``tools/telegram-bridge.py:385-393``). It has no partial-write filter, so a
naive ``open().write()`` here can be read mid-write and delivered truncated --
or quarantined as unparseable. ``collabkit.notify.write_outbox``
(``tools/collabkit/notify.py:154``) already does temp-file + fsync +
``os.replace`` and mints a collision-free, lexicographically-ordered filename.
Reusing it costs one ``sys.path`` entry and no dependency: collab-kit is
stdlib-only.

**Failure policy, split by timing.** Alerting is a convenience; the journal is
the record. So:

* :meth:`Alerter.preflight` fails *loudly at startup* if alerting is unavailable,
  when there is still time to fix it and nothing has been traded;
* :meth:`Alerter.send` is best-effort and never raises, because by the time an
  alert is being sent the fill has already happened and no exception can undo it.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from ._collabkit import last_error, load

_write_outbox: Callable[..., Any] | None = None


def _load() -> Callable[..., Any] | None:
    """Import ``collabkit.notify.write_outbox`` once, memoized."""
    global _write_outbox
    if _write_outbox is None:
        _write_outbox = load("notify", "write_outbox")
    return _write_outbox


class Alerter:
    """Best-effort phone alerts. Construct once; call :meth:`send` freely."""

    def __init__(self, project: str = "ibkr", *, enabled: bool = True) -> None:
        self.project = project
        self.enabled = enabled
        self.last_error: str = ""

    # -- availability ----------------------------------------------------

    def available(self) -> tuple[bool, str]:
        """``(ok, reason)`` -- is the outbox path usable right now?"""
        if not self.enabled:
            return False, "alerting disabled (--no-alerts)"
        if _load() is None:
            return False, last_error() or "collab-kit is not importable"
        if not os.environ.get("COLLAB_HOME", "").strip():
            # collab_home() falls back to the kit dir, which still works -- this
            # is a warning about surprise, not a hard failure.
            return True, "COLLAB_HOME is unset; collab-kit will default it to the kit directory"
        return True, ""

    def preflight(self) -> tuple[bool, str]:
        """Check alerting *before* trading, while it is still cheap to fix."""
        ok, reason = self.available()
        if not ok:
            return False, reason
        if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
            return True, (
                "TELEGRAM_BOT_TOKEN is unset -- alerts will queue in the outbox but "
                "nothing will deliver them until the bridge runs"
            )
        return True, reason

    # -- sending ---------------------------------------------------------

    def send(self, title: str, body: str = "", *, urgent: bool = False) -> bool:
        """Queue one alert. Never raises; returns whether it was queued."""
        ok, reason = self.available()
        if not ok:
            self.last_error = reason
            return False

        writer = _load()
        if writer is None:  # pragma: no cover - guarded by available()
            return False

        text = f"{title}\n{body}".rstrip() if body else title
        try:
            writer(
                text,
                project=self.project,
                source="ibkr-engine",
                priority="high" if urgent else "normal",
            )
        except Exception as exc:  # pragma: no cover - disk full, permissions
            # By the time we are alerting, the order has already happened. An
            # exception here cannot undo it, and must not mask it.
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        return True

    # -- convenience shapes ----------------------------------------------

    def fill(self, record: dict[str, Any]) -> bool:
        """Announce a fill from the journal record, so the two cannot disagree."""
        return self.send(
            f"FILLED {record.get('side', '?')} {record.get('quantity', '?')} "
            f"{record.get('symbol', '?')} @ {record.get('avg_fill_price', '?')}",
            f"order {record.get('order_id', '?')} on {record.get('account', '?')}\n"
            f"status {record.get('status', '?')}",
        )

    def refused(self, intent_text: str, reason: str) -> bool:
        return self.send(f"REFUSED {intent_text}", reason)

    def problem(self, what: str, detail: str = "") -> bool:
        return self.send(f"ENGINE PROBLEM: {what}", detail, urgent=True)
