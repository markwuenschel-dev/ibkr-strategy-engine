#!/usr/bin/env python3
"""Always-on alert watcher across *every* registered collab.

    python3 tools/watch-all-handoffs.py            # desktop notifications
    python3 tools/watch-all-handoffs.py --phone    # + Telegram, via the outbox

This is the "nothing waits unseen" backstop from ARCHITECTURE.md. The
per-session watchers only cover the collab their session is pinned to; this one
fans every collab's events out to notifications, so a handoff in a project you
are not currently sitting in still reaches you.

It defaults to firing desktop notifications (the per-seat watchers do not),
because its output is usually not being read by anyone -- the notification *is*
the delivery.
"""

import os
import sys


def _bootstrap():
    tools = os.path.dirname(os.path.realpath(__file__))
    if tools not in sys.path:
        sys.path.insert(0, tools)


if __name__ == "__main__":
    _bootstrap()
    from collabkit import seats
    from collabkit.watchcli import run

    raise SystemExit(
        run(
            None,
            prog="watch-all-handoffs.py",
            description="Fan handoff events from every registered collab out to notifications.",
            seat=seats.BROADCAST,
            single=False,
            scope_label="all",
            default_notify=True,
        )
    )
