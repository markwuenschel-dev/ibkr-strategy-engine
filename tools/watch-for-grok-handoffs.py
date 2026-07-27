#!/usr/bin/env python3
"""Reviewer-side watcher: surfaces handoffs addressed to the reviewer seat.

    HANDOFF_ROOT=$COLLAB_HOME/<name> python3 tools/watch-for-grok-handoffs.py

Run this in the reviewer's session. The reviewer gets pinged the moment the
builder requests a review, which is the whole point: a review that waits for
someone to remember to check a directory is a review that does not happen.

Inbound phone messages are deliberately *not* surfaced here -- the builder seat
owns the conversation with you, and two agents answering the same message is
worse than one. Pass ``--seat all`` if you want this session to see everything.
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
            prog="watch-for-grok-handoffs.py",
            description="Surface handoffs addressed to the reviewer seat.",
            seat=seats.REVIEWER,
            single=True,
            scope_label="reviewer",
        )
    )
