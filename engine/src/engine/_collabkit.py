"""Locate and import collab-kit, which lives outside this package.

collab-kit is stdlib-only, so importing it adds no dependency -- but it is not
installed as a distribution either. It sits in ``tools/`` in the same repository
and its own entry points reach it by putting that directory on ``sys.path``
(see ``tools/handoff:17-27``). This module does the same thing once, so
:mod:`engine.alerts` and :mod:`engine.cli` do not each grow their own copy.

Everything here degrades to ``None`` rather than raising. collab-kit provides
alerting and a process lock; both are valuable and neither is worth refusing to
trade over. The caller decides how loudly to complain -- see
:meth:`engine.alerts.Alerter.preflight`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_ERROR: str = ""


def tools_dir() -> Path | None:
    """collab-kit's ``tools/`` directory, or None if it cannot be found.

    ``$KIT_DIR`` wins because collab-kit's installer sets it and it survives the
    engine being installed as a wheel somewhere else entirely. The repo-relative
    guess is the fallback for a plain checkout.
    """
    override = os.environ.get("KIT_DIR", "").strip()
    if override:
        candidate = Path(override).expanduser() / "tools"
        if (candidate / "collabkit").is_dir():
            return candidate

    # engine/src/engine/_collabkit.py -> repo root is four parents up.
    here = Path(__file__).resolve()
    if len(here.parents) >= 4:
        candidate = here.parents[3] / "tools"
        if (candidate / "collabkit").is_dir():
            return candidate
    return None


def ensure_importable() -> bool:
    """Put collab-kit on ``sys.path``. Returns whether it is now importable."""
    global _ERROR
    tools = tools_dir()
    if tools is None:
        _ERROR = "could not locate collab-kit's tools/ directory (set KIT_DIR)"
        return False
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    return True


def load(module: str, attribute: str) -> Any | None:
    """Import ``collabkit.<module>.<attribute>``, or None with a reason recorded."""
    global _ERROR
    if not ensure_importable():
        return None
    try:
        imported = __import__(f"collabkit.{module}", fromlist=[attribute])
        return getattr(imported, attribute)
    except Exception as exc:  # pragma: no cover - environmental
        _ERROR = f"{type(exc).__name__}: {exc}"
        return None


def load_dotenv() -> Any | None:
    """Apply the repo's git-ignored ``.env``, or None if collab-kit is missing.

    Called before :class:`engine.config.EngineConfig` reads ``os.environ``, so
    ``IBKR_ACCOUNT_ID`` and friends can live in a file rather than a shell that
    has to be re-exported in every new terminal.

    One ordering caveat, deliberately not worked around: ``$KIT_DIR`` is how
    :func:`tools_dir` finds collab-kit when the engine is installed as a wheel
    outside this repo, and it is read from the *real* environment. Putting
    ``KIT_DIR`` in ``.env`` cannot work -- the file cannot tell the loader where
    the loader lives. In a plain checkout the repo-relative fallback resolves it
    and the caveat never arises.
    """
    global _ERROR
    loader = load("dotenv", "load_dotenv")
    if loader is None:
        return None
    try:
        return loader()
    except Exception as exc:  # pragma: no cover - environmental
        _ERROR = f"{type(exc).__name__}: {exc}"
        return None


def last_error() -> str:
    return _ERROR
