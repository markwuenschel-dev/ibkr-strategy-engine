"""Option legs, strategies, and the invariants they must satisfy.

:class:`engine.safety.OrderIntent` names one symbol and one integer quantity. A
vertical cannot be expressed in it at all, and widening it with optional strike
/ expiry / right fields would produce an object whose validity depends on which
fields happen to be set -- exactly the shape that lets an uncovered short slip
past a check written for the equity case. So these are separate types, and the
equity intent is left untouched.

Three decisions carry most of the safety weight here:

**Invariants are enforced in __post_init__, not by a gate.** An invalid
structure never becomes an object. There is no window in which a malformed
strategy exists and is waiting for someone to remember to validate it, and no
ordering dependency between "build" and "check".

**Maximum loss is recomputed and compared, not trusted.** It is a stored field
so the number the governor saw is the number the journal records -- but the
constructor recomputes it from the legs and refuses a mismatch, so a wrong
figure cannot be smuggled past the risk gates by a caller that computed it
badly. Every price, strike, width and loss is :class:`~decimal.Decimal`.

**The IBKR net-credit sign convention lives in the broker adapter, not here.**
Submitting a credit means ``BUY`` at a *negative* limit price, and a ``SELL`` at
a positive price inverts the leg actions and is rejected as a riskless
combination. That is a wire-format concern. Strategy code reasons about a
positive ``limit_price`` plus an explicit :class:`PriceEffect`, and the adapter
is responsible for the translation.

Two adaptations from the specification, both to make a stated invariant
enforceable rather than aspirational:

* ``maximum_loss`` is named ``maximum_loss_per_contract``, with
  :attr:`OptionStrategyIntent.total_maximum_loss` derived. The position-sizing
  formula divides a risk budget by the per-contract figure, so leaving the unit
  implicit invites a quantity-squared error.
* ``closes_strategy_id`` is added. "A close order must reference the persisted
  open strategy" cannot be checked without a field naming it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from ..errors import InvalidStrategyError

__all__ = [
    "OptionRight",
    "OrderAction",
    "PriceEffect",
    "StrategyAction",
    "StrategyType",
    "OptionLegIntent",
    "OptionStrategyIntent",
    "compute_maximum_loss_per_contract",
]


class OptionRight(str, Enum):
    """Whether a contract is a call or a put, in IBKR's single-letter form."""

    CALL = "C"
    PUT = "P"


