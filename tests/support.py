"""Shared harness for the collab-kit suite: isolation, fixtures, drivers.

Three jobs, in the order they matter:

1. **Make the package importable.** ``tests/__init__.py`` does the actual
   ``sys.path`` work -- it is the only hook that runs before a test module's own
   top-level ``import collabkit`` -- and this module re-asserts it so that
   importing ``tests.support`` directly is also enough. The repo root is derived
   from ``__file__``; nothing is hardcoded.

2. **Guarantee isolation.** :class:`IsolatedHomeTestCase` gives every test its
   own ``$COLLAB_HOME`` inside a fresh ``tempfile.TemporaryDirectory`` and
   *asserts* it, in both setUp and cleanup. A suite that quietly wrote into the
   developer's real ``$COLLAB_HOME`` would be worse than no suite: it would
   destroy live handoffs and the damage would look like a kit bug.

3. **Drive the CLI in-process.** :func:`run_cli` captures stdout/stderr and
   normalizes argparse's ``SystemExit`` into an exit code, so tests can assert
   on the documented codes in ``collabkit.errors`` rather than on message text.
"""

from __future__ import annotations

import contextlib
import io
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence
from unittest import mock

# --------------------------------------------------------------------------
# locations -- all derived, nothing hardcoded
# --------------------------------------------------------------------------

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
TOOLS_DIR = REPO_ROOT / "tools"
BRIDGE_PATH = TOOLS_DIR / "telegram-bridge.py"
WORKFLOW_PATH = TOOLS_DIR / "diff-regression-hunt.workflow.js"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

# collabkit.atomic reads this once, at import time, so it has to be set before
# the first `import collabkit`. fsync on every write makes a few thousand tiny
# test writes measurably slow and buys the suite nothing.
os.environ.setdefault("COLLAB_KIT_NO_FSYNC", "1")

# Snapshotted before any test patches os.environ, so the isolation guard can
# prove a test is not pointing at whatever the developer really uses.
REAL_COLLAB_HOME = os.environ.get("COLLAB_HOME")
REAL_HANDOFF_ROOT = os.environ.get("HANDOFF_ROOT")
USER_HOME = Path.home().resolve()

WINDOWS = sys.platform.startswith("win")


def expected_failure_on_windows(func):
    """Mark a test that exposes a defect only reachable on Windows.

    The suite has to stay green on every platform, so a Windows-only defect
    cannot simply be ``@unittest.expectedFailure``: on POSIX the code is
    correct and the test would report an *unexpected success*, which unittest
    counts as a failure. Applying the marker conditionally keeps the run green
    everywhere while still refusing to pretend the Windows behaviour is fine.
    """
    return unittest.expectedFailure(func) if WINDOWS else func


# --------------------------------------------------------------------------
# the isolated base case
# --------------------------------------------------------------------------


