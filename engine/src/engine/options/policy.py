"""Thresholds for the options risk checks and the portfolio governor.

Deliberately **not** fields on :class:`engine.config.EngineConfig`.
``engine.options.__init__`` states the dependency direction: options code may use
the equity engine's config, journal and errors, and the equity path never learns
that options exist. Adding ``max_sector_bpr_fraction`` to ``EngineConfig`` would
invert that, and would put an options-only knob in the object that guards the
paper-port interlock -- the one file that should stay readable in a single sitting.

Three properties this module has to have, because the governor is only as
trustworthy as the numbers it compares against:

**Validated at construction.** A fraction of ``0`` disables the cap it exists to
enforce, and a fraction above ``1`` is almost always a percent that forgot to be
divided. Both are refused here rather than producing a governor that silently
approves everything. There is no ``RiskPolicy`` object in existence whose
thresholds have not been checked.

**Deterministic.** Every threshold is a :class:`~decimal.Decimal` and every
lookup table is a tuple of pairs, so two runs with the same environment produce
byte-identical decisions and journal records. Nothing here reads a clock or
depends on dict ordering. The one float conversion is in :func:`_seconds`, where
``timedelta`` requires one; it is bounded by a finiteness check and quantizes to
microseconds, so a configured age below a microsecond becomes zero and is then
refused as non-positive rather than silently accepted.

**Fail closed by omission.** ``sector_for`` and ``correlation_group_for`` return
``None`` for a symbol the operator has not classified. The governor treats
``None`` as a refusal, not as "unconstrained" -- an unclassified symbol is one
whose concentration nobody has bounded, which is exactly the position that ends
up being six correlated trades wearing six different tickers.

The defaults encode ordinary defined-risk premium-selling practice as a starting
point, not as received wisdom. They are intentionally conservative; raise them
deliberately, in a diff, the same way the port allowlist moves.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from ..errors import ConfigError

__all__ = [
    "POLICY_VERSION",
    "ENV_PREFIX",
    "RiskPolicy",
]

POLICY_VERSION = "options-risk/1"

ENV_PREFIX = "IBKR_OPTIONS_"

# -- candidate-level defaults ------------------------------------------------

DEFAULT_MAX_DEFINED_LOSS_PER_POSITION = Decimal("500")
DEFAULT_MAX_DEFINED_LOSS_FRACTION = Decimal("0.02")
DEFAULT_MAX_BROKER_MARGIN_PER_POSITION = Decimal("500")
DEFAULT_MAX_BROKER_MARGIN_FRACTION = Decimal("0.02")
DEFAULT_STRESS_MOVE_FRACTION = Decimal("0.15")
DEFAULT_MAX_STRESS_LOSS_PER_POSITION = Decimal("500")
DEFAULT_MAX_STRESS_LOSS_FRACTION = Decimal("0.02")
DEFAULT_QUOTE_MAXIMUM_AGE_SECONDS = Decimal("10")

# -- strike-selection and sizing defaults ------------------------------------
#
# "16-delta neutral / 30-delta directional strikes" is the recorded strategy, so
# the two targets are separate fields rather than one number with a comment. A
# single ``target_delta`` would make the neutral and directional cases share a
# knob that must move in opposite directions the moment either is tuned.

DEFAULT_NEUTRAL_TARGET_DELTA = Decimal("0.16")
DEFAULT_DIRECTIONAL_TARGET_DELTA = Decimal("0.30")
DEFAULT_TARGET_WIDTH = Decimal("5")
DEFAULT_RISK_BUDGET_PER_POSITION = Decimal("500")

# -- portfolio-level defaults ------------------------------------------------

DEFAULT_MAX_TOTAL_BPR_FRACTION = Decimal("0.35")
DEFAULT_MAX_INCREMENTAL_BPR_FRACTION = Decimal("0.05")
DEFAULT_MAX_UNDERLYING_BPR_FRACTION = Decimal("0.10")
DEFAULT_MAX_SECTOR_BPR_FRACTION = Decimal("0.15")
DEFAULT_MAX_CORRELATION_GROUP_BPR_FRACTION = Decimal("0.20")
DEFAULT_PORTFOLIO_SNAPSHOT_MAXIMUM_AGE_SECONDS = Decimal("60")

# -- position-management defaults --------------------------------------------

# Half the credit, bought back. The mechanical reason is that the last half of a
# credit takes far longer to earn than the first and is exposed to gamma the
# whole time; closing at 50% frees the buying power while the position still has
# a comfortable distance to its short strike.
DEFAULT_PROFIT_TARGET_FRACTION = Decimal("0.50")
# 21 days is where gamma stops being a second-order term. The same adverse move
# costs several times more here than at 45 DTE, so the defined-risk maximum stops
# being a comfortable bound and becomes something a single session can reach.
DEFAULT_MANAGEMENT_DTE = 21
# Exit rather than roll by default: a roll is a new position wearing an old
# position's story, and the engine cannot yet prove the follow-on structure was
# validated as its own trade.
DEFAULT_ROLL_AT_MANAGEMENT_DTE = False

# Only the symbols the equity allowlist already names. An unclassified symbol
# fails closed at the governor, so a short map is a narrow scope, not a hole.
DEFAULT_SECTORS: tuple[tuple[str, str], ...] = (
    ("SPY", "BROAD_MARKET"),
    ("AAPL", "TECHNOLOGY"),
    ("MSFT", "TECHNOLOGY"),
)

DEFAULT_CORRELATION_GROUPS: tuple[tuple[str, str], ...] = (
    ("SPY", "US_LARGE_CAP"),
    ("AAPL", "US_LARGE_CAP"),
    ("MSFT", "US_LARGE_CAP"),
)


def _refuse(message: str, *, hint: str | None = None) -> None:
    raise ConfigError(message, hint=hint)


def _check_fraction(value: Decimal, label: str) -> None:
    """A fraction of account equity. Must land in ``(0, 1]``.

    ``0`` is refused rather than read as "allow nothing": a cap of zero disables
    the check by making it unsatisfiable, and an operator who wants to trade
    nothing has the HALT file. Above ``1`` is refused because it is nearly always
    a percentage that was not divided by a hundred, and the failure mode of that
    typo is a governor that approves every candidate it is ever shown.
    """
    if not isinstance(value, Decimal):
        _refuse(f"{label} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        _refuse(f"{label} must be finite, got {value}")
    if value <= 0:
        _refuse(
            f"{label} must be greater than zero, got {value}",
            hint="a cap of zero disables the check it exists to perform; "
            "to trade nothing, use the HALT file",
        )
    if value > 1:
        _refuse(
            f"{label} must not exceed 1, got {value}",
            hint="this is a fraction of net liquidation, not a percentage -- "
            "15% is 0.15",
        )


def _check_amount(value: Decimal, label: str) -> None:
    if not isinstance(value, Decimal):
        _refuse(f"{label} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        _refuse(f"{label} must be finite, got {value}")
    if value <= 0:
        _refuse(
            f"{label} must be greater than zero, got {value}",
            hint="a cap of zero or less would disable the check it exists to perform",
        )


def _check_target_delta(value: Decimal, label: str) -> None:
    """A short-strike delta target, as a magnitude. Must land in ``(0, 1)``.

    Stated as a magnitude rather than signed, because put deltas are negative and
    call deltas positive: a signed target would have to be written twice, once
    per right, and the two copies would drift.

    Both ends are exclusive, and for different reasons. ``0`` targets a contract
    with no exposure to the underlying -- the selector would walk to the furthest
    listed strike and sell a contract worth nothing, which collects no premium to
    justify the wing that protects it. ``1`` targets a deep in-the-money strike,
    which is not a premium-selling short strike at all; it is a synthetic
    position in the underlying wearing an option's name.
    """
    if not isinstance(value, Decimal):
        _refuse(f"{label} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        _refuse(f"{label} must be finite, got {value}")
    if not Decimal("0") < value < Decimal("1"):
        _refuse(
            f"{label} must be between 0 and 1 exclusive, got {value}",
            hint="this is the absolute value of the short strike's delta -- "
            "0.16 is a 16-delta short",
        )


def _check_pairs(pairs: tuple[tuple[str, str], ...], label: str) -> None:
    if not isinstance(pairs, tuple):
        _refuse(f"{label} must be a tuple of pairs, got {type(pairs).__name__}")
    seen: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:
            _refuse(f"{label} entries must be (symbol, label) pairs, got {pair!r}")
        symbol, classification = pair
        if not isinstance(symbol, str) or not symbol.strip():
            _refuse(f"{label} has an entry with an empty symbol: {pair!r}")
        if not isinstance(classification, str) or not classification.strip():
            _refuse(f"{label} has no classification for {symbol!r}")
        key = symbol.strip().upper()
        if key in seen:
            _refuse(
                f"{label} classifies {key} more than once",
                hint="a symbol with two classifications makes concentration "
                "arithmetic depend on which one is read first",
            )
        seen.add(key)


@dataclass(frozen=True)
class RiskPolicy:
    """Every threshold the candidate checks and the governor compare against.

    Frozen and built from tuples rather than dicts so an instance is hashable and
    cannot be edited after a decision has been made against it -- the policy that
    approved a candidate is the policy the journal records.
    """

    # -- candidate-level -------------------------------------------------
    max_defined_loss_per_position: Decimal = DEFAULT_MAX_DEFINED_LOSS_PER_POSITION
    max_defined_loss_fraction: Decimal = DEFAULT_MAX_DEFINED_LOSS_FRACTION
    max_broker_margin_per_position: Decimal = DEFAULT_MAX_BROKER_MARGIN_PER_POSITION
    max_broker_margin_fraction: Decimal = DEFAULT_MAX_BROKER_MARGIN_FRACTION
    stress_move_fraction: Decimal = DEFAULT_STRESS_MOVE_FRACTION
    max_stress_loss_per_position: Decimal = DEFAULT_MAX_STRESS_LOSS_PER_POSITION
    max_stress_loss_fraction: Decimal = DEFAULT_MAX_STRESS_LOSS_FRACTION
    quote_maximum_age: timedelta = timedelta(seconds=10)

    # -- portfolio-level -------------------------------------------------
    max_total_bpr_fraction: Decimal = DEFAULT_MAX_TOTAL_BPR_FRACTION
    max_incremental_bpr_fraction: Decimal = DEFAULT_MAX_INCREMENTAL_BPR_FRACTION
    max_underlying_bpr_fraction: Decimal = DEFAULT_MAX_UNDERLYING_BPR_FRACTION
    max_sector_bpr_fraction: Decimal = DEFAULT_MAX_SECTOR_BPR_FRACTION
    max_correlation_group_bpr_fraction: Decimal = (
        DEFAULT_MAX_CORRELATION_GROUP_BPR_FRACTION
    )
    portfolio_snapshot_maximum_age: timedelta = timedelta(seconds=60)

    # -- strike selection and sizing -------------------------------------
    neutral_target_delta: Decimal = DEFAULT_NEUTRAL_TARGET_DELTA
    directional_target_delta: Decimal = DEFAULT_DIRECTIONAL_TARGET_DELTA
    target_width: Decimal = DEFAULT_TARGET_WIDTH
    risk_budget_per_position: Decimal = DEFAULT_RISK_BUDGET_PER_POSITION

    # -- position management ---------------------------------------------
    profit_target_fraction: Decimal = DEFAULT_PROFIT_TARGET_FRACTION
    management_dte: int = DEFAULT_MANAGEMENT_DTE
    roll_at_management_dte: bool = DEFAULT_ROLL_AT_MANAGEMENT_DTE

    # -- classification --------------------------------------------------
    sectors: tuple[tuple[str, str], ...] = DEFAULT_SECTORS
    correlation_groups: tuple[tuple[str, str], ...] = DEFAULT_CORRELATION_GROUPS

    version: str = POLICY_VERSION

    # -- validation ------------------------------------------------------

    def __post_init__(self) -> None:
        for label in (
            "max_defined_loss_per_position",
            "max_broker_margin_per_position",
            "max_stress_loss_per_position",
            # A width of zero is a short leg with its protection on the same
            # strike -- no protection at all -- and a risk budget of zero sizes
            # every candidate to nothing, which reads in a report as "the market
            # offered nothing" rather than "the budget was misconfigured".
            "target_width",
            "risk_budget_per_position",
        ):
            _check_amount(getattr(self, label), label)

        for label in ("neutral_target_delta", "directional_target_delta"):
            _check_target_delta(getattr(self, label), label)

        for label in (
            "max_defined_loss_fraction",
            "max_broker_margin_fraction",
            "max_stress_loss_fraction",
            "max_total_bpr_fraction",
            "max_incremental_bpr_fraction",
            "max_underlying_bpr_fraction",
            "max_sector_bpr_fraction",
            "max_correlation_group_bpr_fraction",
        ):
            _check_fraction(getattr(self, label), label)

        # The stress move is a magnitude, and a move of 100% or more would take
        # the underlying to zero or double it -- past the point where a terminal
        # payoff on an equity option says anything useful.
        if not isinstance(self.stress_move_fraction, Decimal):
            _refuse("stress_move_fraction must be a Decimal")
        if not self.stress_move_fraction.is_finite():
            _refuse(f"stress_move_fraction must be finite, got {self.stress_move_fraction}")
        if not Decimal("0") < self.stress_move_fraction < Decimal("1"):
            _refuse(
                f"stress_move_fraction must be between 0 and 1 exclusive, "
                f"got {self.stress_move_fraction}",
                hint="0.15 is a 15% adverse move in the underlying",
            )

        for label in ("quote_maximum_age", "portfolio_snapshot_maximum_age"):
            age = getattr(self, label)
            if not isinstance(age, timedelta):
                _refuse(f"{label} must be a timedelta, got {type(age).__name__}")
            if age <= timedelta(0):
                _refuse(
                    f"{label} must be positive, got {age}",
                    hint="a non-positive maximum age would reject every quote, "
                    "including a fresh one",
                )

        # An incremental cap above the total cap can never bind, which makes it
        # look like a control while doing nothing.
        if self.max_incremental_bpr_fraction > self.max_total_bpr_fraction:
            _refuse(
                f"max_incremental_bpr_fraction {self.max_incremental_bpr_fraction} "
                f"exceeds max_total_bpr_fraction {self.max_total_bpr_fraction}",
                hint="a single position may not be allowed more buying power than "
                "the whole book",
            )

        # The profit target is a fraction of maximum profit, and both ends are
        # exclusive. ``0`` is a target that is met the instant the position is
        # opened, which would close every trade for its own credit; ``1`` waits
        # for the last cent of extrinsic value, which is the part that takes
        # longest to earn and is exposed to gamma the whole time -- a target
        # that in practice is only reached at expiry, which is the outcome the
        # management rules exist to avoid.
        if not isinstance(self.profit_target_fraction, Decimal):
            _refuse(
                f"profit_target_fraction must be a Decimal, got "
                f"{type(self.profit_target_fraction).__name__}"
            )
        if not self.profit_target_fraction.is_finite():
            _refuse(
                f"profit_target_fraction must be finite, got "
                f"{self.profit_target_fraction}"
            )
        if not Decimal("0") < self.profit_target_fraction < Decimal("1"):
            _refuse(
                f"profit_target_fraction must be between 0 and 1 exclusive, got "
                f"{self.profit_target_fraction}",
                hint="0.50 takes half the credit collected; a 1.50 credit is "
                "then bought back at 0.75",
            )

        # bool is a subclass of int, so ``roll_at_management_dte=21`` and
        # ``management_dte=True`` would both sail through an isinstance check
        # written the obvious way -- and True as a DTE means every position is
        # managed one day before expiry.
        if not isinstance(self.management_dte, int) or isinstance(
            self.management_dte, bool
        ):
            _refuse(
                f"management_dte must be an int, got "
                f"{type(self.management_dte).__name__}"
            )
        if self.management_dte <= 0:
            _refuse(
                f"management_dte must be greater than zero, got {self.management_dte}",
                hint="a threshold of zero only fires on expiration day itself, "
                "which is after the gamma this rule exists to avoid",
            )

        if not isinstance(self.roll_at_management_dte, bool):
            _refuse(
                f"roll_at_management_dte must be a bool, got "
                f"{type(self.roll_at_management_dte).__name__}"
            )

        _check_pairs(self.sectors, "sectors")
        _check_pairs(self.correlation_groups, "correlation_groups")

        if not isinstance(self.version, str) or not self.version.strip():
            _refuse("version must be a non-empty string")

    # -- classification lookups ------------------------------------------

    def sector_for(self, symbol: str) -> str | None:
        """The operator's sector for this symbol, or ``None`` if unclassified.

        ``None`` is not "no sector constraint". The governor refuses a candidate
        whose sector it cannot determine, because an unclassified symbol is one
        whose concentration has never been bounded by anybody.
        """
        key = symbol.strip().upper()
        for candidate, sector in self.sectors:
            if candidate.strip().upper() == key:
                return sector
        return None

    def correlation_group_for(self, symbol: str) -> str | None:
        """The operator's correlation group for this symbol, or ``None``.

        Same contract as :meth:`sector_for`: unknown means refuse, never allow.
        """
        key = symbol.strip().upper()
        for candidate, group in self.correlation_groups:
            if candidate.strip().upper() == key:
                return group
        return None

    # -- construction ----------------------------------------------------

    @classmethod
    def from_env(
        cls, env: dict[str, str] | None = None, **overrides: object
    ) -> "RiskPolicy":
        """Build from ``IBKR_OPTIONS_*``, with explicit overrides winning.

        Mirrors :meth:`engine.config.EngineConfig.from_env` so there is one
        convention in the codebase rather than two.
        """
        source = os.environ if env is None else env

        values: dict[str, object] = {
            "max_defined_loss_per_position": _decimal(
                source,
                f"{ENV_PREFIX}MAX_DEFINED_LOSS_PER_POSITION",
                DEFAULT_MAX_DEFINED_LOSS_PER_POSITION,
            ),
            "max_defined_loss_fraction": _decimal(
                source,
                f"{ENV_PREFIX}MAX_DEFINED_LOSS_FRACTION",
                DEFAULT_MAX_DEFINED_LOSS_FRACTION,
            ),
            "max_broker_margin_per_position": _decimal(
                source,
                f"{ENV_PREFIX}MAX_BROKER_MARGIN_PER_POSITION",
                DEFAULT_MAX_BROKER_MARGIN_PER_POSITION,
            ),
            "max_broker_margin_fraction": _decimal(
                source,
                f"{ENV_PREFIX}MAX_BROKER_MARGIN_FRACTION",
                DEFAULT_MAX_BROKER_MARGIN_FRACTION,
            ),
            "stress_move_fraction": _decimal(
                source, f"{ENV_PREFIX}STRESS_MOVE_FRACTION", DEFAULT_STRESS_MOVE_FRACTION
            ),
            "max_stress_loss_per_position": _decimal(
                source,
                f"{ENV_PREFIX}MAX_STRESS_LOSS_PER_POSITION",
                DEFAULT_MAX_STRESS_LOSS_PER_POSITION,
            ),
            "max_stress_loss_fraction": _decimal(
                source,
                f"{ENV_PREFIX}MAX_STRESS_LOSS_FRACTION",
                DEFAULT_MAX_STRESS_LOSS_FRACTION,
            ),
            "quote_maximum_age": _seconds(
                source,
                f"{ENV_PREFIX}QUOTE_MAXIMUM_AGE_SECONDS",
                DEFAULT_QUOTE_MAXIMUM_AGE_SECONDS,
            ),
            "max_total_bpr_fraction": _decimal(
                source, f"{ENV_PREFIX}MAX_TOTAL_BPR_FRACTION", DEFAULT_MAX_TOTAL_BPR_FRACTION
            ),
            "max_incremental_bpr_fraction": _decimal(
                source,
                f"{ENV_PREFIX}MAX_INCREMENTAL_BPR_FRACTION",
                DEFAULT_MAX_INCREMENTAL_BPR_FRACTION,
            ),
            "max_underlying_bpr_fraction": _decimal(
                source,
                f"{ENV_PREFIX}MAX_UNDERLYING_BPR_FRACTION",
                DEFAULT_MAX_UNDERLYING_BPR_FRACTION,
            ),
            "max_sector_bpr_fraction": _decimal(
                source,
                f"{ENV_PREFIX}MAX_SECTOR_BPR_FRACTION",
                DEFAULT_MAX_SECTOR_BPR_FRACTION,
            ),
            "max_correlation_group_bpr_fraction": _decimal(
                source,
                f"{ENV_PREFIX}MAX_CORRELATION_GROUP_BPR_FRACTION",
                DEFAULT_MAX_CORRELATION_GROUP_BPR_FRACTION,
            ),
            "portfolio_snapshot_maximum_age": _seconds(
                source,
                f"{ENV_PREFIX}PORTFOLIO_SNAPSHOT_MAXIMUM_AGE_SECONDS",
                DEFAULT_PORTFOLIO_SNAPSHOT_MAXIMUM_AGE_SECONDS,
            ),
            "neutral_target_delta": _decimal(
                source,
                f"{ENV_PREFIX}NEUTRAL_TARGET_DELTA",
                DEFAULT_NEUTRAL_TARGET_DELTA,
            ),
            "directional_target_delta": _decimal(
                source,
                f"{ENV_PREFIX}DIRECTIONAL_TARGET_DELTA",
                DEFAULT_DIRECTIONAL_TARGET_DELTA,
            ),
            "target_width": _decimal(
                source, f"{ENV_PREFIX}TARGET_WIDTH", DEFAULT_TARGET_WIDTH
            ),
            "risk_budget_per_position": _decimal(
                source,
                f"{ENV_PREFIX}RISK_BUDGET_PER_POSITION",
                DEFAULT_RISK_BUDGET_PER_POSITION,
            ),
            "profit_target_fraction": _decimal(
                source,
                f"{ENV_PREFIX}PROFIT_TARGET_FRACTION",
                DEFAULT_PROFIT_TARGET_FRACTION,
            ),
            "management_dte": _int(
                source, f"{ENV_PREFIX}MANAGEMENT_DTE", DEFAULT_MANAGEMENT_DTE
            ),
            "roll_at_management_dte": _bool(
                source,
                f"{ENV_PREFIX}ROLL_AT_MANAGEMENT_DTE",
                DEFAULT_ROLL_AT_MANAGEMENT_DTE,
            ),
        }

        sectors = _pairs(source, f"{ENV_PREFIX}SECTORS")
        if sectors is not None:
            values["sectors"] = sectors

        groups = _pairs(source, f"{ENV_PREFIX}CORRELATION_GROUPS")
        if groups is not None:
            values["correlation_groups"] = groups

        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    def describe(self) -> str:
        """One-screen summary for the scan report. No secrets, no clock reads."""
        return "\n".join(
            [
                f"  policy         {self.version}",
                f"  per position   <= {self.max_defined_loss_per_position} defined loss "
                f"({self.max_defined_loss_fraction} of net liq)",
                f"  broker margin  <= {self.max_broker_margin_per_position} "
                f"({self.max_broker_margin_fraction} of net liq)",
                f"  stress         {self.stress_move_fraction} move, "
                f"<= {self.max_stress_loss_per_position} loss "
                f"({self.max_stress_loss_fraction} of net liq)",
                f"  portfolio BPR  <= {self.max_total_bpr_fraction} total, "
                f"<= {self.max_incremental_bpr_fraction} incremental",
                f"  concentration  <= {self.max_underlying_bpr_fraction} underlying, "
                f"<= {self.max_sector_bpr_fraction} sector, "
                f"<= {self.max_correlation_group_bpr_fraction} correlation group",
                f"  strikes        {self.neutral_target_delta} delta neutral, "
                f"{self.directional_target_delta} delta directional, "
                f"{self.target_width} wide",
                f"  sizing         {self.risk_budget_per_position} risk budget "
                f"per position",
                f"  management     take {self.profit_target_fraction} of max profit, "
                f"{'roll' if self.roll_at_management_dte else 'exit'} at "
                f"{self.management_dte} DTE",
                f"  max ages       quote {self.quote_maximum_age}, "
                f"portfolio {self.portfolio_snapshot_maximum_age}",
                f"  classified     {len(self.sectors)} sectors, "
                f"{len(self.correlation_groups)} correlation groups",
            ]
        )

    def to_record(self) -> dict[str, str]:
        """What the journal stores so a past decision can be re-derived.

        The classification maps are included, not just the numeric caps. A
        sector- or correlation-concentration refusal is only reproducible if the
        mapping that produced the classification travels with it -- without them
        a future reader can see that ``TECHNOLOGY`` was full but not which
        symbols the engine counted as technology at the time.
        """
        return {
            "version": self.version,
            "sectors": ",".join(f"{s}:{c}" for s, c in self.sectors),
            "correlation_groups": ",".join(
                f"{s}:{c}" for s, c in self.correlation_groups
            ),
            "max_defined_loss_per_position": str(self.max_defined_loss_per_position),
            "max_defined_loss_fraction": str(self.max_defined_loss_fraction),
            "max_broker_margin_per_position": str(self.max_broker_margin_per_position),
            "max_broker_margin_fraction": str(self.max_broker_margin_fraction),
            "stress_move_fraction": str(self.stress_move_fraction),
            "max_stress_loss_per_position": str(self.max_stress_loss_per_position),
            "max_stress_loss_fraction": str(self.max_stress_loss_fraction),
            "max_total_bpr_fraction": str(self.max_total_bpr_fraction),
            "max_incremental_bpr_fraction": str(self.max_incremental_bpr_fraction),
            "max_underlying_bpr_fraction": str(self.max_underlying_bpr_fraction),
            "max_sector_bpr_fraction": str(self.max_sector_bpr_fraction),
            "max_correlation_group_bpr_fraction": str(
                self.max_correlation_group_bpr_fraction
            ),
            "neutral_target_delta": str(self.neutral_target_delta),
            "directional_target_delta": str(self.directional_target_delta),
            "target_width": str(self.target_width),
            "risk_budget_per_position": str(self.risk_budget_per_position),
            "profit_target_fraction": str(self.profit_target_fraction),
            "management_dte": str(self.management_dte),
            "roll_at_management_dte": str(self.roll_at_management_dte),
            "quote_maximum_age_seconds": str(self.quote_maximum_age.total_seconds()),
            "portfolio_snapshot_maximum_age_seconds": str(
                self.portfolio_snapshot_maximum_age.total_seconds()
            ),
        }


def _decimal(
    source: "dict[str, str] | os._Environ[str]", key: str, default: Decimal
) -> Decimal:
    raw = (source.get(key) or "").strip()
    if not raw:
        return default
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ConfigError(
            f"{key}={raw!r} is not a decimal number",
            hint="thresholds are parsed as Decimal so a rounding artefact cannot "
            "move a risk cap",
        ) from None


def _int(source: "dict[str, str] | os._Environ[str]", key: str, default: int) -> int:
    """Parse a whole number of days.

    ``"21.5"`` is refused rather than truncated. A day count that silently loses
    its fraction is a threshold the operator wrote and the engine did not use,
    and the direction of the truncation is not obviously the safe one.
    """
    raw = (source.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(
            f"{key}={raw!r} is not a whole number of days",
            hint="21 means twenty-one calendar days to expiry",
        ) from None


def _bool(source: "dict[str, str] | os._Environ[str]", key: str, default: bool) -> bool:
    """Parse a flag, refusing anything that is not unambiguously one or the other.

    ``bool("false")`` is ``True``, which is the whole reason this exists: the
    obvious implementation turns "roll: false" into "roll: yes", and the
    resulting behaviour -- rolling every position at 21 DTE -- looks like a
    deliberate strategy rather than a parse bug.
    """
    raw = (source.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(
        f"{key}={raw!r} is not a boolean",
        hint="use true/false, yes/no, on/off or 1/0",
    )


def _seconds(
    source: "dict[str, str] | os._Environ[str]", key: str, default: Decimal
) -> timedelta:
    """Parse a duration in seconds into a timedelta.

    The finiteness guard is not decoration. ``Decimal("NaN")`` and
    ``Decimal("Infinity")`` both parse cleanly, and handing either to
    ``timedelta`` raises ``ValueError`` or ``OverflowError`` -- neither of which
    is a :class:`~engine.errors.ConfigError`, so a caller catching ConfigError to
    report a configuration problem would instead see an unhandled traceback.
    """
    seconds = _decimal(source, key, default)
    if not seconds.is_finite():
        raise ConfigError(
            f"{key}={str(seconds)!r} is not a finite number of seconds",
            hint="a non-finite maximum age cannot be compared against a quote's "
            "actual age",
        )
    # Decimal's exponent range is far wider than float's, so a value like
    # ``1e400`` is a perfectly finite Decimal that becomes ``inf`` here. The
    # check above does not catch it, and timedelta then raises OverflowError.
    as_float = float(seconds)
    if not math.isfinite(as_float):
        raise ConfigError(
            f"{key}={str(seconds)!r} is too large to express as a duration",
            hint="the value is a valid Decimal but overflows a float",
        )
    try:
        return timedelta(seconds=as_float)
    except (OverflowError, ValueError) as exc:
        raise ConfigError(
            f"{key}={str(seconds)!r} is not a usable duration: {exc}"
        ) from None


def _pairs(
    source: "dict[str, str] | os._Environ[str]", key: str
) -> tuple[tuple[str, str], ...] | None:
    """Parse ``SPY:BROAD_MARKET,AAPL:TECHNOLOGY`` into validated pairs.

    Returns ``None`` when unset so the caller can keep the default, and refuses a
    malformed entry rather than dropping it -- a silently skipped mapping is a
    symbol that fails closed later for a reason nobody will connect to a typo here.
    """
    raw = (source.get(key) or "").strip()
    if not raw:
        return None
    pairs: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        if item.count(":") != 1:
            raise ConfigError(
                f"{key} entry {item!r} is not in SYMBOL:CLASSIFICATION form",
                hint="for example SPY:BROAD_MARKET,AAPL:TECHNOLOGY",
            )
        symbol, classification = item.split(":")
        if not symbol.strip() or not classification.strip():
            raise ConfigError(f"{key} entry {item!r} has an empty side")
        pairs.append((symbol.strip().upper(), classification.strip().upper()))
    return tuple(pairs)
