"""What the book currently holds, as the governor needs to see it.

Separate from :mod:`engine.options.governor` so that the governor and the port
that supplies it can both name these types without either importing the other.

**The reported total wins when it is larger.** A snapshot carries both a
per-position breakdown and, optionally, the broker's own total buying-power
figure. They can legitimately disagree: the engine knows about the option
structures it opened, and the broker knows about everything -- equity positions,
a structure opened by hand in TWS, a residual leg from an assignment. So
:attr:`PortfolioSnapshot.total_buying_power_reserved` takes the **maximum** of
the two rather than trusting either.

**What that rule does not do.** It bounds the book's *total* commitment only.
:meth:`PortfolioSnapshot.buying_power_for_underlying` and
:meth:`PortfolioSnapshot.buying_power_where` iterate ``positions`` alone, because
a reported total carries no attribution -- there is no way to know which
underlying, sector or correlation group an unexplained excess belongs to, and
assigning it to the candidate's own bucket would be an invention. The consequence
is precise and worth stating plainly: buying power the engine cannot account for
tightens the total-BPR check and is invisible to the three concentration checks.
Closing that gap needs per-position attribution from a store of open structures,
which does not exist yet -- see
:class:`engine.options.adapters.IBKRPortfolioStateAdapter`.

**Staleness is the caller's decision, not a hidden default.** ``age_at`` returns
the age and nothing more; the governor compares it against a configured maximum
and refuses. This module has no opinion about how old is too old and reads no
clock of its own, so a snapshot's verdict is reproducible from its own fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from ..errors import InvalidPortfolioStateError

__all__ = [
    "PositionExposure",
    "PortfolioSnapshot",
]


def _refuse(message: str, *, hint: str | None = None) -> None:
    raise InvalidPortfolioStateError(message, hint=hint)


def _amount(value: object, label: str, *, allow_zero: bool = True) -> Decimal:
    if not isinstance(value, Decimal):
        _refuse(
            f"{label} must be a Decimal, got {type(value).__name__}",
            hint="binary floats do not represent money exactly, and these figures "
            "are compared against risk caps",
        )
    if not value.is_finite():  # type: ignore[union-attr]
        _refuse(f"{label} must be finite, got {value!r}")
    if value < 0:  # type: ignore[operator]
        _refuse(f"{label} must not be negative, got {value}")
    if not allow_zero and value == 0:  # type: ignore[operator]
        _refuse(f"{label} must be greater than zero, got {value}")
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class PositionExposure:
    """One open structure's contribution to the book's risk.

    ``buying_power_reserved`` is what the broker is holding against it, and
    ``maximum_loss`` is the defined-risk worst case. They are both recorded
    because the governor caps buying power while a human reading the journal
    wants to know what could actually be lost, and deriving either from the other
    is only valid for some structures.
    """

    underlying: str
    buying_power_reserved: Decimal
    maximum_loss: Decimal
    strategy_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.underlying, str) or not self.underlying.strip():
            _refuse("position underlying must be a non-empty string")
        _amount(self.buying_power_reserved, "buying_power_reserved")
        _amount(self.maximum_loss, "maximum_loss")
        if self.strategy_id is not None and not isinstance(self.strategy_id, UUID):
            _refuse(f"strategy_id must be a UUID, got {type(self.strategy_id).__name__}")

    @property
    def normalized_underlying(self) -> str:
        return self.underlying.strip().upper()


@dataclass(frozen=True)
class PortfolioSnapshot:
    """The account state one governor decision was made against.

    Frozen, and carrying its own ``as_of``, so the record of a decision contains
    the state that produced it rather than a reference to something that has
    since moved.
    """

    as_of: datetime
    net_liquidation: Decimal
    positions: tuple[PositionExposure, ...] = ()
    reported_buying_power_reserved: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.as_of, datetime):
            _refuse(f"as_of must be a datetime, got {type(self.as_of).__name__}")
        if self.as_of.tzinfo is None:
            _refuse(
                "as_of must be timezone-aware",
                hint="a naive timestamp cannot be compared against the decision "
                "time, so staleness could not be established",
            )
        # Zero net liquidation is refused rather than allowed through as a
        # degenerate case: every portfolio cap is a fraction of it, so zero makes
        # every limit zero and the resulting refusals would name the wrong cause.
        _amount(self.net_liquidation, "net_liquidation", allow_zero=False)

        if not isinstance(self.positions, tuple):
            _refuse(f"positions must be a tuple, got {type(self.positions).__name__}")
        for position in self.positions:
            if not isinstance(position, PositionExposure):
                _refuse(
                    f"every position must be a PositionExposure, got "
                    f"{type(position).__name__}"
                )
        if self.reported_buying_power_reserved is not None:
            _amount(
                self.reported_buying_power_reserved, "reported_buying_power_reserved"
            )

    # -- derived ----------------------------------------------------------

    @property
    def derived_buying_power_reserved(self) -> Decimal:
        """Sum over the structures the engine knows about."""
        return sum(
            (p.buying_power_reserved for p in self.positions), start=Decimal("0")
        )

    @property
    def total_buying_power_reserved(self) -> Decimal:
        """The conservative total: the larger of derived and broker-reported.

        See the module docstring. A broker total below the engine's own sum means
        the engine is holding a stale or double-counted view, and a broker total
        above it means there are positions the engine did not open. Taking the
        maximum is wrong in neither direction that matters -- it can only refuse
        a candidate that a more precise figure would have allowed.
        """
        derived = self.derived_buying_power_reserved
        if self.reported_buying_power_reserved is None:
            return derived
        return max(derived, self.reported_buying_power_reserved)

    def buying_power_for_underlying(self, symbol: str) -> Decimal:
        key = symbol.strip().upper()
        return sum(
            (
                p.buying_power_reserved
                for p in self.positions
                if p.normalized_underlying == key
            ),
            start=Decimal("0"),
        )

    def buying_power_where(self, symbols: frozenset[str]) -> Decimal:
        """Reserved buying power across a set of already-normalized symbols.

        Used for sector and correlation-group aggregation: the governor resolves
        which symbols belong to the group and asks for their total, so the
        classification rule stays in the policy and the arithmetic stays here.
        """
        return sum(
            (
                p.buying_power_reserved
                for p in self.positions
                if p.normalized_underlying in symbols
            ),
            start=Decimal("0"),
        )

    @property
    def underlyings(self) -> frozenset[str]:
        return frozenset(p.normalized_underlying for p in self.positions)

    def age_at(self, when: datetime) -> timedelta:
        """How old this snapshot is at a decision time. No clock is read here."""
        return when - self.as_of

    def describe(self) -> str:
        lines = [
            f"  as of          {self.as_of.isoformat()}",
            f"  net liq        {self.net_liquidation}",
            f"  BPR            {self.total_buying_power_reserved} "
            f"(derived {self.derived_buying_power_reserved}, "
            f"reported {self.reported_buying_power_reserved})",
            f"  positions      {len(self.positions)}",
        ]
        for position in self.positions:
            lines.append(
                f"    {position.normalized_underlying:<8} "
                f"BPR {position.buying_power_reserved} "
                f"max loss {position.maximum_loss}"
            )
        return "\n".join(lines)

    def to_record(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "net_liquidation": str(self.net_liquidation),
            "total_buying_power_reserved": str(self.total_buying_power_reserved),
            "derived_buying_power_reserved": str(self.derived_buying_power_reserved),
            "reported_buying_power_reserved": (
                str(self.reported_buying_power_reserved)
                if self.reported_buying_power_reserved is not None
                else None
            ),
            "position_count": len(self.positions),
            "underlyings": sorted(self.underlyings),
        }
