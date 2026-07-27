"""Test package for collab-kit.

Run everything from the repository root::

    python3 -m unittest discover -s tests -t . -v

This file exists so that ``-t .`` can import the suite as ``tests.<module>``,
which is what lets every test module share ``tests.support``.

It also performs the import bootstrap. The code under test lives in ``tools/``,
which is not on ``sys.path``; the package initializer is the only hook that is
guaranteed to run *before* a test module's own top-level ``import collabkit``,
so the path has to be arranged here rather than in ``support``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

# collabkit.atomic reads this once at import time. fsync on every write makes a
# few thousand tiny test writes measurably slow and buys the suite nothing.
os.environ.setdefault("COLLAB_KIT_NO_FSYNC", "1")
