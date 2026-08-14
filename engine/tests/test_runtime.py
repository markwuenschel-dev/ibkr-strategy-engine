"""The shared operational process primitives, tested where they now live.

``engine.runtime`` was extracted from ``engine.paperday`` so a scheduler can
reuse the process plumbing without importing the session controller. Two
properties therefore matter more than any single function here:

* ``runtime`` stays a **leaf** -- stdlib imports only. The cycle it exists to
  prevent returns the moment it imports ``paperday``, ``options`` or ``cli``.
* ``_engine_dir()`` still resolves to the same ``engine/`` directory it did
  from ``paperday.py``. That anchor picks the state directory, i.e. which order
  book every command touches; a wrong answer here retargets the live book
  silently, which is exactly the 2026-07-31 split-brain incident its docstring
  records.

No test in this file opens a socket or spawns a process. ``tests/conftest.py``
blocks ``socket.socket`` for the whole session precisely because this package
can talk to a broker, so :func:`default_tcp_probe` is exercised by replacing
``socket.create_connection`` with a stand-in that raises the errors a closed
port really raises.
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

from engine import paperday, runtime
from engine.runtime import (
    EngineCommandResult,
    EngineCommandRunner,
    SubprocessProcessPort,
    _engine_dir,
    default_tcp_probe,
)


class TestEngineCommandResult:
    """A plain record -- no fakes, no patching, nothing to stub."""

    def test_positional_construction_keeps_code_and_output(self) -> None:
        result = EngineCommandResult(0, "connected to TWS paper as DU1234567")
        assert result.code == 0
        assert result.stdout == "connected to TWS paper as DU1234567"

    def test_keyword_construction_is_equivalent(self) -> None:
        assert EngineCommandResult(code=2, stdout="boom") == EngineCommandResult(2, "boom")

    def test_records_differing_in_either_field_are_not_equal(self) -> None:
        assert EngineCommandResult(0, "ok") != EngineCommandResult(1, "ok")
        assert EngineCommandResult(0, "ok") != EngineCommandResult(0, "OK")

    def test_a_nonzero_code_is_preserved_verbatim(self) -> None:
        """Callers branch on ``code == 0``; the runner must not normalise it."""
        result = EngineCommandResult(20, "REFUSED: entry gate CLOSED")
        assert result.code == 20
        assert result.code != 0


class TestDefaultTcpProbe:
    """A closed port must be a quiet ``False``, never an exception."""

    @staticmethod
    def _refuse(exc: BaseException):
        def _create_connection(*args: object, **kwargs: object):
            raise exc

        return _create_connection

    @pytest.mark.parametrize(
        "error",
        [
            ConnectionRefusedError(61, "Connection refused"),
            TimeoutError("timed out"),
            OSError(10061, "No connection could be made"),
            socket.gaierror("name or service not known"),
        ],
        ids=["refused", "timeout", "oserror", "dns-failure"],
    )
    def test_returns_false_for_a_closed_port(
        self, monkeypatch: pytest.MonkeyPatch, error: BaseException
    ) -> None:
        monkeypatch.setattr(socket, "create_connection", self._refuse(error))
        assert default_tcp_probe("127.0.0.1", 7497) is False

    def test_returns_false_without_raising_on_the_default_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def _create_connection(address: tuple[str, int], timeout: float | None = None):
            seen["address"] = address
            seen["timeout"] = timeout
            raise ConnectionRefusedError

        monkeypatch.setattr(socket, "create_connection", _create_connection)
        assert default_tcp_probe("localhost", 4002) is False
        assert seen == {"address": ("localhost", 4002), "timeout": 3.0}

    def test_an_explicit_timeout_reaches_the_socket_layer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def _create_connection(address: tuple[str, int], timeout: float | None = None):
            seen["timeout"] = timeout
            raise ConnectionRefusedError

        monkeypatch.setattr(socket, "create_connection", _create_connection)
        assert default_tcp_probe("127.0.0.1", 7497, timeout=0.25) is False
        assert seen["timeout"] == 0.25

    def test_an_open_port_is_true_and_the_connection_is_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed: list[bool] = []

        class _Connection:
            def __enter__(self) -> "_Connection":
                return self

            def __exit__(self, *_: object) -> bool:
                closed.append(True)
                return False

        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _Connection())
        assert default_tcp_probe("127.0.0.1", 7497) is True
        assert closed == [True], "the probe must not leak the connection it opened"

    def test_the_suite_still_forbids_a_real_socket(self) -> None:
        """The guard in conftest is what makes the stand-ins above mandatory."""
        with pytest.raises(RuntimeError, match="must not open sockets"):
            socket.socket()


class TestSubprocessProcessPortParsing:
    """The parsing half of the port, with the PowerShell call seam overridden.

    Nothing here shells out; each case replaces ``_powershell`` with the text
    Windows would have returned, so the digit filtering and the liveness rule
    are pinned without a process.
    """

    class _Port(SubprocessProcessPort):
        def __init__(self, output: str) -> None:
            self.output = output
            self.scripts: list[str] = []

        def _powershell(self, script: str) -> str:
            self.scripts.append(script)
            return self.output

    def test_pids_matching_keeps_only_the_digit_tokens(self) -> None:
        port = self._Port("1234\r\n5678\r\n")
        assert port.pids_matching("watch-for-claude-handoffs.py") == [1234, 5678]

    def test_pids_matching_ignores_powershell_noise_and_empty_output(self) -> None:
        assert self._Port("").pids_matching("nothing") == []
        assert self._Port("ProcessId\n-------\n42\n").pids_matching("x") == [42]

    def test_pids_matching_passes_the_needle_into_the_query(self) -> None:
        port = self._Port("")
        port.pids_matching("watch-for-grok-handoffs.py")
        assert "watch-for-grok-handoffs.py" in port.scripts[0]
        assert "Win32_Process" in port.scripts[0]

    def test_cmdline_is_stripped(self) -> None:
        port = self._Port("  python tools\\watcher.py  \r\n")
        assert port.cmdline(1234) == "python tools\\watcher.py"

    def test_alive_is_a_command_line_question_not_a_pid_question(self) -> None:
        """A recycled PID with no command line is dead; see the class docstring."""
        assert self._Port("python tools\\watcher.py").alive(1234) is True
        assert self._Port("   \r\n").alive(1234) is False
        assert self._Port("").alive(1234) is False

    def test_cmdline_queries_the_pid_it_was_given(self) -> None:
        port = self._Port("x")
        port.cmdline(4321)
        assert "ProcessId=4321" in port.scripts[0]


class TestEngineCommandRunnerWiring:
    def test_the_state_dir_is_pinned_on_construction(self, tmp_path: Path) -> None:
        runner = EngineCommandRunner(tmp_path / "state")
        assert runner.state_dir == tmp_path / "state"


class TestTheEngineDirAnchor:
    """The highest-risk part of the extraction: which book commands touch."""

    def test_the_anchor_is_the_engine_directory_holding_this_package(self) -> None:
        expected = Path(runtime.__file__).resolve().parents[2]
        assert _engine_dir() == expected
        assert (_engine_dir() / "src" / "engine" / "runtime.py").is_file()
        assert (_engine_dir() / "pyproject.toml").is_file()

    def test_paperday_resolves_the_same_anchor_after_the_move(self) -> None:
        assert paperday._engine_dir is _engine_dir
        assert paperday._engine_dir() == _engine_dir()

    def test_the_default_state_dir_is_unchanged(self) -> None:
        assert paperday.PaperDayPaths.default().state_dir == _engine_dir() / ".engine"

    def test_the_anchor_never_consults_the_working_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The 2026-07-31 incident in ``_engine_dir``'s docstring, as a test."""
        before = _engine_dir()
        monkeypatch.chdir(tmp_path)
        assert _engine_dir() == before


