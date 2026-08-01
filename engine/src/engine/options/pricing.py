"""Where a combo order's limit price comes from, and what grid it must sit on.

The engine's first live order was priced at the midpoint of the book -- short mid
minus long mid, 0.20 on a 1-wide SPY vertical -- and sat ``Submitted`` and
unfilled for 101 minutes of liquid regular-session trading. Nothing was broken.
The mid is simply not where a two-legged spread trades: it is the average of two
prices, and averages are not quotes. A spread trades somewhere between the mid
and the *natural*, and finding that point is a search, not a calculation.

This module is the arithmetic half of that search. It answers three questions and
deliberately performs no I/O, so every one of them is testable against a
hand-written book:

**What is the natural?**  For a credit structure, the price you receive if you
cross the market on every leg at once: sell each short leg at its **bid** and buy
each long leg at its **ask**. See :func:`natural_credit` for why no other pairing
is defensible.

**What grid may a limit price sit on?**  US listed options do not quote on a
continuous line. An off-grid limit is not rounded by IBKR -- it is rejected -- so
:func:`quantize_credit` is not a cosmetic step. The grid is per-class and is
declared as data in :data:`TICK_REGIME_BY_CLASS`, defaulting to the *coarsest*
regime for anything unlisted, because a coarser grid is always accepted and a
finer one is not.

**What sequence of prices does the walk try?**  :func:`build_ladder` produces the
monotone credit sequence -- midpoint, one third toward the natural, two thirds,
then the natural or the economic floor -- already quantized, already clamped to
the authorization's price envelope, and already proven non-increasing.

The one thing this module will not do is decide *whether* a price is safe. A
lower credit means a higher maximum loss, and re-checking that at every rung is
:mod:`engine.options.walk`'s job, because it needs the broker and the portfolio
and this module needs neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from ..errors import InvalidStrategyError
from .domain import OptionStrategyIntent, PriceEffect
from .ports import StrategyQuoteSnapshot
from .proof import PriceEnvelope, observed_credit

__all__ = [
    "TickRegime",
    "PENNY_THROUGHOUT",
    "PENNY_INTERVAL",
    "STANDARD_INCREMENTS",
    "TICK_REGIME_BY_CLASS",
    "DEFAULT_TICK_REGIME",
    "TICK_BOUNDARY",
    "tick_regime_for",
    "quantize_credit",
    "natural_credit",
    "midpoint_credit",
    "PriceLadder",
    "LADDER_FRACTIONS",
    "build_ladder",
]

ZERO = Decimal("0")
ONE = Decimal("1")
THREE = Decimal("3")


def _refuse(message: str, *, hint: str | None = None) -> None:
    raise InvalidStrategyError(message, hint=hint)


# ---------------------------------------------------------------------------
# The tick grid
# ---------------------------------------------------------------------------
#
# **Where these numbers come from, and where they deliberately do not.**
#
# The source of truth is the exchange rulebooks, not the broker. Each regime
# below was read out of the minimum-increment rule itself, and the same text
# appears in materially identical form across every venue checked:
#
#   Cboe C1 Rule 5.4  ·  Cboe C2 Rule 5.4  ·  Cboe BZX Rule 21.5
#   Nasdaq NOM Options 3 s3  ·  Nasdaq PHLX Options 3 s3
#   MIAX Rule 510  ·  NYSE American 960NY / NYSE Arca 6.72-O
#
# The SPY carve-out is explicit rather than inferred. Cboe BZX Rule 21.5(a)(3):
# "...one (1) cent if the options series is trading at less than $3.00, five (5)
# cents if ... at $3.00 or higher, *unless for QQQQ, SPY, or IWM where the
# minimum quoting increment will be one cent for all series regardless of
# price*." MIAX Rule 510(a)(3)(i) says the same in fewer words: "one cent
# ($0.01) for all options contracts in QQQ, SPY and IWM."
#
# **IBKR's own increment page is stale and must not be used here.** It documents
# penny-program options as "under USD 3.00 - Penny increments (0.01); over USD
# 3.00 - Nickel increments (0.05)" with no SPY/QQQ/IWM carve-out at all, and
# still calls it the "Penny Pilot Program" -- a name retired in 2020 when it
# became the Penny Interval Program. Taking the grid from there would round every
# SPY spread above $3.00 to a nickel for no reason. The rulebook wins.
#
# One distinction worth knowing and deliberately not exploited: BZX 21.5(b) and
# NOM Options 3 s3(b) both set the minimum *trading* increment at one cent for
# all series, while the *quoting* increment is the coarser schedule below. A
# resting limit order is a quote, so the quoting increment is the one that binds
# here; the finer trading increment is reachable only through price improvement
# and complex-order legging, which this engine does not attempt.

#: The price at which every US listed-option increment schedule changes tier.
#: It is $3.00 in every regime below, which is why it is one constant rather
#: than a field each regime sets for itself.
TICK_BOUNDARY = Decimal("3.00")


@dataclass(frozen=True)
class TickRegime:
    """A two-tier minimum-increment schedule for one class of options.

    Two tiers rather than a single number because every published US listed
    option schedule is two-tier: one increment below $3.00 and a coarser one at
    or above it. Modelling it as a single tick would be wrong on one side of the
    boundary whichever value was chosen, and the side it is wrong on is the side
    that gets the order rejected.

    ``__post_init__`` enforces the property the quantizer relies on: the boundary
    must itself be a whole multiple of **both** increments. When that holds, the
    two grids meet exactly at $3.00, and rounding within one tier can never land
    between the rungs of the other -- which is what makes a single quantize step
    correct instead of needing to iterate across the boundary.
    """

    name: str
    below: Decimal
    at_or_above: Decimal
    boundary: Decimal = TICK_BOUNDARY

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            _refuse("a tick regime must be named")
        for label in ("below", "at_or_above", "boundary"):
            value = getattr(self, label)
            if not isinstance(value, Decimal):
                _refuse(f"{label} must be a Decimal, got {type(value).__name__}")
            if not value.is_finite() or value <= ZERO:
                _refuse(f"{label} must be a positive finite Decimal, got {value}")
        if self.at_or_above < self.below:
            _refuse(
                f"{self.name}: the increment at or above {self.boundary} "
                f"({self.at_or_above}) is finer than the one below it ({self.below})",
                hint="every published US option schedule coarsens with price; an "
                "inverted pair is a transcription error",
            )
        for label in ("below", "at_or_above"):
            increment = getattr(self, label)
            if (self.boundary / increment) % ONE != ZERO:
                _refuse(
                    f"{self.name}: the boundary {self.boundary} is not a whole "
                    f"multiple of the {label} increment {increment}",
                    hint="the two tiers must meet exactly at the boundary, or a "
                    "price rounded in one tier can land off the grid of the other",
                )

    def increment_for(self, price: Decimal) -> Decimal:
        """The minimum increment applying **at** this price.

        The comparison is ``>=``: a price of exactly $3.00 is in the upper tier.
        That is the boundary the tests pin, because it is the one an
        off-by-one-comparison gets wrong, and it is the one where the two
        answers differ by a factor of five.
        """
        return self.at_or_above if price >= self.boundary else self.below

    def describe(self) -> str:
        return (
            f"{self.name}: {self.below} below {self.boundary}, "
            f"{self.at_or_above} at or above"
        )


#: The finest schedule in use: one cent at every price, named for QQQ, SPY and
#: IWM specifically in BZX 21.5(a)(3) and MIAX 510(a)(3)(i). It matters here
#: because a walk on a nickel grid across a 1-wide spread often has fewer than
#: four distinct rungs between the mid and the natural; on a penny grid the
#: four-step walk is always expressible.
PENNY_THROUGHOUT = TickRegime(
    name="penny-throughout",
    below=Decimal("0.01"),
    at_or_above=Decimal("0.01"),
)

#: A cent below $3.00, a nickel at or above it: the Penny Interval Program's
#: ordinary schedule, for a class admitted to it but not named in the
#: quoted-in-pennies-throughout carve-out.
PENNY_INTERVAL = TickRegime(
    name="penny-interval",
    below=Decimal("0.01"),
    at_or_above=Decimal("0.05"),
)

#: A nickel below $3.00, a dime at or above it -- the schedule for classes in no
#: penny program at all, unchanged across all seven rulebooks checked. This is
#: the **default** for an unlisted symbol, and the
#: direction of that default is the safety property: a price on the nickel/dime
#: grid is also a valid price on every finer grid, so quoting a coarse price into
#: a penny-quoted class is merely a slightly worse price. The reverse -- assuming
#: pennies in a nickel class -- is a rejected order.
STANDARD_INCREMENTS = TickRegime(
    name="standard",
    below=Decimal("0.05"),
    at_or_above=Decimal("0.10"),
)

#: Which schedule each underlying quotes on. Deliberately a small explicit table
#: rather than a rule inferred from price or volume: exchange participation in a
#: penny program is an administrative fact about a *class*, not something
#: derivable from its market data, and inferring it would be a guess wearing a
#: calculation's clothes.
#:
#: Entries are added only for symbols this engine actually trades. Everything
#: else falls to :data:`DEFAULT_TICK_REGIME` and is quoted coarsely, which costs
#: a fraction of the spread and never costs a rejection.
TICK_REGIME_BY_CLASS: dict[str, TickRegime] = {
    "SPY": PENNY_THROUGHOUT,
    "QQQ": PENNY_THROUGHOUT,
    "IWM": PENNY_THROUGHOUT,
}

#: What an unlisted class is assumed to be. The coarsest regime, on purpose.
DEFAULT_TICK_REGIME = STANDARD_INCREMENTS


def tick_regime_for(symbol: str) -> TickRegime:
    """The increment schedule for an underlying, coarsest-by-default.

    Case and surrounding whitespace are normalized here rather than at every
    call site, because the symbol arrives from an intent, a config file and a
    command line, and exactly one of those three reliably arrives uppercase.
    """
    if not isinstance(symbol, str):
        return DEFAULT_TICK_REGIME
    return TICK_REGIME_BY_CLASS.get(symbol.strip().upper(), DEFAULT_TICK_REGIME)


def quantize_credit(
    price: Decimal,
    *,
    regime: TickRegime,
    rounding: str = ROUND_FLOOR,
) -> Decimal:
    """Snap a credit onto ``regime``'s grid. **Not a cosmetic step.**

    IBKR does not round an off-grid limit price into shape; it rejects the order.
    A midpoint is an average of two quotes and is therefore off-grid roughly half
    the time by construction -- ``(0.21 + 0.20) / 2`` is ``0.205`` -- so an
    unquantized walk fails on its first rung, in the market, for a reason that
    looks nothing like a pricing bug.

    ``ROUND_FLOOR`` by default, and the direction is a decision rather than a
    convention. Flooring a credit moves it *toward* the natural: it is the
    direction the walk is already travelling, it can never push a price above the
    authorization envelope's ceiling, and it can never turn a rung into an
    increase over the rung before it. Rounding a credit up would do all three.
    The one place the caller must ask for ``ROUND_CEILING`` is a **floor** -- the
    smallest on-grid price that is still at or above an economic minimum -- and
    :func:`build_ladder` does exactly that.

    Because :class:`TickRegime` guarantees the boundary is a whole multiple of
    both increments, one rounding step is enough: a floor from the upper tier
    cannot fall below the boundary, and a ceiling from the lower tier cannot rise
    above it. No boundary-crossing second pass is needed, and a test pins the
    $3.00 case in both directions to keep that true.
    """
    if not isinstance(price, Decimal):
        _refuse(f"price must be a Decimal, got {type(price).__name__}")
    if not price.is_finite():
        _refuse(f"price must be finite, got {price}")
    if price < ZERO:
        _refuse(
            f"a credit must not be negative, got {price}",
            hint="the IBKR negative-limit convention belongs to the broker "
            "adapter; a credit is carried here as a positive magnitude",
        )

    increment = regime.increment_for(price)
    try:
        rungs = (price / increment).to_integral_value(rounding=rounding)
        return (rungs * increment).quantize(increment)
    except (InvalidOperation, ArithmeticError) as exc:  # pragma: no cover
        _refuse(f"could not quantize {price} onto {regime.name}: {exc}")
        raise  # pragma: no cover - _refuse always raises


# ---------------------------------------------------------------------------
# Reading a book
# ---------------------------------------------------------------------------


def _quotes_by_con_id(snapshot: StrategyQuoteSnapshot | None) -> dict[int, Any] | None:
    if snapshot is None:
        return None
    return {quote.con_id: quote for quote in snapshot.legs}


def natural_credit(
    intent: OptionStrategyIntent, snapshot: StrategyQuoteSnapshot | None
) -> Decimal | None:
    """What the book pays **right now** for this structure, crossing every leg.

    **The definition, stated explicitly because the whole walk is anchored to
    it:** each short leg is sold at its *bid* and each long leg is bought at its
    *ask*. Net credit is the sum of the short bids minus the sum of the long
    asks, per share.

    **Why no other pairing is defensible.** A limit price is a claim that both
    legs can be done simultaneously, so each leg has to be valued at the price
    someone else is standing ready to trade at -- the side of the book you would
    hit, not the side you would join. Selling the short at its *ask* means
    waiting for a buyer; buying the long at its *bid* means waiting for a seller.
    Combine those and you get a number that is not a price at all but a
    coincidence: it is what you would collect if two independent counterparties
    both happened to come to you. That is the price the mid flatters, and it is
    exactly the price that sat unfilled for 101 minutes.

    Being the *worst* executable price is the point. It is the floor of the walk,
    not its target: the walk starts at the mid precisely because the natural
    gives up the entire spread, and it only reaches the natural if nothing better
    fills first.

    Returns ``None`` when any leg is unpriced on the side it needs -- a
    one-sided market. The caller must treat that as a refusal. A structure with
    no bid on its short leg has no natural, and substituting the last trade or
    the close would produce a limit price backed by nothing.
    """
    quotes = _quotes_by_con_id(snapshot)
    if quotes is None:
        return None
    total = ZERO
    for leg in intent.legs:
        quote = quotes.get(leg.con_id)
        if quote is None:
            return None
        # Short: we sell, so we hit the bid. Long: we buy, so we lift the ask.
        price = quote.bid if leg.is_short else quote.ask
        if price is None or price < ZERO:
            return None
        total += price * leg.ratio if leg.is_short else -price * leg.ratio
    return total


def midpoint_credit(
    intent: OptionStrategyIntent, snapshot: StrategyQuoteSnapshot | None
) -> Decimal | None:
    """Short mids minus long mids -- the price the first live order was sent at.

    Delegates to :func:`engine.options.proof.observed_credit` rather than
    recomputing it. The execution proof's price envelope is measured against that
    function, and a second implementation here would let the walk and the
    envelope disagree about what "the current credit" means while both looked
    right in isolation.
    """
    return observed_credit(intent, snapshot)


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

#: How far toward the natural each attempt reaches. Four rungs: the midpoint,
#: one third of the way, two thirds, and the whole way. Expressed as fractions
#: rather than as four formulas so that "the walk has four evenly spaced steps"
#: is a readable property of the module rather than something reconstructed from
#: arithmetic, and so a test can assert the shape rather than four numbers.
LADDER_FRACTIONS: tuple[Decimal, ...] = (
    ZERO,
    ONE / THREE,
    Decimal(2) / THREE,
    ONE,
)


@dataclass(frozen=True)
class PriceLadder:
    """The exact sequence of credits one walk will offer, and why.

    ``requested`` is the four raw rungs before duplicates are removed;
    ``rungs`` is what the walk actually sends. They differ whenever the book is
    tight enough that two thirds of the distance rounds to the same tick as one
    third -- on a 1-wide penny-quoted spread with a two-cent-wide market, that is
    the normal case, not an edge case.

    Duplicates are removed rather than re-sent. Replacing a working order with
    another at an identical limit price accomplishes nothing except surrendering
    its place in the queue, which makes the order strictly *less* likely to fill
    than if it had been left alone. A walk that does that is not walking.
    """

    start: Decimal
    target: Decimal
    floor: Decimal
    ceiling: Decimal
    regime: TickRegime
    requested: tuple[Decimal, ...]
    rungs: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        previous: Decimal | None = None
        for rung in self.rungs:
            if previous is not None and rung >= previous:
                _refuse(
                    f"ladder rungs must strictly decrease, got {list(self.rungs)}",
                    hint="a credit walk gives ground monotonically; a rung that "
                    "asks for more than the one before it is not a walk",
                )
            if rung < self.floor or rung > self.ceiling:
                _refuse(
                    f"ladder rung {rung} is outside the allowed band "
                    f"[{self.floor}, {self.ceiling}]"
                )
            previous = rung

    @property
    def attempts(self) -> int:
        return len(self.rungs)

    def describe(self) -> str:
        return (
            f"ladder on {self.regime.name}: "
            + " -> ".join(str(rung) for rung in self.rungs)
            + f"  (mid {self.start}, natural target {self.target}, "
            f"floor {self.floor}, ceiling {self.ceiling})"
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "regime": self.regime.name,
            "start": str(self.start),
            "target": str(self.target),
            "floor": str(self.floor),
            "ceiling": str(self.ceiling),
            "requested": [str(rung) for rung in self.requested],
            "rungs": [str(rung) for rung in self.rungs],
        }


def build_ladder(
    intent: OptionStrategyIntent,
    snapshot: StrategyQuoteSnapshot | None,
    *,
    envelope: PriceEnvelope,
    minimum_credit: Decimal,
    regime: TickRegime | None = None,
    fractions: tuple[Decimal, ...] = LADDER_FRACTIONS,
) -> PriceLadder | None:
    """The monotone credit sequence for one structure against one book.

    ``None`` -- never a ladder with no rungs -- when the book cannot support a
    walk at all: an unpriceable leg, a natural above the midpoint (a crossed
    book), or an envelope whose floor has risen above its ceiling. Each of those
    is a refusal the caller must surface, and returning an empty ladder would let
    a ``for rung in ladder.rungs`` loop swallow all three silently.

    **The envelope is the caller's, and it is anchored.** It must be derived from
    the intent that was *authorized*, not from the intent of the current rung.
    Re-deriving it each attempt would let the envelope ratchet down in step with
    the walk, so that every price was trivially inside a band that had followed
    it there -- a bound that moves with the thing it bounds is not a bound. See
    :mod:`engine.options.walk`, which anchors it once and passes the same object
    to every attempt.

    ``minimum_credit`` is the economic floor: the credit below which this trade
    is no longer the trade that was approved. The effective floor is the
    **larger** of that and the envelope's own minimum, raised to the next tick,
    so the walk stops at whichever constraint binds first.
    """
    if intent.price_effect is not PriceEffect.CREDIT:
        _refuse(
            f"the price walk prices credit structures, got "
            f"{intent.price_effect.value}",
            hint="a debit close walks the other way and is not this function",
        )
    regime = regime or tick_regime_for(intent.underlying)

    midpoint = midpoint_credit(intent, snapshot)
    natural = natural_credit(intent, snapshot)
    if midpoint is None or natural is None:
        return None

    if natural > midpoint:
        # The book is crossed, or its two sides came from different moments.
        # Either way the arithmetic that produced them is not describing one
        # market, and interpolating between them would produce an *increasing*
        # sequence wearing a walk's name.
        return None

    # The band. Every bound rounds toward the *inside* of the band it came from,
    # so no bound is widened by its own quantization:
    #
    #   ceiling   floors  -- the most we may ask, rounded down onto the grid
    #   floor     ceils   -- the least we may accept, rounded up
    #   start     floors  -- the midpoint, rounded toward the natural
    #   target    ceils   -- the furthest concession, rounded back toward us
    #
    # ``target`` is the one that is easy to get backwards, and getting it wrong
    # is how a nickel-grid walk offers 0.15 against a natural of 0.17 -- conceding
    # two cents *more* than the market was ever asking for.
    ceiling = quantize_credit(envelope.maximum, regime=regime, rounding=ROUND_FLOOR)
    raw_floor = max(envelope.minimum, minimum_credit)
    floor = quantize_credit(raw_floor, regime=regime, rounding=ROUND_CEILING)
    if floor > ceiling:
        return None

    start = quantize_credit(min(midpoint, ceiling), regime=regime, rounding=ROUND_FLOOR)
    target = quantize_credit(
        max(natural, floor), regime=regime, rounding=ROUND_CEILING
    )
    if start < floor or start > ceiling or target > ceiling:
        return None
    if target > start:
        # No grid point lies between the midpoint and the concession limit. The
        # walk has exactly one price to offer, which is the honest answer for a
        # three-cent span on a nickel grid.
        target = start

    span = start - target
    requested = tuple(
        quantize_credit(start - span * fraction, regime=regime, rounding=ROUND_FLOOR)
        for fraction in fractions
    )

    rungs: list[Decimal] = []
    for rung in requested:
        clamped = min(max(rung, target), ceiling)
        if rungs and clamped >= rungs[-1]:
            # Either a duplicate after quantization or -- once clamped -- a rung
            # the floor has caught up with. Both mean this attempt would offer a
            # price the walk has already offered.
            continue
        rungs.append(clamped)

    if not rungs:
        return None

    return PriceLadder(
        start=start,
        target=target,
        floor=floor,
        ceiling=ceiling,
        regime=regime,
        requested=requested,
        rungs=tuple(rungs),
    )
