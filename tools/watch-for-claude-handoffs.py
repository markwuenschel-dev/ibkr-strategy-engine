#!/usr/bin/env python3
"""Builder-side watcher: surfaces handoffs addressed to the builder seat.

    HANDOFF_ROOT=$COLLAB_HOME/<name> python3 tools/watch-for-claude-handoffs.py

Run this in the builder's session. It tails that collab's ``handoffs/pending/``
and prints a block the moment the reviewer sends something back -- plus any
message you sent from your phone, since the builder is the seat that talks to
you.

Named for the common pairing (Claude builds, Grok reviews). If you run it the
other way round, pass ``--seat reviewer`` or set
``COLLAB_SEAT_ALIASES=claude=reviewer``.
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
            prog="watch-for-claude-handoffs.py",
            description="Surface handoffs addressed to the builder seat.",
            seat=seats.BUILDER,
            single=True,
            scope_label="builder",
        )
    )