class TestRuntimeIsALeaf:
    def test_runtime_imports_nothing_from_this_package(self) -> None:
        source = Path(runtime.__file__).read_text(encoding="utf-8")
        imported: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append("." * node.level + (node.module or ""))
        assert imported == [
            "__future__",
            "contextlib",
            "os",
            "socket",
            "subprocess",
            "sys",
            "dataclasses",
            "pathlib",
        ], f"engine.runtime must stay stdlib-only; found {imported}"


class TestPaperdayStillReExports:
    """Nothing that imported these from ``paperday`` may break."""

    @pytest.mark.parametrize(
        "name",
        [
            "SubprocessProcessPort",
            "EngineCommandResult",
            "EngineCommandRunner",
            "default_tcp_probe",
        ],
    )
    def test_the_moved_name_is_the_same_object_on_both_modules(self, name: str) -> None:
        assert getattr(paperday, name) is getattr(runtime, name)
        assert name in paperday.__all__

    def test_the_controller_defaults_still_point_at_the_moved_primitives(
        self, tmp_path: Path
    ) -> None:
        controller = paperday.PaperDayController(
            paths=paperday.PaperDayPaths(state_dir=tmp_path / "state")
        )
        assert isinstance(controller.processes, SubprocessProcessPort)
        assert isinstance(controller.engine, EngineCommandRunner)
        assert controller.tcp_probe is default_tcp_probe
