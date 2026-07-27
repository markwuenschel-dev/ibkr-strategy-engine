"""The interlocks. These are the tests that keep this a paper-only tool.

A safety rail that has never been observed to stop anything is not a rail, so
every one of these asserts a *refusal*, not just that the happy path works.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import LIVE_PORTS, PAPER_PORTS, EngineConfig
from engine.errors import ConfigError, UnsafeConfigError

ACCOUNT = "DU1234567"


class TestPortInterlock:
    @pytest.mark.parametrize("port", sorted(PAPER_PORTS))
    def test_paper_ports_are_accepted(self, port: int, tmp_path: Path) -> None:
        config = EngineConfig(account_id=ACCOUNT, port=port, state_dir=tmp_path)
        assert config.port == port
        assert config.venue == PAPER_PORTS[port]

    @pytest.mark.parametrize("port", sorted(LIVE_PORTS))
    def test_live_ports_are_refused_by_name(self, port: int, tmp_path: Path) -> None:
        with pytest.raises(UnsafeConfigError) as caught:
            EngineConfig(account_id=ACCOUNT, port=port, state_dir=tmp_path)
        # The message must say which live venue it was, or the operator has to
        # go and look up what 7496 means while something is going wrong.
        assert LIVE_PORTS[port] in str(caught.value)
        assert "LIVE" in str(caught.value)

    def test_an_unknown_port_is_refused_rather_than_assumed_safe(self, tmp_path: Path) -> None:
        with pytest.raises(UnsafeConfigError):
            EngineConfig(account_id=ACCOUNT, port=9999, state_dir=tmp_path)

    def test_live_refusal_is_a_distinct_type_from_ordinary_config_error(
        self, tmp_path: Path
    ) -> None:
        # A broad `except ConfigError` that shrugs and uses a default must not
        # be able to swallow "you pointed at a live account".
        assert issubclass(UnsafeConfigError, ConfigError)
        with pytest.raises(UnsafeConfigError):
            EngineConfig(account_id=ACCOUNT, port=7496, state_dir=tmp_path)

    def test_the_env_path_is_gated_too(self, tmp_path: Path) -> None:
        # Not just the constructor: the route operators actually use.
        env = {"IBKR_ACCOUNT_ID": ACCOUNT, "IBKR_PORT": "7496"}
        with pytest.raises(UnsafeConfigError):
            EngineConfig.from_env(env, state_dir=tmp_path)


class TestAccountInterlock:
    def test_a_missing_account_id_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as caught:
            EngineConfig(account_id="", state_dir=tmp_path)
        assert "IBKR_ACCOUNT_ID" in str(caught.value)

    def test_a_whitespace_only_account_id_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            EngineConfig(account_id="   ", state_dir=tmp_path)

    def test_an_untrimmed_account_id_is_refused(self, tmp_path: Path) -> None:
        # Silently trimming would mean the id we assert against the broker is
        # not the one the operator wrote down.
        with pytest.raises(ConfigError):
            EngineConfig(account_id=" DU1234567 ", state_dir=tmp_path)


class TestCaps:
    @pytest.mark.parametrize(
        "field",
        ["max_order_notional", "max_position_qty", "max_orders_per_session", "max_margin_impact"],
    )
    @pytest.mark.parametrize("value", [0, -1])
    def test_a_non_positive_cap_is_refused(
        self, field: str, value: float, tmp_path: Path
    ) -> None:
        with pytest.raises(ConfigError):
            EngineConfig(account_id=ACCOUNT, state_dir=tmp_path, **{field: value})

    def test_an_empty_symbol_allowlist_is_refused(self, tmp_path: Path) -> None:
        # An empty allowlist read as "allow everything" is the classic
        # fail-open bug. Assert it fails closed.
        with pytest.raises(ConfigError):
            EngineConfig(account_id=ACCOUNT, state_dir=tmp_path, symbol_allowlist=())


class TestFromEnv:
    def test_values_are_read_from_the_environment(self, tmp_path: Path) -> None:
        env = {
            "IBKR_ACCOUNT_ID": ACCOUNT,
            "IBKR_PORT": "4002",
            "IBKR_HOST": "10.0.0.5",
            "IBKR_CLIENT_ID": "9",
            "IBKR_MAX_ORDER_NOTIONAL": "250.5",
            "IBKR_SYMBOL_ALLOWLIST": "spy, aapl ,msft",
        }
        config = EngineConfig.from_env(env, state_dir=tmp_path)
        assert (config.port, config.host, config.client_id) == (4002, "10.0.0.5", 9)
        assert config.max_order_notional == 250.5
        assert config.symbol_allowlist == ("SPY", "AAPL", "MSFT")

    def test_overrides_beat_the_environment(self, tmp_path: Path) -> None:
        env = {"IBKR_ACCOUNT_ID": ACCOUNT, "IBKR_PORT": "7497"}
        config = EngineConfig.from_env(env, port=4002, state_dir=tmp_path)
        assert config.port == 4002

    def test_a_malformed_number_is_refused_not_defaulted(self, tmp_path: Path) -> None:
        env = {"IBKR_ACCOUNT_ID": ACCOUNT, "IBKR_PORT": "not-a-port"}
        with pytest.raises(ConfigError):
            EngineConfig.from_env(env, state_dir=tmp_path)

    def test_describe_never_leaks_anything_secret(self, tmp_path: Path) -> None:
        config = EngineConfig(account_id=ACCOUNT, state_dir=tmp_path)
        text = config.describe()
        assert ACCOUNT in text and "7497" in text
        # The engine holds no credentials, and describe() is printed on every
        # run -- assert it stays that way.
        assert "password" not in text.lower() and "token" not in text.lower()