class IsolatedHomeTestCase(unittest.TestCase):
    """Base class: private temp dir, private ``$COLLAB_HOME``, asserted."""

    def setUp(self) -> None:
        super().setUp()
        self._tempdir = tempfile.TemporaryDirectory(prefix="collabkit-tests-")
        self.addCleanup(self._remove_tempdir)
        self.tmp = Path(self._tempdir.name).resolve()
        self.home = self.tmp / "collab-home"
        self.home.mkdir(parents=True, exist_ok=True)

        patcher = mock.patch.dict(
            os.environ,
            {
                "COLLAB_HOME": str(self.home),
                "COLLAB_KIT_NO_FSYNC": "1",
                # Deterministic output: no ANSI codes, no unicode glyphs that a
                # legacy Windows code page would mangle.
                "NO_COLOR": "1",
                "COLLAB_KIT_ASCII": "1",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        # patch.dict restores the whole mapping on stop, so deleting here is
        # safe and is the only way to prove a command is *not* reading ambient
        # scope it should have ignored.
        for name in (
            "HANDOFF_ROOT",
            "KIT_DIR",
            "COLLAB_SEAT_ALIASES",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
        ):
            os.environ.pop(name, None)

        self.assert_isolated()
        self.addCleanup(self.assert_isolated)

    # -- the guard -------------------------------------------------------

    def assert_isolated(self) -> None:
        """Fail loudly if this test could reach real user state."""
        value = os.environ.get("COLLAB_HOME")
        self.assertTrue(value, "COLLAB_HOME must be set for every test")
        resolved = Path(str(value)).expanduser().resolve()

        self.assertTrue(
            resolved.is_relative_to(self.tmp),
            f"COLLAB_HOME {resolved} escaped this test's temp dir {self.tmp}",
        )
        self.assertNotEqual(resolved, USER_HOME, "COLLAB_HOME is the user's home directory")
        self.assertNotEqual(resolved, REPO_ROOT, "COLLAB_HOME is the repository root")
        if REAL_COLLAB_HOME:
            self.assertNotEqual(
                resolved,
                Path(REAL_COLLAB_HOME).expanduser().resolve(),
                "COLLAB_HOME is the developer's real collab home",
            )

        root = os.environ.get("HANDOFF_ROOT")
        if root:
            self.assertTrue(
                Path(root).expanduser().resolve().is_relative_to(self.tmp),
                f"HANDOFF_ROOT {root} escaped this test's temp dir {self.tmp}",
            )

    def _remove_tempdir(self) -> None:
        # Windows refuses to unlink a file another handle still has open; a
        # leaked temp dir must not turn into a spurious test failure.
        try:
            self._tempdir.cleanup()
        except OSError:
            shutil.rmtree(self._tempdir.name, ignore_errors=True)

    # -- fixtures --------------------------------------------------------

    def set_handoff_root(self, root: Path) -> None:
        """Scope ``collab-handoff`` at one collab, the way a watcher does."""
        os.environ["HANDOFF_ROOT"] = str(root)
        self.assert_isolated()

    def home_paths(self):
        from collabkit.paths import HomePaths

        return HomePaths(self.home).ensure()

    def make_collab(self, name: str = "demo"):
        """Create ``$COLLAB_HOME/<name>`` with the full queue skeleton."""
        from collabkit.paths import CollabPaths

        return CollabPaths.at(self.home / name, name).ensure()

    def make_store(self, name: str = "demo"):
        from collabkit.store import HandoffStore

        return HandoffStore(self.make_collab(name), collab=name)

    # -- assertions ------------------------------------------------------

    def assert_inside(self, child: Path, parent: Path) -> None:
        """Containment check that survives symlinks and ``..`` segments."""
        resolved_child = Path(child).resolve()
        resolved_parent = Path(parent).resolve()
        self.assertTrue(
            resolved_child.is_relative_to(resolved_parent),
            f"{resolved_child} escaped {resolved_parent}",
        )


# --------------------------------------------------------------------------
# CLI driver
# --------------------------------------------------------------------------


def run_cli(argv: Sequence[str], *, root_mode: bool = False) -> tuple[int, str, str]:
    """Run ``handoff`` (or ``collab-handoff``) in-process.

    Returns ``(exit_code, stdout, stderr)``. argparse reports its own usage
    errors by raising ``SystemExit``, so that is folded back into an exit code
    here -- callers assert on codes, never on message text.
    """
    from collabkit import cli

    entry = cli.main_root if root_mode else cli.main
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = entry(list(argv))
        except SystemExit as exc:
            if isinstance(exc.code, int):
                code = exc.code
            else:
                code = 0 if exc.code is None else 1
    return int(code), stdout.getvalue(), stderr.getvalue()


# --------------------------------------------------------------------------
# telegram-bridge.py, which is not importable by name
# --------------------------------------------------------------------------

_BRIDGE_MODULE: Any = None


def load_bridge_module() -> Any:
    """Import ``tools/telegram-bridge.py``, whose filename is not an identifier.

    Cached: the module installs ``tools/`` on ``sys.path`` and re-executing it
    per test would be pure overhead.
    """
    global _BRIDGE_MODULE
    if _BRIDGE_MODULE is not None:
        return _BRIDGE_MODULE
    spec = importlib.util.spec_from_file_location("collabkit_telegram_bridge", BRIDGE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {BRIDGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _BRIDGE_MODULE = module
    return module


class FakeTelegramClient:
    """Stand-in for ``TelegramClient``. Same surface, zero network.

    ``fail_with`` makes the next (and every) ``send_message`` raise, which is
    how the transient-vs-permanent quarantine policy is exercised.
    """

    def __init__(self, *, fail_with: BaseException | None = None) -> None:
        self.sent: list[tuple[Any, str]] = []
        self.updates: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.fail_with = fail_with

    # -- surface ---------------------------------------------------------

    def redact(self, text: str) -> str:
        return text

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        self.calls.append((method, params))
        return None

    def get_me(self) -> dict[str, Any]:
        return {"id": 1, "username": "fake_bot"}

    def send_message(self, chat_id: Any, text: str, *, parse_mode: str = "") -> Any:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}

    def get_updates(self, offset: int, *, long_poll: int = 25) -> list[dict[str, Any]]:
        return [u for u in self.updates if int(u.get("update_id", 0)) >= offset]

    # -- helpers ---------------------------------------------------------

    @property
    def texts(self) -> list[str]:
        return [text for _chat, text in self.sent]


def telegram_message(text: str, *, chat_id: int = 555, message_id: int = 1) -> dict[str, Any]:
    """A minimal Bot API ``message`` payload."""
    return {"message_id": message_id, "chat": {"id": chat_id}, "text": text}
