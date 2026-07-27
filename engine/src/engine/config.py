"""Engine configuration -- and the interlocks that keep this a paper-only tool.

Read this module first. Everything else assumes it has already run.

The central decision: **live trading is not a flag away.** There is no
``--live``, no ``allow_live=True``, no environment variable that flips it. The
only route to a live account is editing :data:`PAPER_PORTS` in this file, in a
diff, under review. That is deliberate -- a config toggle is exactly the kind of
thing that gets set "just to test something" at 1am.

Two independent gates, because either one alone has a plausible failure mode:

``port``        A live TWS/Gateway refuses to serve a paper port, so a correct
                port is strong evidence of a paper endpoint. But ports are
                operator-configurable, so this is necessary and not sufficient.
``account_id``  The operator states which account they expect, and the broker
                is asked to confirm it (see :mod:`engine.broker`). This catches
                the case where someone has reconfigured TWS to serve a live
                account on 7497.

Deliberately *not* used as a gate: the folklore that paper accounts start with
``DU``. It is widely repeated, this session did not verify it against IBKR
documentation, and a safety check nobody has confirmed is worse than no check --
it invites trust it has not earned. The account pin above achieves the same end
without depending on it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError, UnsafeConfigError

# Ports the engine will connect to. Paper endpoints only.
PAPER_PORTS = {
    7497: "TWS paper",
    4002: "IB Gateway paper",
}

# Named solely so the refusal can say *why*. Never connected to.
LIVE_PORTS = {
    7496: "TWS live",
    4001: "IB Gateway live",
}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7497
DEFAULT_CLIENT_ID = 17

# Caps are intentionally tiny. This is a walking skeleton: the first order it
# ever places should be one share of something liquid. Raise them deliberately.
DEFAULT_MAX_ORDER_NOTIONAL = 1_000.0
DEFAULT_MAX_POSITION_QTY = 10
DEFAULT_MAX_ORDERS_PER_SESSION = 5
DEFAULT_MAX_MARGIN_IMPACT = 5_000.0
DEFAULT_SYMBOL_ALLOWLIST = ("SPY", "AAPL", "MSFT")

ENV_PREFIX = "IBKR_"


@dataclass(frozen=True)
class EngineConfig:
    """Validated engine settings. Constructing one is itself a safety check."""

    account_id: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    client_id: int = DEFAULT_CLIENT_ID

    max_order_notional: float = DEFAULT_MAX_ORDER_NOTIONAL
    max_position_qty: int = DEFAULT_MAX_POSITION_QTY
    max_orders_per_session: int = DEFAULT_MAX_ORDERS_PER_SESSION
    max_margin_impact: float = DEFAULT_MAX_MARGIN_IMPACT
    symbol_allowlist: tuple[str, ...] = DEFAULT_SYMBOL_ALLOWLIST

    state_dir: Path = field(default_factory=lambda: Path.cwd() / ".engine")
    project: str = "ibkr"
    connect_timeout: float = 10.0

    # -- derived paths ---------------------------------------------------

    @property
    def journal_path(self) -> Path:
        """Append-only order journal. Never rotated; see :mod:`engine.journal`."""
        return self.state_dir / "orders.jsonl"

    @property
    def halt_file(self) -> Path:
        """Kill switch. Its mere existence stops every order."""
        return self.state_dir / "HALT"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "engine.lock"

    @property
    def venue(self) -> str:
        return PAPER_PORTS.get(self.port, "unknown")

    # -- validation ------------------------------------------------------

    def __post_init__(self) -> None:
        self._check_port()
        self._check_account()
        self._check_caps()

    def _check_port(self) -> None:
        if self.port in PAPER_PORTS:
            return
        if self.port in LIVE_PORTS:
            raise UnsafeConfigError(
                f"port {self.port} is {LIVE_PORTS[self.port]} -- a LIVE trading endpoint",
                hint=(
                    "this engine connects to paper endpoints only "
                    f"({', '.join(f'{p} ({n})' for p, n in sorted(PAPER_PORTS.items()))}). "
                    "Reaching a live account requires editing PAPER_PORTS in engine/config.py, "
                    "in a reviewed diff -- not a setting."
                ),
            )
        raise UnsafeConfigError(
            f"port {self.port} is not a known paper endpoint",
            hint=(
                "refusing an unrecognised port rather than assuming it is safe. "
                f"Known paper ports: {', '.join(str(p) for p in sorted(PAPER_PORTS))}"
            ),
        )

    def _check_account(self) -> None:
        if not self.account_id or not self.account_id.strip():
            raise ConfigError(
                f"no account id configured (set {ENV_PREFIX}ACCOUNT_ID)",
                hint=(
                    "the engine asserts the broker is serving exactly this account before "
                    "it will do anything. Find it in TWS: Account > Account Window."
                ),
            )
        if self.account_id != self.account_id.strip():
            raise ConfigError(f"account id {self.account_id!r} has surrounding whitespace")

    def _check_caps(self) -> None:
        numbers = {
            "max_order_notional": self.max_order_notional,
            "max_position_qty": self.max_position_qty,
            "max_orders_per_session": self.max_orders_per_session,
            "max_margin_impact": self.max_margin_impact,
        }
        for name, value in numbers.items():
            if value <= 0:
                raise ConfigError(
                    f"{name} must be greater than zero (got {value!r})",
                    hint="a cap of zero or less would disable the check it exists to perform",
                )
        if not self.symbol_allowlist:
            raise ConfigError(
                "symbol_allowlist is empty",
                hint=(
                    "an empty allowlist would permit every symbol. To trade nothing, "
                    "use the HALT file; to widen it, name the symbols explicitly."
                ),
            )

    # -- construction ----------------------------------------------------

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **overrides: object) -> "EngineConfig":
        """Build from ``IBKR_*`` environment variables, with explicit overrides.

        Overrides win over the environment so tests and the CLI can be explicit
        without mutating global state.
        """
        source = os.environ if env is None else env

        values: dict[str, object] = {
            "account_id": source.get(f"{ENV_PREFIX}ACCOUNT_ID", "").strip(),
            "host": source.get(f"{ENV_PREFIX}HOST", DEFAULT_HOST).strip() or DEFAULT_HOST,
            "port": _int(source, f"{ENV_PREFIX}PORT", DEFAULT_PORT),
            "client_id": _int(source, f"{ENV_PREFIX}CLIENT_ID", DEFAULT_CLIENT_ID),
            "max_order_notional": _float(
                source, f"{ENV_PREFIX}MAX_ORDER_NOTIONAL", DEFAULT_MAX_ORDER_NOTIONAL
            ),
            "max_position_qty": _int(
                source, f"{ENV_PREFIX}MAX_POSITION_QTY", DEFAULT_MAX_POSITION_QTY
            ),
            "max_orders_per_session": _int(
                source, f"{ENV_PREFIX}MAX_ORDERS_PER_SESSION", DEFAULT_MAX_ORDERS_PER_SESSION
            ),
            "max_margin_impact": _float(
                source, f"{ENV_PREFIX}MAX_MARGIN_IMPACT", DEFAULT_MAX_MARGIN_IMPACT
            ),
            "project": source.get(f"{ENV_PREFIX}PROJECT", "ibkr").strip() or "ibkr",
        }

        allowlist = source.get(f"{ENV_PREFIX}SYMBOL_ALLOWLIST", "").strip()
        if allowlist:
            symbols = tuple(
                part.strip().upper() for part in allowlist.split(",") if part.strip()
            )
            values["symbol_allowlist"] = symbols or DEFAULT_SYMBOL_ALLOWLIST

        state_dir = source.get(f"{ENV_PREFIX}STATE_DIR", "").strip()
        if state_dir:
            values["state_dir"] = Path(state_dir).expanduser()

        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    def describe(self) -> str:
        """One-screen summary, printed before anything is done. No secrets here."""
        return "\n".join(
            [
                f"  venue      {self.venue} ({self.host}:{self.port})",
                f"  account    {self.account_id}",
                f"  client id  {self.client_id}",
                f"  caps       <= {self.max_order_notional:,.2f} notional/order, "
                f"<= {self.max_position_qty} qty/symbol, "
                f"<= {self.max_orders_per_session} orders/session",
                f"  margin cap <= {self.max_margin_impact:,.2f}",
                f"  symbols    {', '.join(self.symbol_allowlist)}",
                f"  state      {self.state_dir}",
            ]
        )


def _int(source: dict[str, str] | os._Environ, key: str, default: int) -> int:
    raw = (source.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key}={raw!r} is not an integer") from None


def _float(source: dict[str, str] | os._Environ, key: str, default: float) -> float:
    raw = (source.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{key}={raw!r} is not a number") from None
