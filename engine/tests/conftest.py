"""Shared fixtures — and the two guards that keep this suite hermetic.

collab-kit's suite states the rule (``tests/README.md``): *"no network access in
any test."* It matters more here. These tests exercise the code path that sends
orders to a broker, so a test that accidentally opened a socket to a running TWS
would not merely be slow — it could place an order.

:func:`_block_sockets` therefore replaces ``socket.socket`` for the whole
session. Any test that tries to connect fails loudly instead of succeeding
quietly.

:func:`_isolate_dotenv` is the second guard, and it exists because of a real
leak. ``engine.cli.main`` loads a ``.env`` by searching upward from the working
directory; the suite runs from ``engine/``, so every test that called ``main``
pulled the developer's actual repository ``.env`` — real bot token, real account
id — into ``os.environ`` for the rest of the session. That is bad twice over: a
secret ends up in test output the moment an assertion fails, and the suite
silently starts depending on whichever machine it runs on. Both variables are
therefore cleared and the loader is pointed at a path that does not exist.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from engine.config import ENV_PREFIX, EngineConfig
from engine.journal import OrderJournal
from engine.safety import SafetyGate

PAPER_ACCOUNT = "DU1234567"

# Anything the engine or the bridge reads that a developer might have exported.
_LEAKY_PREFIXES = (ENV_PREFIX, "TELEGRAM_", "COLLAB_")


@pytest.fixture(autouse=True, scope="session")
def _block_sockets() -> None:
    """Make any real socket construction an error for the whole session."""
    real_socket = socket.socket

    class BlockedSocket(real_socket):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(
                "engine tests must not open sockets -- use the fake IB in tests/fakes.py"
            )

    socket.socket = BlockedSocket  # type: ignore[misc, assignment]
    yield
    socket.socket = real_socket  # type: ignore[misc, assignment]


@pytest.fixture(autouse=True, scope="session")
def _isolate_dotenv(tmp_path_factory: pytest.TempPathFactory):
    """Stop the real repository ``.env`` reaching the suite. See the module docstring."""
    saved = {
        name: value
        for name, value in os.environ.items()
        if name.startswith(_LEAKY_PREFIXES)
    }
    for name in saved:
        del os.environ[name]

    # A path that cannot exist: `find_file` honours this override absolutely and
    # returns None rather than walking up into the real checkout.
    absent = tmp_path_factory.mktemp("no-dotenv") / "absent.env"
    os.environ["COLLAB_ENV_FILE"] = str(absent)

    yield

    os.environ.pop("COLLAB_ENV_FILE", None)
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _reset_dotenv_cache():
    """``collabkit.dotenv`` caches its first implicit load; tests must not share it."""
    from engine._collabkit import ensure_importable

    if not ensure_importable():  # pragma: no cover - a broken checkout
        yield
        return
    from collabkit import dotenv

    dotenv.reset_for_tests()
    yield
    dotenv.reset_for_tests()


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    return directory


@pytest.fixture
def config(state_dir: Path) -> EngineConfig:
    """A valid paper config pointed entirely at a temp directory."""
    return EngineConfig(
        account_id=PAPER_ACCOUNT,
        port=7497,
        state_dir=state_dir,
        symbol_allowlist=("SPY", "AAPL"),
        max_order_notional=1_000.0,
        max_position_qty=10,
        max_orders_per_session=5,
        max_margin_impact=5_000.0,
    )


@pytest.fixture
def journal(config: EngineConfig) -> OrderJournal:
    return OrderJournal(config.journal_path)


@pytest.fixture
def gate(config: EngineConfig, journal: OrderJournal) -> SafetyGate:
    return SafetyGate(config, journal)
