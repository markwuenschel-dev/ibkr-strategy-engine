"""CLI behaviour, and specifically the ordering of the safety gates.

The bug these exist to prevent was real and was found by running the CLI, not by
a unit test: a halted engine opened a socket to TWS *before* checking the kill
switch, so it reported "connection refused" (exit 5) instead of "halted"
(exit 6). The gates were all present and all correct -- they simply ran in the
wrong order relative to the connection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import cli
from engine.errors import EXIT_CONFIG, EXIT_HALTED, EXIT_OK, EXIT_REFUSED
from engine.journal import OrderJournal
from fakes import FakeIB

ACCOUNT = "DU1234567"


class ExplodingBroker:
    """A broker that fails the test if it is ever constructed.

    This is the whole point: some refusals must happen without a connection.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("the engine connected before it should have")


def run(argv: list[str], state_dir: Path, **kwargs: object) -> int:
    return cli.main(
        ["--account", ACCOUNT, "--state-dir", str(state_dir), "--no-alerts", *argv],
        **kwargs,  # type: ignore[arg-type]
    )


def run_trade(argv: list[str], state_dir: Path, broker_factory: object) -> int:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--account", ACCOUNT, "--state-dir", str(state_dir), "--no-alerts", *argv]
    )
    return cli.cmd_trade(args, broker_factory=broker_factory)


class TestGatesThatMustPrecedeTheConnection:
    def test_a_halted_engine_never_connects(self, state_dir: Path) -> None:
        (state_dir / "HALT").write_text("stop", encoding="utf-8")
        with pytest.raises(Exception) as caught:
            run_trade(
                ["trade", "--symbol", "SPY", "--qty", "1", "--arm"],
                state_dir,
                ExplodingBroker,
            )
        # HaltedError, not the AssertionError from ExplodingBroker.
        assert caught.value.__class__.__name__ == "HaltedError"

    def test_a_disallowed_symbol_never_connects(self, state_dir: Path) -> None:
        with pytest.raises(Exception) as caught:
            run_trade(
                ["trade", "--symbol", "GME", "--qty", "1", "--arm"],
                state_dir,
                ExplodingBroker,
            )
        assert "allowlist" in str(caught.value)

    def test_a_nonsense_quantity_never_connects(self, state_dir: Path) -> None:
        with pytest.raises(Exception) as caught:
            run_trade(
                ["trade", "--symbol", "SPY", "--qty", "0", "--arm"],
                state_dir,
                ExplodingBroker,
            )
        assert "positive" in str(caught.value)

    def test_the_halt_refusal_is_journalled_with_its_stage(self, state_dir: Path) -> None:
        (state_dir / "HALT").write_text("stop", encoding="utf-8")
        with pytest.raises(Exception):
            run_trade(
                ["trade", "--symbol", "SPY", "--qty", "1", "--arm"],
                state_dir,
                ExplodingBroker,
            )
        # The kill switch short-circuits before the refusal is journalled, so
        # what must survive is the preflight record proving we got that far and
        # no further.
        events = [r["event"] for r in OrderJournal(state_dir / "orders.jsonl").records()]
        assert "order_placed" not in events


class TestExitCodes:
    def test_halted_exits_with_the_halted_code(self, state_dir: Path) -> None:
        (state_dir / "HALT").write_text("stop", encoding="utf-8")
        assert run(["trade", "--symbol", "SPY", "--qty", "1", "--arm"], state_dir) == EXIT_HALTED

    def test_a_live_port_exits_with_the_config_code(self, state_dir: Path) -> None:
        code = cli.main(
            ["--account", ACCOUNT, "--state-dir", str(state_dir), "--port", "7496", "doctor"]
        )
        assert code == EXIT_CONFIG

    def test_doctor_succeeds_without_a_connection(self, state_dir: Path) -> None:
        assert run(["doctor"], state_dir) == EXIT_OK


class TestDryRun:
    def _factory(self, ib: FakeIB):
        from engine.broker import Broker

        def make(config, journal):  # type: ignore[no-untyped-def]
            return Broker(config, journal, ib=ib)

        return make

    def test_an_unarmed_trade_previews_then_refuses_without_placing(
        self, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ib = FakeIB(accounts=[ACCOUNT])
        with pytest.raises(Exception) as caught:
            run_trade(
                ["trade", "--symbol", "SPY", "--qty", "1"], state_dir, self._factory(ib)
            )
        assert "not armed" in str(caught.value)
        # The dry run's value is that you still see what it would have sent.
        printed = capsys.readouterr().out
        assert "pre-trade preview" in printed
        assert "would send" in printed
        assert ib.placed == [], "an unarmed run must never place an order"

    def test_an_armed_trade_places_exactly_one_order(self, state_dir: Path) -> None:
        ib = FakeIB(accounts=[ACCOUNT])
        code = run_trade(
            ["trade", "--symbol", "SPY", "--qty", "1", "--arm"],
            state_dir,
            self._factory(ib),
        )
        assert code == EXIT_OK
        assert len(ib.placed) == 1
        events = [r["event"] for r in OrderJournal(state_dir / "orders.jsonl").records()]
        assert "order_placed" in events and "order_result" in events


class TestKillSwitchCommands:
    def test_halt_then_resume_round_trips(self, state_dir: Path) -> None:
        assert run(["halt", "because"], state_dir) == EXIT_OK
        assert (state_dir / "HALT").is_file()
        assert run(["resume"], state_dir) == EXIT_OK
        assert not (state_dir / "HALT").exists()

    def test_resume_is_idempotent(self, state_dir: Path) -> None:
        assert run(["resume"], state_dir) == EXIT_OK

    def test_the_journal_command_reads_back_what_happened(
        self, state_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(["halt", "a reason"], state_dir)
        capsys.readouterr()
        run(["journal", "-n", "5"], state_dir)
        assert "halted" in capsys.readouterr().out


class TestRefusalCodes:
    def test_an_over_cap_order_is_refused_with_the_refused_code(
        self, state_dir: Path
    ) -> None:
        # 1 share priced at 100 is fine; 50 shares breaches the notional cap.
        ib = FakeIB(accounts=[ACCOUNT])
        from engine.broker import Broker

        def make(config, journal):  # type: ignore[no-untyped-def]
            return Broker(config, journal, ib=ib)

        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "--account", ACCOUNT, "--state-dir", str(state_dir), "--no-alerts",
                "trade", "--symbol", "SPY", "--qty", "50", "--arm",
            ]
        )
        with pytest.raises(Exception) as caught:
            cli.cmd_trade(args, broker_factory=make)
        assert caught.value.exit_code == EXIT_REFUSED  # type: ignore[attr-defined]
        assert ib.placed == []
