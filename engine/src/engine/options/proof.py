"""A bounded operational test of the IBKR execution lifecycle.

This module exists so that the question *"does an order actually make it through
IBKR's combo lifecycle, and can we see every step of it?"* can be answered
without ever loosening the strategy's own thresholds. Those two questions get
confused constantly, and the confusion is expensive in both directions: an
operator who wants to test plumbing lowers the IV Rank floor "just for one run"
and forgets to put it back, or an operator who refuses to lower it never learns
that the fill path was broken until the first real signal arrives.

**What the proof is allowed to skip.** Opportunity filters, and only those. IV
Rank is an opinion about whether *now* is a good time to sell premium. It is
not a safety property, and a proof that has to wait for a high-IV regime is a
proof that gets run once a quarter, badly. So the proof runs with the IV Rank
filter off and says so out loud, in the report and in the audit tag.

**What the proof may never skip.** Everything that answers "is this order
survivable if it fills". Live uniform market-data provenance, quote and greek
freshness, defined-risk construction, the maximum-loss check, the broker
what-if margin, the stress loss, portfolio reconciliation, the authorization
token, the kill switch, the paper-port restriction and durable persistence all
run exactly as they do on a strategy pass, because the proof reaches them
through :func:`engine.options.runner.run_once` rather than through a second
copy of the pipeline. A verification path with its own copy of the gates would
prove something about the copy.

**Why the bounds are a schema and not configuration.** The whole value of the
proof is that its blast radius is knowable without reading the environment. So
:class:`ExecutionProofProfile` refuses at construction any bound looser than the
module ceilings below, and :meth:`ExecutionProofProfile.derive_policy` folds
those ceilings into the environment's policy with ``min()`` -- or ``max()``
where a *larger* number is the tighter one. The direction is chosen per field
rather than applied uniformly, because "tighter" is not the same as "smaller":
a bigger ``stress_move_fraction`` is a harsher test, and a bigger
``quote_maximum_age`` is a laxer one. The consequence is the property the proof
is worth having: an ``IBKR_OPTIONS_*`` variable can make a proof run stricter
and can never make it looser.

Nothing here can send an order. The proof reaches the market through the same
single chokepoint every other path does.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID, uuid4

from ..config import PAPER_PORTS
from ..errors import ConfigError
from .domain import OptionStrategyIntent, PriceEffect
from .marketdata import Liveness
from .orderstate import BrokerOrderSnapshot
from .policy import RiskPolicy
from .ports import LiveMarketDataPort, StrategyQuoteSnapshot
from .sink import OrderLifecycleSink

__all__ = [
    "PROOF_VERSION",
    "PROOF_AUDIT_TAG",
    "PROOF_CONFIGURATION_VERSION",
    "PROOF_SYMBOL",
    "PROOF_QUANTITY",
    "PROOF_TARGET_WIDTH",
    "PROOF_MAXIMUM_WIDTH",
    "PROOF_MAXIMUM_DEFINED_LOSS",
    "PROOF_MAXIMUM_BROKER_MARGIN",
    "PROOF_MAXIMUM_STRESS_LOSS",
    "PROOF_MAXIMUM_OPENING_ORDERS",
    "PROOF_LEG_COUNT",
    "ExecutionProofProfile",
    "PriceEnvelope",
    "OpeningOrderBudget",
    "ProofEntryPreflight",
    "RecordingLifecycleSink",
    "vertical_width",
    "observed_credit",
    "envelope_for",
]

ZERO = Decimal("0")

PROOF_VERSION = "options-execution-proof/1"

#: Stamped onto every journal record and every persisted intent the proof
#: produces, so a proof order can never be mistaken for a strategy order in the
#: durable log -- which is the only place anyone will look a month from now.
PROOF_AUDIT_TAG = "OPTIONS_EXECUTION_PROOF"

#: Written into ``OptionStrategyIntent.configuration_version``. The ordinary
#: runner writes ``options-runner/1`` there; a record carrying this value was
#: produced by the proof and by nothing else.
PROOF_CONFIGURATION_VERSION = PROOF_VERSION

# -- the hard ceilings -------------------------------------------------------
#
# Every one of these is a module constant rather than a default that reads the
# environment. A profile may be constructed *tighter* than these and is refused
# outright if it is looser, so the worst case of a proof run is bounded by
# reading this file and nothing else.

PROOF_SYMBOL = "SPY"
PROOF_QUANTITY = 1
PROOF_LEG_COUNT = 2
PROOF_TARGET_WIDTH = Decimal("1")
PROOF_MAXIMUM_WIDTH = Decimal("1")
PROOF_MAXIMUM_DEFINED_LOSS = Decimal("100")
PROOF_MAXIMUM_BROKER_MARGIN = Decimal("150")
PROOF_MAXIMUM_STRESS_LOSS = Decimal("100")
PROOF_MAXIMUM_OPENING_ORDERS = 1

# Fractional ceilings. These are not in the brief's list, but leaving them to
# the environment would defeat it: ``MAX_DEFINED_LOSS_FRACTION=0.9`` cannot
# widen the $100 cap, yet the fraction check is a second gate and an unbounded
# second gate is a gate nobody is watching.
PROOF_MAXIMUM_DEFINED_LOSS_FRACTION = Decimal("0.02")
PROOF_MAXIMUM_BROKER_MARGIN_FRACTION = Decimal("0.02")
PROOF_MAXIMUM_STRESS_LOSS_FRACTION = Decimal("0.02")
PROOF_MAXIMUM_TOTAL_BPR_FRACTION = Decimal("0.35")
PROOF_MAXIMUM_INCREMENTAL_BPR_FRACTION = Decimal("0.05")
PROOF_MAXIMUM_UNDERLYING_BPR_FRACTION = Decimal("0.10")
PROOF_MAXIMUM_SECTOR_BPR_FRACTION = Decimal("0.15")
PROOF_MAXIMUM_CORRELATION_GROUP_BPR_FRACTION = Decimal("0.20")

# Ages clamp downward -- a longer maximum age accepts staler data.
PROOF_QUOTE_MAXIMUM_AGE = dt.timedelta(seconds=10)
PROOF_PORTFOLIO_SNAPSHOT_MAXIMUM_AGE = dt.timedelta(seconds=60)

# The stress move clamps *upward*: a bigger adverse move is a harsher test, so
# the environment may raise it and may not lower it below this floor.
PROOF_MINIMUM_STRESS_MOVE_FRACTION = Decimal("0.15")

# -- the price envelope ------------------------------------------------------

#: How far the book may drift between pricing the candidate and sending it,
#: as an absolute credit per share and as a fraction of the intended credit.
#: The wider of the two applies, so a 0.05 credit is not held to a tolerance of
#: half a cent.
PROOF_ENVELOPE_ABSOLUTE_TOLERANCE = Decimal("0.05")
PROOF_ENVELOPE_FRACTIONAL_TOLERANCE = Decimal("0.10")

#: The smallest credit a proof order may still be sent for. A credit spread
#: that has decayed to nothing is not a cheaper test, it is a different trade.
PROOF_MINIMUM_CREDIT = Decimal("0.01")


def _refuse(message: str, *, hint: str | None = None) -> None:
    raise ConfigError(message, hint=hint)


@dataclass(frozen=True)
class ExecutionProofProfile:
    """The complete blast radius of one proof session, fixed at construction.

    Frozen, validated, and hashable, so the fingerprint printed at the top of a
    proof run identifies exactly the bounds that run was subject to. A future
    reader of the journal can recompute it and see whether the bounds moved.
    """

    port: int
    account: str = ""
    symbol: str = PROOF_SYMBOL
    quantity: int = PROOF_QUANTITY
    leg_count: int = PROOF_LEG_COUNT
    target_width: Decimal = PROOF_TARGET_WIDTH
    maximum_width: Decimal = PROOF_MAXIMUM_WIDTH
    maximum_defined_loss: Decimal = PROOF_MAXIMUM_DEFINED_LOSS
    maximum_broker_margin: Decimal = PROOF_MAXIMUM_BROKER_MARGIN
    maximum_stress_loss: Decimal = PROOF_MAXIMUM_STRESS_LOSS
    maximum_opening_orders: int = PROOF_MAXIMUM_OPENING_ORDERS
    audit_tag: str = PROOF_AUDIT_TAG
    version: str = PROOF_VERSION

    # -- validation ------------------------------------------------------

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or self.symbol.strip().upper() != PROOF_SYMBOL:
            _refuse(
                f"the execution proof runs on {PROOF_SYMBOL} only, got {self.symbol!r}",
                hint="the proof is a plumbing test with a fixed underlying; to trade "
                "another symbol, use options-run with the real policy",
            )
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            _refuse(f"port must be an int, got {type(self.port).__name__}")
        if self.port not in PAPER_PORTS:
            _refuse(
                f"the execution proof runs on a paper port only, got {self.port}",
                hint="paper ports are "
                + ", ".join(f"{p} ({n})" for p, n in sorted(PAPER_PORTS.items())),
            )
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            _refuse(f"quantity must be an int, got {type(self.quantity).__name__}")
        if self.quantity != PROOF_QUANTITY:
            _refuse(
                f"the execution proof sends exactly {PROOF_QUANTITY} contract, got "
                f"{self.quantity}",
                hint="a proof that scales with size is no longer a proof of the "
                "lifecycle, it is a position",
            )
        if self.leg_count != PROOF_LEG_COUNT:
            _refuse(
                f"the execution proof sends one vertical -- exactly "
                f"{PROOF_LEG_COUNT} legs -- got {self.leg_count}"
            )
        if self.maximum_opening_orders != PROOF_MAXIMUM_OPENING_ORDERS:
            _refuse(
                f"the execution proof opens exactly "
                f"{PROOF_MAXIMUM_OPENING_ORDERS} order per session, got "
                f"{self.maximum_opening_orders}"
            )
        if self.audit_tag != PROOF_AUDIT_TAG:
            _refuse(
                f"the audit tag is fixed at {PROOF_AUDIT_TAG!r}",
                hint="it is how a proof order is told apart from a strategy order "
                "in the durable log, so it is not a caller's to choose",
            )
        if self.version != PROOF_VERSION:
            _refuse(f"the proof version is fixed at {PROOF_VERSION!r}")

        # Every money bound may be tightened and may never be widened. Checked
        # here rather than clamped silently: a caller who asked for a $5,000
        # proof has misunderstood something, and quietly giving them $100 would
        # leave that misunderstanding intact.
        for label, ceiling in (
            ("target_width", PROOF_TARGET_WIDTH),
            ("maximum_width", PROOF_MAXIMUM_WIDTH),
            ("maximum_defined_loss", PROOF_MAXIMUM_DEFINED_LOSS),
            ("maximum_broker_margin", PROOF_MAXIMUM_BROKER_MARGIN),
            ("maximum_stress_loss", PROOF_MAXIMUM_STRESS_LOSS),
        ):
            value = getattr(self, label)
            if not isinstance(value, Decimal):
                _refuse(f"{label} must be a Decimal, got {type(value).__name__}")
            if not value.is_finite():
                _refuse(f"{label} must be finite, got {value}")
            if value <= ZERO:
                _refuse(f"{label} must be greater than zero, got {value}")
            if value > ceiling:
                _refuse(
                    f"{label} {value} exceeds the execution-proof ceiling {ceiling}",
                    hint="the proof's bounds are a schema, not configuration; to "
                    "raise one, move the constant in engine/options/proof.py in a diff",
                )

        if not isinstance(self.account, str):
            _refuse(f"account must be a string, got {type(self.account).__name__}")

    # -- the derived policy ----------------------------------------------

    def derive_policy(self, env: dict[str, str] | None = None) -> RiskPolicy:
        """The environment's policy, with every proof ceiling folded in.

        The clamp direction is chosen per field, because "tighter" is not
        "smaller". Three fields tighten by growing -- the stress move -- and two
        by shrinking -- the maximum ages -- and applying one rule uniformly would
        silently invert half of them.

        The result is that ``IBKR_OPTIONS_MAX_DEFINED_LOSS_PER_POSITION=999999``
        produces a policy with a $100 cap, and
        ``IBKR_OPTIONS_MAX_DEFINED_LOSS_PER_POSITION=10`` produces one with a $10
        cap. The environment is still read; it just cannot spend.
        """
        base = RiskPolicy.from_env(env)
        return RiskPolicy.from_env(
            env,
            # -- caps: the environment may only lower them ----------------
            max_defined_loss_per_position=min(
                base.max_defined_loss_per_position, self.maximum_defined_loss
            ),
            max_broker_margin_per_position=min(
                base.max_broker_margin_per_position, self.maximum_broker_margin
            ),
            max_stress_loss_per_position=min(
                base.max_stress_loss_per_position, self.maximum_stress_loss
            ),
            max_defined_loss_fraction=min(
                base.max_defined_loss_fraction, PROOF_MAXIMUM_DEFINED_LOSS_FRACTION
            ),
            max_broker_margin_fraction=min(
                base.max_broker_margin_fraction, PROOF_MAXIMUM_BROKER_MARGIN_FRACTION
            ),
            max_stress_loss_fraction=min(
                base.max_stress_loss_fraction, PROOF_MAXIMUM_STRESS_LOSS_FRACTION
            ),
            max_total_bpr_fraction=min(
                base.max_total_bpr_fraction, PROOF_MAXIMUM_TOTAL_BPR_FRACTION
            ),
            max_incremental_bpr_fraction=min(
                base.max_incremental_bpr_fraction,
                PROOF_MAXIMUM_INCREMENTAL_BPR_FRACTION,
            ),
            max_underlying_bpr_fraction=min(
                base.max_underlying_bpr_fraction, PROOF_MAXIMUM_UNDERLYING_BPR_FRACTION
            ),
            max_sector_bpr_fraction=min(
                base.max_sector_bpr_fraction, PROOF_MAXIMUM_SECTOR_BPR_FRACTION
            ),
            max_correlation_group_bpr_fraction=min(
                base.max_correlation_group_bpr_fraction,
                PROOF_MAXIMUM_CORRELATION_GROUP_BPR_FRACTION,
            ),
            # -- structure: one narrow vertical, sized to the loss cap -----
            target_width=min(base.target_width, self.target_width),
            risk_budget_per_position=min(
                base.risk_budget_per_position, self.maximum_defined_loss
            ),
            # -- ages: a longer maximum age accepts staler data ------------
            quote_maximum_age=min(base.quote_maximum_age, PROOF_QUOTE_MAXIMUM_AGE),
            portfolio_snapshot_maximum_age=min(
                base.portfolio_snapshot_maximum_age,
                PROOF_PORTFOLIO_SNAPSHOT_MAXIMUM_AGE,
            ),
            # -- the one field where bigger is stricter --------------------
            stress_move_fraction=max(
                base.stress_move_fraction, PROOF_MINIMUM_STRESS_MOVE_FRACTION
            ),
        )

    # -- what the built structure has to look like -----------------------

    def check_intent(self, intent: OptionStrategyIntent) -> tuple[str, ...]:
        """Every proof bound that is a property of the candidate, not the policy.

        The policy bounds are enforced by the ordinary risk gates against the
        derived policy. These are the ones that have no policy field to live in
        -- leg count, width, symbol, quantity -- and would otherwise be enforced
        nowhere. Returns refusals rather than raising so the caller can report
        all of them at once instead of the first.
        """
        problems: list[str] = []

        if intent.underlying.strip().upper() != self.symbol:
            problems.append(
                f"the proof runs on {self.symbol}, but the candidate is "
                f"{intent.underlying}"
            )
        if intent.quantity != self.quantity:
            problems.append(
                f"the proof sends exactly {self.quantity} contract, but the "
                f"candidate is sized {intent.quantity}"
            )
        if len(intent.legs) != self.leg_count:
            problems.append(
                f"the proof sends one vertical ({self.leg_count} legs), but the "
                f"candidate has {len(intent.legs)}"
            )
        if intent.price_effect is not PriceEffect.CREDIT:
            problems.append(
                f"the proof opens for a credit, but the candidate is a "
                f"{intent.price_effect.value}"
            )

        width = vertical_width(intent)
        if width is None:
            problems.append("the candidate's legs do not form a single vertical")
        elif width > self.maximum_width:
            problems.append(
                f"the candidate is {width} wide, above the proof's maximum width "
                f"{self.maximum_width}"
            )

        loss = intent.total_maximum_loss
        if loss > self.maximum_defined_loss:
            problems.append(
                f"the candidate's defined loss {loss} is above the proof's "
                f"maximum {self.maximum_defined_loss}"
            )

        # A bounded limit price is the whole reason the proof is safe to arm.
        # An unpriced or non-positive limit is how a combo order becomes an
        # effective market order on a book nobody has read.
        price = intent.limit_price
        if not isinstance(price, Decimal) or not price.is_finite():
            problems.append(f"the candidate has no usable limit price ({price!r})")
        elif price < PROOF_MINIMUM_CREDIT:
            problems.append(
                f"the candidate's credit {price} is below the proof's minimum "
                f"{PROOF_MINIMUM_CREDIT}"
            )

        return tuple(problems)

    # -- identity ---------------------------------------------------------

    def to_record(self) -> dict[str, str]:
        return {
            "version": self.version,
            "audit_tag": self.audit_tag,
            "symbol": self.symbol,
            "port": str(self.port),
            "quantity": str(self.quantity),
            "leg_count": str(self.leg_count),
            "target_width": str(self.target_width),
            "maximum_width": str(self.maximum_width),
            "maximum_defined_loss": str(self.maximum_defined_loss),
            "maximum_broker_margin": str(self.maximum_broker_margin),
            "maximum_stress_loss": str(self.maximum_stress_loss),
            "maximum_opening_orders": str(self.maximum_opening_orders),
        }

    def fingerprint(self) -> str:
        """A stable hash of the bounds this session ran under.

        The account is deliberately excluded: two proofs on two paper accounts
        under identical bounds should fingerprint identically, because what the
        fingerprint answers is "were the bounds the same", not "was it the same
        machine".
        """
        canonical = json.dumps(self.to_record(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> str:
        return "\n".join(
            [
                f"  profile        {self.version}  fingerprint {self.fingerprint()}",
                f"  audit tag      {self.audit_tag}",
                f"  scope          {self.symbol} only, paper port {self.port}, "
                f"account {self.account or '(unset)'}",
                f"  structure      one vertical, {self.leg_count} legs, "
                f"quantity {self.quantity}, width <= {self.maximum_width}",
                f"  money bounds   defined loss <= {self.maximum_defined_loss}, "
                f"broker margin <= {self.maximum_broker_margin}, "
                f"stress loss <= {self.maximum_stress_loss}",
                f"  session cap    {self.maximum_opening_orders} opening order, "
                "no scheduler, one pass",
                "  filters        IV Rank OFF -- this is a lifecycle proof, not a "
                "strategy signal",
                "  gates          provenance, freshness, defined risk, max loss, "
                "broker what-if, stress, reconciliation,",
                "                 authorization token, kill switch, paper port and "
                "durable persistence ALL still run",
            ]
        )


# ---------------------------------------------------------------------------
# The price envelope
# ---------------------------------------------------------------------------


def vertical_width(intent: OptionStrategyIntent) -> Decimal | None:
    """The distance between the two strikes, or ``None`` if it is not a vertical.

    ``None`` rather than zero for the degenerate cases, because a width of zero
    is itself a meaningful and very bad answer -- a short leg protected on its
    own strike -- and conflating it with "this structure has more than two
    distinct strikes" would let one hide inside the other.
    """
    strikes = sorted({leg.strike for leg in intent.legs})
    if len(strikes) != 2:
        return None
    return strikes[1] - strikes[0]


def observed_credit(
    intent: OptionStrategyIntent, snapshot: StrategyQuoteSnapshot | None
) -> Decimal | None:
    """What this structure is worth per share in the book right now.

    Built the same way the entry price was: short mids minus long mids. Returns
    ``None`` when any leg is unpriced, which the caller must treat as a refusal
    -- an unpriceable book is exactly when a limit order is most dangerous.
    """
    if snapshot is None:
        return None
    quotes = {quote.con_id: quote for quote in snapshot.legs}
    total = ZERO
    for leg in intent.legs:
        quote = quotes.get(leg.con_id)
        if quote is None:
            return None
        mid = quote.mid
        if mid is None or mid < ZERO:
            return None
        total += mid * leg.ratio if leg.is_short else -mid * leg.ratio
    return total


@dataclass(frozen=True)
class PriceEnvelope:
    """The band of credits a proof order may still be sent for.

    Printed before the order goes out, so the operator sees the bound *before*
    the thing it bounds -- not afterwards in a log, where it is a description
    rather than a control.
    """

    reference: Decimal
    minimum: Decimal
    maximum: Decimal
    width: Decimal | None = None

    def contains(self, price: Decimal | None) -> bool:
        if price is None:
            return False
        return self.minimum <= price <= self.maximum

    def describe(self) -> str:
        width = f", spread width {self.width}" if self.width is not None else ""
        return (
            f"credit {self.reference} allowed between {self.minimum} and "
            f"{self.maximum}{width}"
        )

    def to_record(self) -> dict[str, str | None]:
        return {
            "reference": str(self.reference),
            "minimum": str(self.minimum),
            "maximum": str(self.maximum),
            "width": str(self.width) if self.width is not None else None,
        }


def envelope_for(intent: OptionStrategyIntent) -> PriceEnvelope:
    """The allowed price band around the credit the candidate was priced at.

    Bounded on **both** sides. A book that has moved in our favour is still a
    book that has moved, and the risk figures -- maximum loss, stress loss,
    broker margin -- were all computed against the old credit. Accepting a
    better price would mean transmitting an order whose arithmetic was checked
    against a different trade.
    """
    reference = intent.limit_price
    tolerance = max(
        PROOF_ENVELOPE_ABSOLUTE_TOLERANCE,
        (reference * PROOF_ENVELOPE_FRACTIONAL_TOLERANCE),
    )
    width = vertical_width(intent)

    minimum = max(reference - tolerance, PROOF_MINIMUM_CREDIT)
    maximum = reference + tolerance
    if width is not None:
        # A credit at or above the width is not a credit spread; it is a claim
        # to be paid more than the structure can ever lose.
        ceiling = width - PROOF_MINIMUM_CREDIT
        if ceiling > ZERO:
            maximum = min(maximum, ceiling)

    return PriceEnvelope(
        reference=reference, minimum=minimum, maximum=maximum, width=width
    )


# ---------------------------------------------------------------------------
# Session budget
# ---------------------------------------------------------------------------


@dataclass
class OpeningOrderBudget:
    """Exactly one opening order per proof session.

    ``run_once`` already considers at most one entry per pass, so on the happy
    path this never binds. It exists because "at most one per pass" and "at most
    one per session" are different claims, and the second is the one the brief
    asks for -- a future caller that loops the runner would satisfy the first
    and violate the second without this object noticing anything unusual.
    """

    limit: int = PROOF_MAXIMUM_OPENING_ORDERS
    spent: int = 0

    def claim(self) -> bool:
        """Take one order from the budget. ``False`` when there is none left."""
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit


# ---------------------------------------------------------------------------
# Lifecycle capture
# ---------------------------------------------------------------------------


@dataclass
class RecordingLifecycleSink:
    """Keeps every observation, then forwards it to the durable recorder.

    Ordering is deliberate: the observation is appended to the in-memory log
    *before* it is forwarded, so a persistence failure still leaves the operator
    with the timeline that led up to it. The in-memory log is a report; the
    inner sink is the record of truth, and the proof never treats one as the
    other.

    Like every sink, this can only write history. It has no way to authorize,
    build, cancel or send an order, which is what makes it safe to hand to code
    running inside the polling loop where the authorization token is long out of
    scope.
    """

    inner: OrderLifecycleSink | None = None
    observations: list[tuple[UUID, BrokerOrderSnapshot, bool]] = field(
        default_factory=list
    )

    def observe(
        self,
        strategy_id: UUID,
        observation: BrokerOrderSnapshot,
        *,
        closing: bool = False,
    ) -> bool:
        self.observations.append((strategy_id, observation, closing))
        if self.inner is None:
            return True
        return self.inner.observe(strategy_id, observation, closing=closing)

    # -- reporting --------------------------------------------------------

    def timeline(self) -> tuple[str, ...]:
        """One line per observation, in the order the broker produced them.

        Deduplication is *not* applied. The durable store deduplicates because
        an event log should record what changed; this is the opposite artefact
        -- it should record what was heard, including the eleven identical
        ``Submitted`` callbacks, because "the broker said nothing new for
        eleven polls" is exactly the symptom an operator is looking for.
        """
        lines: list[str] = []
        for index, (strategy_id, snapshot, closing) in enumerate(
            self.observations, start=1
        ):
            lines.append(
                f"  {index:>3}  {snapshot.observed_at.isoformat()}  "
                f"{'CLOSE' if closing else 'OPEN ':<5}  "
                f"{snapshot.state.value:<26}  "
                f"raw={snapshot.raw_status or '-':<12}  "
                f"order={snapshot.order_id}  perm={snapshot.perm_id}  "
                f"filled={snapshot.filled}  remaining={snapshot.remaining}  "
                f"avg={snapshot.average_price}  commission={snapshot.commission}  "
                f"strategy={strategy_id}"
            )
            if snapshot.message:
                lines.append(f"       message: {snapshot.message}")
        return tuple(lines)

    def to_record(self) -> list[dict[str, Any]]:
        return [
            {
                "strategy_id": str(strategy_id),
                "closing": closing,
                **snapshot.to_record(),
            }
            for strategy_id, snapshot, closing in self.observations
        ]


# ---------------------------------------------------------------------------
# The pre-send preflight
# ---------------------------------------------------------------------------


@dataclass
class ProofEntryPreflight:
    """The last thing that runs before the authorization token is minted.

    Three jobs, in this order, and the order is the point:

    1. **Shape.** Everything the proof bounds that the policy has no field for
       -- symbol, quantity, leg count, width, credit floor.
    2. **Envelope.** Print the allowed price band, then re-read the book and
       refuse if it has left the band. Printing first is what makes this a
       control rather than a log line: the operator sees the bound, and then
       sees whether it held.
    3. **Budget.** Claimed last, and only on an armed run. A dry run sends
       nothing, so charging it for the session's single opening order would make
       the obvious workflow -- look at it unarmed, then arm it -- fail on the
       second step for a reason that has nothing to do with the market. A run
       that refuses at step 1 or 2 does not spend it either, for the same
       reason.

    Returns a refusal string or ``None``. A string becomes a runner blocker,
    which stops the entry before :func:`engine.options.transmit.authorize_open`
    is ever called -- so a proof that fails here has no authorization token in
    existence, not merely an unused one.
    """

    profile: ExecutionProofProfile
    budget: OpeningOrderBudget
    emit: Callable[[str], None] | None = None
    envelope: PriceEnvelope | None = None
    credit_at_send: Decimal | None = None
    refusal: str | None = None

    def _say(self, line: str) -> None:
        if self.emit is not None:
            self.emit(line)

    def __call__(
        self,
        *,
        intent: OptionStrategyIntent,
        snapshot: StrategyQuoteSnapshot | None,
        market_data: LiveMarketDataPort | None,
        policy: RiskPolicy,
        now: dt.datetime,
        armed: bool = True,
    ) -> str | None:
        problems = self.profile.check_intent(intent)
        if problems:
            self.refusal = "; ".join(problems)
            return self.refusal

        envelope = envelope_for(intent)
        self.envelope = envelope

        self._say("")
        self._say("PRICE ENVELOPE (evaluated before the order is built)")
        self._say(f"  intended       {envelope.describe()}")
        self._say(
            "  order type     bounded limit -- the combo is priced at the "
            "intended credit and can never cross the book without a price"
        )

        if market_data is None:
            self.refusal = (
                "no market-data port, so the book cannot be re-read before the "
                "order is sent"
            )
            self._say(f"  REFUSED        {self.refusal}")
            return self.refusal

        try:
            fresh = market_data.strategy_quotes(
                underlying_symbol=intent.underlying,
                con_ids=[leg.con_id for leg in intent.legs],
            )
        except Exception as exc:  # noqa: BLE001 - adapter boundary; fail closed
            self.refusal = (
                f"the book could not be re-read before sending: "
                f"{type(exc).__name__}: {exc}"
            )
            self._say(f"  REFUSED        {self.refusal}")
            return self.refusal

        livenesses = {quote.provenance.liveness for quote in fresh.legs}
        if livenesses != {Liveness.LIVE}:
            self.refusal = (
                "the pre-send re-read of the book is not uniformly live "
                f"({sorted(state.value for state in livenesses)})"
            )
            self._say(f"  REFUSED        {self.refusal}")
            return self.refusal

        credit = observed_credit(intent, fresh)
        self.credit_at_send = credit
        self._say(f"  observed now   {credit if credit is not None else 'unpriceable'}")

        if not envelope.contains(credit):
            self.refusal = (
                f"the book has moved outside the price envelope: {credit} is not "
                f"between {envelope.minimum} and {envelope.maximum}"
            )
            self._say(f"  REFUSED        {self.refusal}")
            return self.refusal

        if not armed:
            # Nothing will be sent, so nothing is charged. The band still held,
            # and saying so is the whole value of an unarmed proof.
            self._say(
                "  WITHIN BAND    not armed, so the session's opening order is "
                "not spent"
            )
            return None

        if not self.budget.claim():
            self.refusal = (
                f"this proof session has already opened its "
                f"{self.budget.limit} order"
            )
            self._say(f"  REFUSED        {self.refusal}")
            return self.refusal

        self._say("  WITHIN BAND    proceeding to authorization")
        return None


def new_proof_session_id() -> UUID:
    """A fresh id for one proof session.

    Separate from any strategy id. It ties the profile fingerprint, the journal
    records and the printed timeline of a single run together, which is the
    thing a later reader needs and which no strategy id can provide -- a proof
    that refuses before building a candidate has no strategy id at all.
    """
    return uuid4()