class OrderAction(str, Enum):
    """What a single leg does. Not to be confused with :class:`StrategyAction`."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def inverted(self) -> OrderAction:
        """The action that closes a leg opened with this one."""
        return OrderAction.SELL if self is OrderAction.BUY else OrderAction.BUY


class StrategyAction(str, Enum):
    """What the strategy as a whole does to the portfolio.

    ``ROLL`` exists because it is the domain's vocabulary, but no code path
    emits it yet: a roll is modelled as a close plus a freshly validated open
    plus a link record, and that is not built until ordinary open, close,
    restart reconciliation and partial fills are proven.
    """

    OPEN = "OPEN"
    CLOSE = "CLOSE"
    ROLL = "ROLL"


class StrategyType(str, Enum):
    """The defined-risk structures supported at this stage. Deliberately three."""

    PUT_CREDIT_SPREAD = "PUT_CREDIT_SPREAD"
    CALL_CREDIT_SPREAD = "CALL_CREDIT_SPREAD"
    IRON_CONDOR = "IRON_CONDOR"


class PriceEffect(str, Enum):
    """Which way money moves. An enum rather than a literal so it cannot be
    compared against an arbitrary string that happens to be spelled right."""

    CREDIT = "CREDIT"
    DEBIT = "DEBIT"


def _refuse(message: str, *, hint: str | None = None) -> None:
    raise InvalidStrategyError(message, hint=hint)


def _positive_int(value: object, label: str) -> int:
    """Reject bools explicitly -- ``isinstance(True, int)`` is True in Python,
    and a ``ratio=True`` would otherwise sail through as 1."""
    if not isinstance(value, int) or isinstance(value, bool):
        _refuse(f"{label} must be an int, got {type(value).__name__}")
    if value <= 0:  # type: ignore[operator]
        _refuse(f"{label} must be positive, got {value!r}")
    return value  # type: ignore[return-value]


def _decimal(value: object, label: str) -> Decimal:
    if not isinstance(value, Decimal):
        _refuse(
            f"{label} must be a Decimal, got {type(value).__name__}",
            hint="binary floats do not represent strikes or credits exactly",
        )
    if not value.is_finite():  # type: ignore[union-attr]
        _refuse(f"{label} must be finite, got {value!r}")
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class OptionLegIntent:
    """One leg of a strategy, pinned to a contract IBKR has already qualified.

    ``multiplier`` has no default on purpose. It comes from the qualified
    contract, and a default of 100 would be silently wrong for every contract
    that has been adjusted for a split or a special dividend -- the kind of
    error that shows up as a risk figure off by a factor nobody notices.
    """

    con_id: int
    symbol: str
    expiration: date
    strike: Decimal
    right: OptionRight
    action: OrderAction
    ratio: int
    multiplier: int
    exchange: str
    trading_class: str | None = None

    def __post_init__(self) -> None:
        # A con_id of 0 is what an unqualified ib_async Contract carries, so
        # this check is the one that proves qualification actually happened.
        _positive_int(self.con_id, "con_id")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            _refuse("leg symbol must be a non-empty string")
        if not isinstance(self.expiration, date) or isinstance(self.expiration, datetime):
            _refuse("leg expiration must be a date, not a datetime")
        strike = _decimal(self.strike, "strike")
        if strike <= 0:
            _refuse(f"strike must be positive, got {strike}")
        if not isinstance(self.right, OptionRight):
            _refuse(f"right must be an OptionRight, got {self.right!r}")
        if not isinstance(self.action, OrderAction):
            _refuse(f"action must be an OrderAction, got {self.action!r}")
        _positive_int(self.ratio, "ratio")
        _positive_int(self.multiplier, "multiplier")
        if not isinstance(self.exchange, str) or not self.exchange.strip():
            _refuse("leg exchange must be a non-empty string")

    @property
    def is_short(self) -> bool:
        return self.action is OrderAction.SELL

    @property
    def is_long(self) -> bool:
        return self.action is OrderAction.BUY

    def inverted(self) -> OptionLegIntent:
        """The same contract, traded the other way, for a closing order.

        Same ``con_id`` -- a close reuses the persisted contract rather than
        looking one up again, so it cannot drift onto a different strike.
        """
        return OptionLegIntent(
            con_id=self.con_id,
            symbol=self.symbol,
            expiration=self.expiration,
            strike=self.strike,
            right=self.right,
            action=self.action.inverted,
            ratio=self.ratio,
            multiplier=self.multiplier,
            exchange=self.exchange,
            trading_class=self.trading_class,
        )

    def describe(self) -> str:
        return (
            f"{self.action.value} {self.ratio}x {self.symbol} "
            f"{self.expiration:%Y-%m-%d} {self.strike} {self.right.value}"
        )


def _sole(
    legs: tuple[OptionLegIntent, ...], right: OptionRight, action: OrderAction
) -> OptionLegIntent | None:
    """The single leg with this right and action, or None if not exactly one."""
    found = [leg for leg in legs if leg.right is right and leg.action is action]
    return found[0] if len(found) == 1 else None


def compute_maximum_loss_per_contract(
    *,
    strategy_type: StrategyType,
    legs: tuple[OptionLegIntent, ...],
    credit: Decimal,
    multiplier: int,
) -> Decimal:
    """Worst case for one contract of this structure, in account currency.

    Raises :class:`~engine.errors.InvalidStrategyError` rather than returning a
    sentinel: a strategy whose maximum loss cannot be computed is not eligible
    for a broker what-if request, and the caller must not be able to proceed by
    forgetting to compare against None.

    An iron condor uses the **wider** wing. Only one side can be breached at
    expiry, so the narrow side never contributes -- but sizing off the narrow
    side would understate the risk of the side that actually loses.
    """
    if credit <= 0:
        _refuse(
            f"entry credit must be positive, got {credit}",
            hint="a credit strategy that collects nothing has no upside to justify its risk",
        )

    if strategy_type is StrategyType.PUT_CREDIT_SPREAD:
        short = _sole(legs, OptionRight.PUT, OrderAction.SELL)
        long = _sole(legs, OptionRight.PUT, OrderAction.BUY)
        if short is None or long is None:
            _refuse("put credit spread needs exactly one short put and one long put")
        widest = short.strike - long.strike  # type: ignore[union-attr]
    elif strategy_type is StrategyType.CALL_CREDIT_SPREAD:
        short = _sole(legs, OptionRight.CALL, OrderAction.SELL)
        long = _sole(legs, OptionRight.CALL, OrderAction.BUY)
        if short is None or long is None:
            _refuse("call credit spread needs exactly one short call and one long call")
        widest = long.strike - short.strike  # type: ignore[union-attr]
    elif strategy_type is StrategyType.IRON_CONDOR:
        short_put = _sole(legs, OptionRight.PUT, OrderAction.SELL)
        long_put = _sole(legs, OptionRight.PUT, OrderAction.BUY)
        short_call = _sole(legs, OptionRight.CALL, OrderAction.SELL)
        long_call = _sole(legs, OptionRight.CALL, OrderAction.BUY)
        if None in (short_put, long_put, short_call, long_call):
            _refuse(
                "iron condor needs exactly one short put, long put, short call and long call"
            )
        put_width = short_put.strike - long_put.strike  # type: ignore[union-attr]
        call_width = long_call.strike - short_call.strike  # type: ignore[union-attr]
        widest = max(put_width, call_width)
    else:  # pragma: no cover - the enum has no fourth member
        _refuse(f"unsupported strategy type {strategy_type!r}")

    if widest <= 0:
        _refuse(f"strategy width must be positive, got {widest}")
    if credit >= widest:
        _refuse(
            f"entry credit {credit} is not less than the widest wing {widest}",
            hint="a credit at or above the width implies a risk-free structure; "
            "IBKR rejects these as riskless combinations",
        )

    return (widest - credit) * multiplier


@dataclass(frozen=True)
class OptionStrategyIntent:
    """A complete, structurally valid, defined-risk option strategy.

    Constructing one is the validation. If you hold an instance, every invariant
    below has already been checked against these exact legs.
    """

    strategy_id: UUID
    strategy_type: StrategyType
    strategy_action: StrategyAction
    underlying: str
    quantity: int
    legs: tuple[OptionLegIntent, ...]
    expiration: date
    limit_price: Decimal
    price_effect: PriceEffect
    maximum_loss_per_contract: Decimal
    configuration_version: str
    created_at: datetime
    estimated_buying_power_change: Decimal | None = None
    closes_strategy_id: UUID | None = None

    def __post_init__(self) -> None:
        self._check_scalars()
        self._check_legs_are_coherent()
        self._check_shape()
        self._check_lifecycle()
        # Strike ordering, coverage and max loss are all stated in terms of an
        # opening position. A closing order carries the same contracts with
        # every action inverted, so its "long" put is the one that was the short
        # -- checking protective ordering against it would reject a correct
        # close. Those legs came from an OPEN that was validated when it was
        # built; re-deriving the checks in the inverted frame would assert
        # nothing new and would forbid the only close we ever want to send.
        if self.strategy_action is StrategyAction.OPEN:
            self._check_strike_ordering()
            self._check_every_short_is_covered()
            self._check_maximum_loss()

    # -- scalar sanity ----------------------------------------------------

    def _check_scalars(self) -> None:
        if not isinstance(self.strategy_id, UUID):
            _refuse(f"strategy_id must be a UUID, got {type(self.strategy_id).__name__}")
        if not isinstance(self.strategy_type, StrategyType):
            _refuse(f"strategy_type must be a StrategyType, got {self.strategy_type!r}")
        if not isinstance(self.strategy_action, StrategyAction):
            _refuse(f"strategy_action must be a StrategyAction, got {self.strategy_action!r}")
        if not isinstance(self.price_effect, PriceEffect):
            _refuse(f"price_effect must be a PriceEffect, got {self.price_effect!r}")
        if not isinstance(self.underlying, str) or not self.underlying.strip():
            _refuse("underlying must be a non-empty string")
        _positive_int(self.quantity, "quantity")
        limit = _decimal(self.limit_price, "limit_price")
        if limit <= 0:
            _refuse(
                f"limit_price must be a positive magnitude, got {limit}",
                hint="direction is carried by price_effect; the IBKR negative-limit "
                "credit convention belongs to the broker adapter",
            )
        _decimal(self.maximum_loss_per_contract, "maximum_loss_per_contract")
        if self.maximum_loss_per_contract < 0:
            _refuse(
                f"maximum_loss_per_contract must not be negative, "
                f"got {self.maximum_loss_per_contract}"
            )
        if self.estimated_buying_power_change is not None:
            _decimal(self.estimated_buying_power_change, "estimated_buying_power_change")
        if not isinstance(self.configuration_version, str) or not self.configuration_version:
            _refuse("configuration_version must be a non-empty string")
        if not isinstance(self.created_at, datetime):
            _refuse("created_at must be a datetime")
        if self.created_at.tzinfo is None:
            _refuse(
                "created_at must be timezone-aware",
                hint="the journal records UTC; a naive timestamp cannot be compared to it",
            )

    # -- leg coherence ----------------------------------------------------

    def _check_legs_are_coherent(self) -> None:
        if not isinstance(self.legs, tuple):
            _refuse(f"legs must be a tuple, got {type(self.legs).__name__}")
        if not self.legs:
            _refuse("a strategy must have at least one leg")
        for leg in self.legs:
            if not isinstance(leg, OptionLegIntent):
                _refuse(f"every leg must be an OptionLegIntent, got {type(leg).__name__}")

        con_ids = [leg.con_id for leg in self.legs]
        if len(set(con_ids)) != len(con_ids):
            _refuse(
                f"legs must reference distinct contracts, got con_ids {con_ids}",
                hint="a duplicated con_id usually means strike selection returned "
                "the same contract twice",
            )

        underlyings = {leg.symbol.strip().upper() for leg in self.legs}
        if underlyings != {self.underlying.strip().upper()}:
            _refuse(
                f"every leg must be on {self.underlying!r}, got {sorted(underlyings)}"
            )

        expirations = {leg.expiration for leg in self.legs}
        if len(expirations) != 1:
            _refuse(
                f"all legs must share one expiration, got {sorted(expirations)}",
                hint="calendars and diagonals are not supported structures",
            )
        if expirations != {self.expiration}:
            _refuse(
                f"strategy expiration {self.expiration} does not match its legs "
                f"{sorted(expirations)}"
            )

        multipliers = {leg.multiplier for leg in self.legs}
        if len(multipliers) != 1:
            _refuse(
                f"all legs must share one multiplier, got {sorted(multipliers)}",
                hint="a mixed-multiplier structure makes the max-loss arithmetic wrong",
            )

        ratios = {leg.ratio for leg in self.legs}
        if ratios != {1}:
            _refuse(
                f"only 1:1 structures are supported, got ratios {sorted(ratios)}",
                hint="ratio spreads carry undefined risk and are out of scope",
            )

    # -- shape per strategy type -----------------------------------------

    def _check_shape(self) -> None:
        expected_legs = 4 if self.strategy_type is StrategyType.IRON_CONDOR else 2
        if len(self.legs) != expected_legs:
            _refuse(
                f"{self.strategy_type.value} must have exactly {expected_legs} legs, "
                f"got {len(self.legs)}"
            )

        shorts = [leg for leg in self.legs if leg.is_short]
        longs = [leg for leg in self.legs if leg.is_long]

        if self.strategy_type is StrategyType.IRON_CONDOR:
            if len(shorts) != 2 or len(longs) != 2:
                _refuse(
                    f"iron condor must be two short and two long legs, got "
                    f"{len(shorts)} short and {len(longs)} long"
                )
            for right in (OptionRight.PUT, OptionRight.CALL):
                if _sole(self.legs, right, OrderAction.SELL) is None:
                    _refuse(f"iron condor must have exactly one short {right.name.lower()}")
                if _sole(self.legs, right, OrderAction.BUY) is None:
                    _refuse(f"iron condor must have exactly one long {right.name.lower()}")
            return

        if len(shorts) != 1 or len(longs) != 1:
            _refuse(
                f"{self.strategy_type.value} must be exactly one short leg and one "
                f"long protective leg, got {len(shorts)} short and {len(longs)} long"
            )

        required = (
            OptionRight.PUT
            if self.strategy_type is StrategyType.PUT_CREDIT_SPREAD
            else OptionRight.CALL
        )
        wrong = [leg for leg in self.legs if leg.right is not required]
        if wrong:
            _refuse(
                f"{self.strategy_type.value} legs must all be {required.name}s, "
                f"found {[leg.right.name for leg in wrong]}"
            )

    # -- strike ordering --------------------------------------------------

    def _check_strike_ordering(self) -> None:
        """Protection must sit outside the short strike, on both sides.

        A reversed wing is not a mispriced structure -- it is an undefined-risk
        one wearing a defined-risk name, which is precisely the failure the
        whole defined-risk-only rule exists to prevent.
        """
        short_put = _sole(self.legs, OptionRight.PUT, OrderAction.SELL)
        long_put = _sole(self.legs, OptionRight.PUT, OrderAction.BUY)
        if short_put is not None and long_put is not None:
            if long_put.strike >= short_put.strike:
                _refuse(
                    f"long put strike {long_put.strike} must be below the short put "
                    f"strike {short_put.strike}",
                    hint="the protective put sits further out of the money than the short",
                )

        short_call = _sole(self.legs, OptionRight.CALL, OrderAction.SELL)
        long_call = _sole(self.legs, OptionRight.CALL, OrderAction.BUY)
        if short_call is not None and long_call is not None:
            if long_call.strike <= short_call.strike:
                _refuse(
                    f"long call strike {long_call.strike} must be above the short call "
                    f"strike {short_call.strike}",
                    hint="the protective call sits further out of the money than the short",
                )

        if self.strategy_type is StrategyType.IRON_CONDOR:
            if short_put is not None and short_call is not None:
                if short_put.strike >= short_call.strike:
                    _refuse(
                        f"iron condor short put {short_put.strike} must be below the "
                        f"short call {short_call.strike}",
                        hint="inverted short strikes make this a guaranteed-loss structure",
                    )

    # -- lifecycle --------------------------------------------------------

    def _check_lifecycle(self) -> None:
        if self.strategy_action is StrategyAction.OPEN:
            if self.closes_strategy_id is not None:
                _refuse("an opening strategy must not reference a strategy to close")
            if self.price_effect is not PriceEffect.CREDIT:
                _refuse(
                    f"opening a {self.strategy_type.value} must collect a credit, "
                    f"got {self.price_effect.value}"
                )
            return

        if self.closes_strategy_id is None:
            _refuse(
                f"a {self.strategy_action.value} order must name the open strategy it "
                "retires",
                hint="closing legs come from the persisted strategy, never from a "
                "fresh chain lookup",
            )
        if not isinstance(self.closes_strategy_id, UUID):
            _refuse("closes_strategy_id must be a UUID")
        if self.strategy_action is StrategyAction.CLOSE:
            if self.price_effect is not PriceEffect.DEBIT:
                _refuse(
                    f"closing a credit structure pays a debit, got "
                    f"{self.price_effect.value}"
                )

    # -- coverage and risk ------------------------------------------------

    def _check_every_short_is_covered(self) -> None:
        """No opening structure may contain a short option without protection.

        Checked per right, because a long put does not protect a short call.
        """
        for right in (OptionRight.PUT, OptionRight.CALL):
            short_ratio = sum(
                leg.ratio for leg in self.legs if leg.right is right and leg.is_short
            )
            long_ratio = sum(
                leg.ratio for leg in self.legs if leg.right is right and leg.is_long
            )
            if short_ratio and long_ratio < short_ratio:
                _refuse(
                    f"uncovered short {right.name.lower()}: {short_ratio} short against "
                    f"{long_ratio} long",
                    hint="every opening structure must be defined-risk; naked short "
                    "options are out of scope entirely",
                )

    def _check_maximum_loss(self) -> None:
        computed = compute_maximum_loss_per_contract(
            strategy_type=self.strategy_type,
            legs=self.legs,
            credit=self.limit_price,
            multiplier=self.multiplier,
        )
        if computed != self.maximum_loss_per_contract:
            _refuse(
                f"maximum_loss_per_contract {self.maximum_loss_per_contract} does not "
                f"match the value computed from the legs, {computed}",
                hint="the stored figure is what the governor sizes against; it must "
                "not be able to disagree with the structure",
            )

    # -- derived ----------------------------------------------------------

    @property
    def multiplier(self) -> int:
        """Uniform across legs -- ``_check_legs_are_coherent`` guarantees it."""
        return self.legs[0].multiplier

    @property
    def total_maximum_loss(self) -> Decimal:
        """Worst case across the whole position, which is what the governor caps."""
        return self.maximum_loss_per_contract * self.quantity

    @property
    def total_credit(self) -> Decimal:
        """Premium collected across the whole position, before fees."""
        return self.limit_price * self.multiplier * self.quantity

    def closing_intent(
        self,
        *,
        strategy_id: UUID,
        limit_price: Decimal,
        created_at: datetime,
        configuration_version: str,
        quantity: int | None = None,
    ) -> OptionStrategyIntent:
        """Build the order that retires this position, from *these* legs.

        The legs are inverted copies of the persisted ones, carrying the same
        ``con_id`` values. Nothing is re-derived from current chain data, so a
        close cannot land on a contract the position never held.
        """
        if self.strategy_action is not StrategyAction.OPEN:
            _refuse("only an opening strategy can be closed")
        closing_quantity = self.quantity if quantity is None else quantity
        _positive_int(closing_quantity, "quantity")
        if closing_quantity > self.quantity:
            _refuse(
                f"cannot close {closing_quantity} contracts of a {self.quantity}-contract "
                "position",
                hint="a defensive action never increases contract count",
            )
        return OptionStrategyIntent(
            strategy_id=strategy_id,
            strategy_type=self.strategy_type,
            strategy_action=StrategyAction.CLOSE,
            underlying=self.underlying,
            quantity=closing_quantity,
            legs=tuple(leg.inverted() for leg in self.legs),
            expiration=self.expiration,
            limit_price=limit_price,
            price_effect=PriceEffect.DEBIT,
            maximum_loss_per_contract=self.maximum_loss_per_contract,
            configuration_version=configuration_version,
            created_at=created_at,
            closes_strategy_id=self.strategy_id,
        )

    def describe(self) -> str:
        legs = " | ".join(leg.describe() for leg in self.legs)
        return (
            f"{self.strategy_action.value} {self.quantity}x "
            f"{self.strategy_type.value} {self.underlying} "
            f"@ {self.limit_price} {self.price_effect.value} "
            f"[max loss {self.total_maximum_loss}] :: {legs}"
        )
