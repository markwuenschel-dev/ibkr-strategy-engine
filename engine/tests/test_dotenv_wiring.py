"""The engine reads its configuration from a ``.env`` — end to end, via the CLI.

`tests/test_dotenv.py` in the collab-kit suite covers the parser. What is proved
here is only the wiring: that ``engine.cli.main`` applies the file *before*
``EngineConfig.from_env`` runs, and that an exported variable still beats it.

These go through ``cli.main`` rather than calling the loader directly, because
the ordering inside ``main`` is the thing that can regress — the parser being
correct buys nothing if configuration is read first.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from engine import cli
from engine.errors import EXIT_CONFIG, EXIT_OK

PAPER_ACCOUNT = "DU1234567"


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the loader at a temp ``.env``.

    conftest's session guard has already cleared every ``IBKR_*``/``TELEGRAM_*``
    the developer's shell might carry, and its autouse fixture resets the
    loader's cache, so this only has to choose the file.
    """
    dotenv = _dotenv_module()
    path = tmp_path / ".env"
    monkeypatch.setenv(dotenv.ENV_FILE, str(path))

    yield path

    # The loader writes into os.environ directly, which monkeypatch cannot undo.
    for name in ("IBKR_ACCOUNT_ID", "IBKR_PORT", "TELEGRAM_BOT_TOKEN"):
        os.environ.pop(name, None)


def _dotenv_module():
    """Import collabkit.dotenv the way the engine does, or skip."""
    from engine._collabkit import ensure_importable

    if not ensure_importable():  # pragma: no cover - a broken checkout
        pytest.skip("collab-kit not importable from this checkout")
    from collabkit import dotenv

    return dotenv


class TestTheEngineReadsDotenv:
    def test_account_id_from_the_file_is_enough_to_run_doctor(
        self, env_file: Path, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env_file.write_text(f"IBKR_ACCOUNT_ID={PAPER_ACCOUNT}\n", encoding="utf-8")

        code = cli.main(["--state-dir", str(state_dir), "--no-alerts", "doctor"])

        assert code == EXIT_OK
        assert PAPER_ACCOUNT in capsys.readouterr().out

    def test_without_the_file_the_same_command_is_a_config_error(
        self, env_file: Path, state_dir: Path
    ) -> None:
        """Proves the previous test passed because of the file, not ambient state."""
        assert not env_file.exists()

        code = cli.main(["--state-dir", str(state_dir), "--no-alerts", "doctor"])

        assert code == EXIT_CONFIG

    def test_an_exported_variable_beats_the_file(
        self, env_file: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        env_file.write_text("IBKR_ACCOUNT_ID=DU0000000\n", encoding="utf-8")
        monkeypatch.setenv("IBKR_ACCOUNT_ID", PAPER_ACCOUNT)

        code = cli.main(["--state-dir", str(state_dir), "--no-alerts", "doctor"])

        assert code == EXIT_OK
        out = capsys.readouterr().out
        assert PAPER_ACCOUNT in out
        assert "DU0000000" not in out

    def test_a_live_port_in_the_file_is_still_refused(
        self, env_file: Path, state_dir: Path
    ) -> None:
        """The .env is a convenience; it is not a way around the port allowlist."""
        env_file.write_text(
            f"IBKR_ACCOUNT_ID={PAPER_ACCOUNT}\nIBKR_PORT=7496\n", encoding="utf-8"
        )

        code = cli.main(["--state-dir", str(state_dir), "--no-alerts", "doctor"])

        assert code == EXIT_CONFIG

    def test_doctor_reports_which_file_it_used(
        self, env_file: Path, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env_file.write_text(f"IBKR_ACCOUNT_ID={PAPER_ACCOUNT}\n", encoding="utf-8")

        cli.main(["--state-dir", str(state_dir), "--no-alerts", "doctor"])

        out = capsys.readouterr().out
        assert "env file" in out
        assert str(env_file) in out

    def test_doctor_says_so_when_there_is_no_file(
        self, env_file: Path, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.main(["--account", PAPER_ACCOUNT, "--state-dir", str(state_dir), "doctor"])

        assert "no .env found" in capsys.readouterr().out


class TestSecretsAreNotPrinted:
    def test_doctor_never_echoes_a_token_from_the_file(
        self, env_file: Path, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env_file.write_text(
            f"IBKR_ACCOUNT_ID={PAPER_ACCOUNT}\nTELEGRAM_BOT_TOKEN=123:supersecret\n",
            encoding="utf-8",
        )

        cli.main(["--state-dir", str(state_dir), "--no-alerts", "doctor"])

        captured = capsys.readouterr()
        assert "supersecret" not in captured.out
        assert "supersecret" not in captured.err
        # It did reach the environment -- the bridge needs it there.
        assert os.environ.get("TELEGRAM_BOT_TOKEN") == "123:supersecret"
