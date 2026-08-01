"""The bounded execution proof: its ceilings, and the fact they cannot be moved.

The proof exists so that "does IBKR's combo lifecycle actually work" can be
answered without lowering the strategy's own thresholds. That is only worth
anything if its blast radius is knowable *without reading the environment*, so
the tests here are mostly about what the proof refuses:

* every documented bound, asserted against the constant rather than a literal,
  so moving a ceiling moves the test with it;
* the env-cannot-widen property, driven by setting every ``IBKR_OPTIONS_*``
  variable the policy reads to something enormous and asserting the derived
  policy does not move a single field;
* the price envelope, including the refusal when the book drifts out of it and
  the fact that a refusal does **not** consume the session's one opening order;
* construction refusals for a non-SPY symbol, a non-paper port and any quantity
  other than one.

The broker fakes are imported from ``test_options_runner`` rather than copied.
A second copy would drift, and the properties asserted here are about the real
pipeline -- if the fakes stopped reaching the transmission decision, the
"nothing was sent" assertions would start passing for the wrong reason. The
chain is re-declared at one-point spacing because the proof's maximum width is
one, and the strategy suite's five-point chain cannot express that.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from engine.errors import ConfigError
from engine.options import proof as proof_module
from engine.options.marketdata import MarketDataType, OptionGreeks, OptionQuote
from engine.options.policy import ENV_PREFIX, RiskPolicy
from engine.options.proof import (
    PROOF_AUDIT_TAG,
    PROOF_CONFIGURATION_VERSION,
    PROOF_LEG_COUNT,
    PROOF_MAXIMUM_BROKER_MARGIN,
    PROOF_MAXIMUM_DEFINED_LOSS,
    PROOF_MAXIMUM_OPENING_ORDERS,
    PROOF_MAXIMUM_STRESS_LOSS,
    PROOF_MAXIMUM_WIDTH,
    PROOF_MINIMUM_STRESS_MOVE_FRACTION,
    PROOF_QUANTITY,
    PROOF_QUOTE_MAXIMUM_AGE,
    PROOF_SYMBOL,
    PROOF_TARGET_WIDTH,
    PROOF_VERSION,
    ExecutionProofProfile,
    OpeningOrderBudget,
    PriceEnvelope,
    ProofEntryPreflight,
    RecordingLifecycleSink,
    envelope_for,
    observed_credit,
    vertical_width,
)
from engine.options.runner import run_once
from engine.options.selection import Bias

from reviewer import reviewed  # noqa: E402 - sibling test module, see docstring
from test_options_runner import (  # noqa: E402 - sibling test module, see docstring
    NOW,
    SPOT,
    TODAY,
    FakeBroker,
    FakeIB,
    FakeMarketDataPort,
    FakePortfolioPort,
    gate_for,
    leg_delta,
    leg_mid,
    provenance,
    quote_snapshot,
    spread_intent,
    store_for,
)

D = Decimal

PAPER_PORT = 7497
LIVE_PORT = 7496

#: One-point strikes around the 0.30-delta short. ``leg_delta`` in the strategy
#: suite is exactly -0.30 at 450, and a 24-wide selection window over these 25
#: contracts keeps all of them, so delta selection really has a chain to search.
PROOF_STRIKES = [D(strike) for strike in range(438, 463)]

PROOF_SHORT_STRIKE = D("450")
PROOF_LONG_STRIKE = D("449")

#: What the fake what-if reserves. Deliberately under the proof's $150 broker
#: margin ceiling but well over a tenth of it, so a test that accidentally
#: disabled the margin gate would not pass by the number being trivially small.
PROOF_MARGIN = 70.0

#: mid(450) - mid(449) with the strategy suite's price curve: 15.00 - 14.70.
EXPECTED_CREDIT = D("0.30")


# ---------------------------------------------------------------------------
# A one-point chain, and a what-if that fits inside the proof's margin cap
# ---------------------------------------------------------------------------


class _ProofChain:
    strikes = [float(strike) for strike in PROOF_STRIKES]

    def __init__(self, today: dt.date) -> None:
        self.expirations = [
            (today + dt.timedelta(days=days)).strftime("%Y%m%d") for days in (14, 45, 80)
        ]


class _ProofDetail:
    def __init__(self, strike: float) -> None:
        from test_options_runner import _Contract

        self.contract = _Contract(con_id=int(strike), strike=strike, right="P")


class _ProofOrderState:
    initMarginChange = PROOF_MARGIN  # noqa: N815
    maintMarginChange = PROOF_MARGIN  # noqa: N815
    equityWithLoanChange = -2.0  # noqa: N815
    commission = 1.30
    warningText = ""  # noqa: N815


class ProofIB(FakeIB):
    """The strategy suite's fake, on a one-point chain the proof can actually use.

    ``low_iv`` exists for the one property the proof is allowed to relax. With it
    set the implied-volatility history ends at the *bottom* of its range, so IV
    Rank is near zero and the strategy path would refuse -- which is exactly the
    condition under which a lifecycle proof still needs to be runnable.
    """

    def __init__(self, *, low_iv: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.low_iv = low_iv

    def reqHistoricalData(self, *_a: Any, **_k: Any) -> list[Any]:  # noqa: N802
        from test_options_runner import _Bar

        start = self.today - dt.timedelta(days=365)
        if not self.low_iv:
            return super().reqHistoricalData()
        return [
            _Bar(start + dt.timedelta(days=i), 0.60 - 0.002 * i) for i in range(260)
        ]

    def reqSecDefOptParams(self, *_a: Any, **_k: Any) -> list[Any]:  # noqa: N802
        return [_ProofChain(self.today)]

    def reqContractDetails(self, _contract: Any) -> list[Any]:  # noqa: N802
        return [_ProofDetail(float(strike)) for strike in PROOF_STRIKES]

    def whatIfOrder(self, _contract: Any, _order: Any) -> Any:  # noqa: N802
        return _ProofOrderState()


#: SPY quotes in pennies. The strategy suite's nickel half-spread is fine for a
#: five-wide 1.50 credit (crossing cost 0.07 of mid), but the proof's structure
#: is one point wide with a 0.30 mid credit, where the same nickel puts the
#: crossing cost at 0.33 of mid -- over the liquidity gate's 0.25 cap. The
#: proof's liquid baseline is the penny book SPY actually shows.
PROOF_HALF_SPREAD = D("0.01")


class ProofMarketDataPort(FakeMarketDataPort):
    """The strategy suite's port, quoting the penny-wide book the proof needs.

    Everything except the half-spread is inherited unchanged: mids, deltas,
    liveness, ``price_factor`` drift and the ``calls`` log all behave exactly
    as in :class:`FakeMarketDataPort`, so ``EXPECTED_CREDIT`` (a mid-to-mid
    number) is untouched. Open interest and volume sit comfortably above the
    liquidity floors (OI 500, volume 100) for the same reason the runner
    suite's do: unmeasured counts as insufficient.
    """

    def strategy_quotes(
        self,
        *,
        underlying_symbol: str,
        con_ids: Any,
        require_two_sided: bool = False,
    ) -> Any:
        con_ids = tuple(int(c) for c in con_ids)
        self.calls.append((underlying_symbol, con_ids))
        legs = tuple(self._penny_quote(con_id) for con_id in con_ids)
        return quote_snapshot(
            legs,
            symbol=underlying_symbol,
            underlying_reported=self.reported,
            at=self.at,
        )

    def _penny_quote(self, con_id: int) -> OptionQuote:
        from uuid import uuid4

        mid = leg_mid(D(con_id)) * self.price_factor
        generation = uuid4()
        return OptionQuote(
            con_id=con_id,
            provenance=provenance(generation, reported=self.reported, at=self.at),
            bid=mid - PROOF_HALF_SPREAD,
            ask=mid + PROOF_HALF_SPREAD,
            open_interest=1000,
            volume=500,
            greeks=OptionGreeks(
                received_at=self.at,
                subscription_generation=generation,
                delta=leg_delta(D(con_id)),
            ),
        )


class DriftingMarketDataPort(ProofMarketDataPort):
    """Prices normally until ``drift_after`` calls, then scales every mid.

    The entry path pulls the chain once; the proof's preflight pulls the two
    selected legs again immediately before sending. Drifting between those two
    reads is precisely the situation the price envelope exists to catch, and it
    cannot be modelled by a port that answers the same way every time.
    """

    def __init__(self, *, drift_after: int = 1, factor: str = "2", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.drift_after = drift_after
        self.factor = D(factor)

    def strategy_quotes(
        self, *, underlying_symbol: str, con_ids: Any, **kwargs: Any
    ) -> Any:
        if len(self.calls) >= self.drift_after:
            self.price_factor = self.factor
        return super().strategy_quotes(
            underlying_symbol=underlying_symbol, con_ids=con_ids, **kwargs
        )


def proof_profile(**overrides: Any) -> ExecutionProofProfile:
    settings: dict[str, Any] = {"port": PAPER_PORT, "account": "DU1234567"}
    settings.update(overrides)
    return ExecutionProofProfile(**settings)


def run_proof(
    tmp_path: Path,
    *,
    armed: bool,
    market_data: Any = None,
    ib: Any = None,
    profile: ExecutionProofProfile | None = None,
) -> tuple[Any, ProofEntryPreflight, RecordingLifecycleSink, FakeBroker]:
    """One proof pass through the real ``run_once``, with the real gates."""
    from engine.options.sink import LifecycleRecorder

    profile = profile if profile is not None else proof_profile()
    gate = gate_for(tmp_path)
    store = store_for(tmp_path)
    broker = FakeBroker(ib=ib if ib is not None else ProofIB(low_iv=True))
    budget = OpeningOrderBudget(limit=profile.maximum_opening_orders)
    preflight = ProofEntryPreflight(profile=profile, budget=budget)
    capture = RecordingLifecycleSink(inner=LifecycleRecorder(store))
    # The proof opens risk, so it needs a reviewer like any other entry. The
    # gate is the shipped one over a temp collab; only the reviewer's latency is
    # collapsed, so an unreviewed pass here refuses for the reason the test is
    # about rather than for the missing seat.
    verifier, context = reviewed(tmp_path)

    report = run_once(
        broker,
        gate=gate,
        journal=gate.journal,
        store=store,
        policy=profile.derive_policy({}),
        armed=armed,
        symbol=profile.symbol,
        bias=Bias.BULLISH,
        market_data=market_data if market_data is not None else ProofMarketDataPort(),
        portfolio=FakePortfolioPort(),
        now=NOW,
        today=TODAY,
        account="DU1234567",
        configuration_version=PROOF_CONFIGURATION_VERSION,
        enforce_iv_rank=False,
        entry_preflight=preflight,
        sink=capture,
        verifier=verifier,
        approval_context=context,
    )
    return report, preflight, capture, broker


# ---------------------------------------------------------------------------
# CLI harness
# ---------------------------------------------------------------------------


class _StopTheRun(Exception):
    """Aborts a command once the assertion's subject has been observed."""


class _ContextBroker:
    """What ``broker_factory(config, journal)`` has to look like to the CLI."""

    def __init__(self, _config: Any, _journal: Any) -> None:
        self.ib = ProofIB(low_iv=True)

    def __enter__(self) -> "_ContextBroker":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def positions(self) -> tuple[Any, ...]:
        return ()


def _patch_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the two IBKR adapters for fakes.

    The command imports them from ``engine.options.adapters`` inside the
    function body, so patching the module attribute is what the real call site
    actually resolves -- patching a name in ``engine.cli`` would miss it.

    The quote timestamp is taken at *call* time rather than the suite's frozen
    ``NOW``. The command supplies no ``now`` to ``run_once``, so it reads the
    wall clock, and a fixture stamped in 2026 would be refused by the freshness
    gate -- correctly, and for a reason none of these tests are about.
    """
    from engine.options import adapters

    monkeypatch.setattr(
        adapters,
        "IBKRLiveMarketDataAdapter",
        lambda _ib, requested_type=1: ProofMarketDataPort(
            at=dt.datetime.now(dt.timezone.utc)
        ),
    )
    monkeypatch.setattr(
        adapters, "IBKRPortfolioStateAdapter", lambda _broker: FakePortfolioPort()
    )


def _review_in_line(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Give the command a reviewer, in a collab of its own.

    Two things are needed. ``IBKR_COLLAB_ROOT`` is the first: the command finds
    its collab through :func:`default_collab_root`, and without the override a
    proof run would file its review requests into the operator's real
    correspondence. The second is somebody to answer them -- the command builds
    a plain gate, and an unanswered request is ``AWAITING_VERIFICATION``, so the
    proof would print no lifecycle at all and every assertion below would fail
    for a reason none of these tests are about.

    The swap is of *timing*, not of the gate: :class:`ReviewedGate` is the
    shipped ``CollabVerifierGate`` with the reviewer's turn taken immediately
    before the builder looks, so the whole exchange -- request, claim, answer,
    completion, match -- is still the real one.
    """
    from engine.options import approval

    from reviewer import ReviewedGate, ScriptedReviewer, collab_at

    monkeypatch.setenv(approval.ENV_COLLAB_ROOT, str(collab_at(home)))

    def _gate(**kwargs: Any) -> ReviewedGate:
        return ReviewedGate(**kwargs, reviewer=ScriptedReviewer(root=kwargs["root"]))

    monkeypatch.setattr(approval, "CollabVerifierGate", _gate)


def _proof_args(state_dir: Path, *, execution_proof: bool = True, arm: bool = True) -> Any:
    from engine.cli import build_parser

    argv = [
        "--account",
        "DU1234567",
        "--state-dir",
        str(state_dir),
        "--no-alerts",
        "options-verify-execution",
    ]
    if execution_proof:
        argv.append("--execution-proof")
    if arm:
        argv.append("--arm")
    return build_parser().parse_args(argv)


# ===========================================================================
# Construction refusals
# ===========================================================================


class TestTheProfileRefusesToExistOutsideItsBounds:
    def test_a_default_profile_is_the_documented_scope(self) -> None:
        """Every bound in the brief, read off the constants rather than retyped.

        Written against the constants on purpose: a test with $100 spelled out
        would keep passing after somebody raised the ceiling, which is the one
        change this file has to notice.
        """
        profile = proof_profile()
        assert profile.symbol == PROOF_SYMBOL == "SPY"
        assert profile.quantity == PROOF_QUANTITY == 1
        assert profile.leg_count == PROOF_LEG_COUNT == 2
        assert profile.target_width == PROOF_TARGET_WIDTH == D("1")
        assert profile.maximum_width == PROOF_MAXIMUM_WIDTH == D("1")
        assert profile.maximum_defined_loss == PROOF_MAXIMUM_DEFINED_LOSS == D("100")
        assert profile.maximum_broker_margin == PROOF_MAXIMUM_BROKER_MARGIN == D("150")
        assert profile.maximum_stress_loss == PROOF_MAXIMUM_STRESS_LOSS == D("100")
        assert profile.maximum_opening_orders == PROOF_MAXIMUM_OPENING_ORDERS == 1
        assert profile.audit_tag == PROOF_AUDIT_TAG
        assert profile.version == PROOF_VERSION

    @pytest.mark.parametrize("symbol", ["AAPL", "QQQ", "SPX", "spy ", ""])
    def test_a_non_spy_symbol_is_refused(self, symbol: str) -> None:
        if symbol.strip().upper() == "SPY":
            pytest.skip("that one is SPY with whitespace, which is allowed")
        with pytest.raises(ConfigError, match="SPY"):
            proof_profile(symbol=symbol)

    def test_spy_with_whitespace_and_case_is_still_spy(self) -> None:
        """The refusal must be about the underlying, not about typing."""
        assert proof_profile(symbol=" spy ").symbol == " spy "

    @pytest.mark.parametrize("port", [LIVE_PORT, 4001, 8080, 0, -1])
    def test_a_non_paper_port_is_refused(self, port: int) -> None:
        with pytest.raises(ConfigError, match="paper port"):
            proof_profile(port=port)

    def test_both_paper_ports_are_accepted(self) -> None:
        """The inverse assertion: a refusal that refused everything would pass
        every test above while making the command unusable."""
        assert proof_profile(port=7497).port == 7497
        assert proof_profile(port=4002).port == 4002

    @pytest.mark.parametrize("quantity", [0, 2, 10, -1])
    def test_any_quantity_other_than_one_is_refused(self, quantity: int) -> None:
        with pytest.raises(ConfigError, match="exactly 1 contract"):
            proof_profile(quantity=quantity)

    def test_a_bool_is_not_an_acceptable_quantity(self) -> None:
        """``True == 1``. Without an explicit bool check, ``quantity=True``
        sails through the equality test and produces a profile whose quantity
        prints as ``True``."""
        with pytest.raises(ConfigError, match="must be an int"):
            proof_profile(quantity=True)

    @pytest.mark.parametrize(
        ("field", "widened"),
        [
            ("maximum_defined_loss", D("500")),
            ("maximum_broker_margin", D("500")),
            ("maximum_stress_loss", D("500")),
            ("target_width", D("5")),
            ("maximum_width", D("5")),
        ],
    )
    def test_a_widened_money_bound_is_refused(
        self, field: str, widened: Decimal
    ) -> None:
        """The bounds are a schema. A caller asking for a looser one has
        misunderstood something, and silently clamping would leave the
        misunderstanding in place."""
        with pytest.raises(ConfigError, match="ceiling"):
            proof_profile(**{field: widened})

    @pytest.mark.parametrize(
        ("field", "tightened"),
        [
            ("maximum_defined_loss", D("25")),
            ("maximum_broker_margin", D("40")),
            ("maximum_stress_loss", D("25")),
            ("target_width", D("0.5")),
            ("maximum_width", D("0.5")),
        ],
    )
    def test_a_tightened_money_bound_is_accepted(
        self, field: str, tightened: Decimal
    ) -> None:
        assert getattr(proof_profile(**{field: tightened}), field) == tightened

    @pytest.mark.parametrize("value", [D("0"), D("-1")])
    def test_a_non_positive_bound_is_refused(self, value: Decimal) -> None:
        """A cap of zero disables the check it exists to perform."""
        with pytest.raises(ConfigError, match="greater than zero"):
            proof_profile(maximum_defined_loss=value)

    def test_more_than_one_opening_order_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="1 order per session"):
            proof_profile(maximum_opening_orders=2)

    def test_a_leg_count_other_than_a_vertical_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="one vertical"):
            proof_profile(leg_count=4)

    def test_the_audit_tag_is_not_a_callers_to_choose(self) -> None:
        """It is how a proof order is told apart from a strategy order in the
        durable log, which stops being true the moment a caller can set it."""
        with pytest.raises(ConfigError, match="audit tag is fixed"):
            proof_profile(audit_tag="LOOKS_LIKE_A_STRATEGY")

    def test_the_version_is_not_a_callers_to_choose(self) -> None:
        with pytest.raises(ConfigError, match="version is fixed"):
            proof_profile(version="options-runner/1")

    def test_the_audit_tag_is_distinct_from_the_strategy_runners(self) -> None:
        from engine.options.runner import CONFIG_VERSION

        assert PROOF_CONFIGURATION_VERSION != CONFIG_VERSION
        assert PROOF_AUDIT_TAG not in CONFIG_VERSION

    def test_the_fingerprint_moves_with_the_bounds_and_not_with_the_account(
        self,
    ) -> None:
        """What the fingerprint answers is "were the bounds the same", so two
        paper accounts under identical bounds must agree, and a tightened bound
        must not."""
        assert (
            proof_profile(account="DU1").fingerprint()
            == proof_profile(account="DU2").fingerprint()
        )
        assert (
            proof_profile().fingerprint()
            != proof_profile(maximum_defined_loss=D("50")).fingerprint()
        )


# ===========================================================================
# The env-cannot-widen property
# ===========================================================================


#: Every variable ``RiskPolicy.from_env`` reads, set to the largest value that
#: still parses. A few are bounded by their own validation -- a fraction above 1
#: and a delta at or above 1 are refused before any clamping runs -- so those
#: carry the loosest *legal* value instead.
HUGE_ENV: dict[str, str] = {
    f"{ENV_PREFIX}MAX_DEFINED_LOSS_PER_POSITION": "99999999",
    f"{ENV_PREFIX}MAX_DEFINED_LOSS_FRACTION": "1",
    f"{ENV_PREFIX}MAX_BROKER_MARGIN_PER_POSITION": "99999999",
    f"{ENV_PREFIX}MAX_BROKER_MARGIN_FRACTION": "1",
    # Smaller is looser for the stress move: a 0.1% adverse move is a test that
    # nothing can fail. This is the one field clamped upward.
    f"{ENV_PREFIX}STRESS_MOVE_FRACTION": "0.001",
    f"{ENV_PREFIX}MAX_STRESS_LOSS_PER_POSITION": "99999999",
    f"{ENV_PREFIX}MAX_STRESS_LOSS_FRACTION": "1",
    f"{ENV_PREFIX}QUOTE_MAXIMUM_AGE_SECONDS": "86400",
    f"{ENV_PREFIX}MAX_TOTAL_BPR_FRACTION": "1",
    f"{ENV_PREFIX}MAX_INCREMENTAL_BPR_FRACTION": "1",
    f"{ENV_PREFIX}MAX_UNDERLYING_BPR_FRACTION": "1",
    f"{ENV_PREFIX}MAX_SECTOR_BPR_FRACTION": "1",
    f"{ENV_PREFIX}MAX_CORRELATION_GROUP_BPR_FRACTION": "1",
    f"{ENV_PREFIX}PORTFOLIO_SNAPSHOT_MAXIMUM_AGE_SECONDS": "86400",
    f"{ENV_PREFIX}TARGET_WIDTH": "500",
    f"{ENV_PREFIX}RISK_BUDGET_PER_POSITION": "99999999",
}

#: Fields the proof deliberately leaves to the environment, with the reason.
#: None of them can widen the blast radius of a proof order: the two deltas and
#: the management rules choose *which* structure and *when* to close it, and the
#: classification maps only ever add refusals. Named here rather than skipped
#: silently, so a new risk field added to ``RiskPolicy`` fails the sweep below
#: instead of quietly escaping the clamp.
DELIBERATELY_FREE_FIELDS = frozenset(
    {
        "neutral_target_delta",
        "directional_target_delta",
        "profit_target_fraction",
        "management_dte",
        "roll_at_management_dte",
        "sectors",
        "correlation_groups",
        "version",
    }
)


class TestNoEnvironmentVariableCanWidenTheProof:
    def test_a_maximal_environment_changes_no_bounded_field(self) -> None:
        """The property the whole design exists for.

        Every ``IBKR_OPTIONS_*`` variable the policy reads is set to the loosest
        value it will accept, and the derived policy is compared field by field
        against one derived from an empty environment. Sweeping over
        ``dataclasses.fields`` rather than a hand-written list is what makes a
        *future* risk field, added to ``RiskPolicy`` and forgotten here, fail
        this test instead of escaping the clamp.
        """
        profile = proof_profile()
        clean = profile.derive_policy({})
        widened = profile.derive_policy(HUGE_ENV)

        drifted = [
            field.name
            for field in dataclasses.fields(RiskPolicy)
            if field.name not in DELIBERATELY_FREE_FIELDS
            and getattr(clean, field.name) != getattr(widened, field.name)
        ]
        assert drifted == [], (
            "an environment variable moved a bounded field of the proof policy: "
            f"{drifted}"
        )

    def test_the_maximal_environment_really_would_have_moved_the_real_policy(
        self,
    ) -> None:
        """The control. If ``HUGE_ENV`` had no effect on an unclamped policy,
        the test above would be passing because the environment was ignored --
        which proves nothing about the clamp."""
        base = RiskPolicy.from_env(HUGE_ENV)
        assert base.max_defined_loss_per_position == D("99999999")
        assert base.max_broker_margin_per_position == D("99999999")
        assert base.max_stress_loss_per_position == D("99999999")
        assert base.target_width == D("500")
        assert base.stress_move_fraction == D("0.001")
        assert base.quote_maximum_age == dt.timedelta(seconds=86400)

    def test_the_derived_policy_lands_exactly_on_the_ceilings(self) -> None:
        policy = proof_profile().derive_policy(HUGE_ENV)
        assert policy.max_defined_loss_per_position == PROOF_MAXIMUM_DEFINED_LOSS
        assert policy.max_broker_margin_per_position == PROOF_MAXIMUM_BROKER_MARGIN
        assert policy.max_stress_loss_per_position == PROOF_MAXIMUM_STRESS_LOSS
        assert policy.target_width == PROOF_TARGET_WIDTH
        assert policy.risk_budget_per_position == PROOF_MAXIMUM_DEFINED_LOSS
        assert policy.quote_maximum_age == PROOF_QUOTE_MAXIMUM_AGE
        assert policy.stress_move_fraction == PROOF_MINIMUM_STRESS_MOVE_FRACTION

    def test_a_tightening_environment_is_still_honoured(self) -> None:
        """The clamp is a ceiling, not an override. An operator who wants a $10
        proof must get one -- otherwise "min()" is just a constant."""
        policy = proof_profile().derive_policy(
            {
                f"{ENV_PREFIX}MAX_DEFINED_LOSS_PER_POSITION": "10",
                f"{ENV_PREFIX}MAX_BROKER_MARGIN_PER_POSITION": "20",
                f"{ENV_PREFIX}QUOTE_MAXIMUM_AGE_SECONDS": "2",
                # Bigger is stricter here, so a raised value must survive.
                f"{ENV_PREFIX}STRESS_MOVE_FRACTION": "0.40",
            }
        )
        assert policy.max_defined_loss_per_position == D("10")
        assert policy.max_broker_margin_per_position == D("20")
        assert policy.quote_maximum_age == dt.timedelta(seconds=2)
        assert policy.stress_move_fraction == D("0.40")

    def test_a_tightened_profile_beats_a_looser_environment(self) -> None:
        policy = proof_profile(maximum_defined_loss=D("25")).derive_policy(HUGE_ENV)
        assert policy.max_defined_loss_per_position == D("25")

    def test_the_process_environment_is_read_when_no_dict_is_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``derive_policy()`` with no argument reads ``os.environ``. If it did
        not, every assertion above would be about a code path the command never
        takes."""
        for key, value in HUGE_ENV.items():
            monkeypatch.setenv(key, value)
        policy = proof_profile().derive_policy()
        assert policy.max_defined_loss_per_position == PROOF_MAXIMUM_DEFINED_LOSS
        assert policy.stress_move_fraction == PROOF_MINIMUM_STRESS_MOVE_FRACTION


# ===========================================================================
# Bounds that live on the candidate rather than the policy
# ===========================================================================


class TestTheIntentBoundsRefuseWhatThePolicyCannot:
    def test_a_conforming_one_wide_vertical_passes(self) -> None:
        intent = spread_intent(expiration=TODAY + dt.timedelta(days=45))
        narrow = dataclasses.replace(
            intent,
            legs=(
                dataclasses.replace(intent.legs[0], strike=PROOF_SHORT_STRIKE),
                dataclasses.replace(intent.legs[1], strike=PROOF_LONG_STRIKE),
            ),
            limit_price=D("0.30"),
            maximum_loss_per_contract=D("70"),
        )
        assert proof_profile().check_intent(narrow) == ()

    def test_a_five_wide_vertical_is_refused(self) -> None:
        """The strategy's own default width. It is a perfectly good trade and a
        completely unacceptable proof."""
        intent = spread_intent(expiration=TODAY + dt.timedelta(days=45))
        problems = proof_profile().check_intent(intent)
        assert any("wide" in problem for problem in problems), problems

    def test_a_non_spy_candidate_is_refused(self) -> None:
        intent = spread_intent(
            expiration=TODAY + dt.timedelta(days=45), underlying="AAPL"
        )
        problems = proof_profile().check_intent(intent)
        assert any("SPY" in problem for problem in problems), problems

    def test_a_multi_lot_candidate_is_refused(self) -> None:
        intent = spread_intent(expiration=TODAY + dt.timedelta(days=45), quantity=3)
        problems = proof_profile().check_intent(intent)
        assert any("exactly 1 contract" in problem for problem in problems), problems

    def test_a_candidate_whose_defined_loss_exceeds_the_cap_is_refused(self) -> None:
        intent = spread_intent(expiration=TODAY + dt.timedelta(days=45), credit="0.10")
        problems = proof_profile().check_intent(intent)
        assert any("defined loss" in problem for problem in problems), problems

    def test_the_width_of_a_non_vertical_is_none_rather_than_zero(self) -> None:
        """Zero width is itself a very bad answer -- a short leg protected on
        its own strike -- so it must not be the value that also means "this is
        not a two-strike structure".

        Driven through stubs rather than real intents because
        :class:`~engine.options.domain.OptionStrategyIntent` refuses both shapes
        at construction: a same-strike vertical fails strike ordering and a
        four-leg structure is a different strategy type. That refusal is the
        primary defence and this function is the second, so it is tested where
        it can actually be reached.
        """

        class _Leg:
            def __init__(self, strike: Decimal) -> None:
                self.strike = strike

        class _Structure:
            def __init__(self, *strikes: str) -> None:
                self.legs = tuple(_Leg(D(strike)) for strike in strikes)

        assert vertical_width(_Structure("450", "450")) is None
        assert vertical_width(_Structure("450", "445", "440", "435")) is None
        assert vertical_width(_Structure("450", "445")) == D("5")
        assert (
            vertical_width(spread_intent(expiration=TODAY + dt.timedelta(days=45)))
            == D("5")
        )


# ===========================================================================
# The price envelope
# ===========================================================================


class TestThePriceEnvelope:
    def test_the_envelope_brackets_the_intended_credit_on_both_sides(self) -> None:
        """Bounded above as well as below. A book that moved in our favour is
        still a book that moved, and the maximum loss, stress loss and broker
        margin were all computed against the old credit."""
        intent = spread_intent(expiration=TODAY + dt.timedelta(days=45), credit="1.50")
        envelope = envelope_for(intent)
        assert envelope.minimum < intent.limit_price < envelope.maximum
        assert envelope.contains(D("1.50"))
        assert not envelope.contains(D("1.20"))
        assert not envelope.contains(D("1.80"))

    def test_the_envelope_never_allows_a_credit_at_or_above_the_width(self) -> None:
        """A credit at the width is a claim to be paid more than the structure
        can ever lose, which is a mispriced book rather than a gift."""
        intent = spread_intent(expiration=TODAY + dt.timedelta(days=45), credit="4.90")
        envelope = envelope_for(intent)
        assert envelope.maximum < D("5")

    def test_the_envelope_never_allows_a_worthless_credit(self) -> None:
        intent = spread_intent(expiration=TODAY + dt.timedelta(days=45), credit="0.02")
        assert envelope_for(intent).minimum > D("0")

    def test_an_unpriceable_book_is_outside_every_envelope(self) -> None:
        envelope = PriceEnvelope(reference=D("1"), minimum=D("0.5"), maximum=D("1.5"))
        assert not envelope.contains(None)

    def test_the_observed_credit_is_shorts_minus_longs(self) -> None:
        intent = spread_intent(expiration=TODAY + dt.timedelta(days=45))
        port = FakeMarketDataPort()
        snapshot = port.strategy_quotes(
            underlying_symbol="SPY",
            con_ids=[leg.con_id for leg in intent.legs],
        )
        expected = leg_mid(intent.legs[0].strike) - leg_mid(intent.legs[1].strike)
        assert observed_credit(intent, snapshot) == expected

    def test_a_missing_leg_quote_is_unpriceable_rather_than_zero(self) -> None:
        intent = spread_intent(expiration=TODAY + dt.timedelta(days=45))
        port = FakeMarketDataPort()
        partial = port.strategy_quotes(
            underlying_symbol="SPY", con_ids=[intent.legs[0].con_id]
        )
        assert observed_credit(intent, partial) is None
        assert observed_credit(intent, None) is None


# ===========================================================================
# The session budget
# ===========================================================================


class TestTheOpeningOrderBudget:
    def test_exactly_one_order_may_be_claimed(self) -> None:
        budget = OpeningOrderBudget()
        assert budget.limit == PROOF_MAXIMUM_OPENING_ORDERS == 1
        assert budget.claim() is True
        assert budget.exhausted is True
        assert budget.claim() is False
        assert budget.spent == 1

    def test_a_refused_preflight_does_not_spend_the_budget(self) -> None:
        """A run refused for a moved book must be re-runnable. Spending the
        budget on a refusal would make the second attempt fail for a reason
        that has nothing to do with the market."""
        profile = proof_profile()
        budget = OpeningOrderBudget()
        preflight = ProofEntryPreflight(profile=profile, budget=budget)
        # Five-wide: refused at step 1, before the envelope is even built.
        refusal = preflight(
            intent=spread_intent(expiration=TODAY + dt.timedelta(days=45)),
            snapshot=None,
            market_data=FakeMarketDataPort(),
            policy=profile.derive_policy({}),
            now=NOW,
        )
        assert refusal is not None
        assert budget.spent == 0

    def test_an_unarmed_pass_does_not_spend_the_budget(self, tmp_path: Path) -> None:
        """The obvious workflow is: look at it unarmed, then arm it. Charging
        the dry run would make the second step fail for a reason that has
        nothing to do with the market."""
        _, preflight, _, broker = run_proof(tmp_path, armed=False)
        assert broker.ib.placed == []
        assert preflight.budget.spent == 0
        assert preflight.envelope is not None, (
            "the envelope must still be evaluated unarmed -- that is what a dry "
            "run is for"
        )
        assert preflight.refusal is None


# ===========================================================================
# Lifecycle capture
# ===========================================================================


class _Recording:
    def __init__(self) -> None:
        self.seen: list[tuple[Any, Any, bool]] = []

    def observe(self, strategy_id: Any, observation: Any, *, closing: bool = False) -> bool:
        self.seen.append((strategy_id, observation, closing))
        return True


class TestTheCaptureSinkRecordsWithoutSwallowing:
    def _snapshot(self, state: Any, **kwargs: Any) -> Any:
        from engine.options.orderstate import BrokerOrderSnapshot

        return BrokerOrderSnapshot(state=state, observed_at=NOW, **kwargs)

    def test_every_observation_reaches_both_the_log_and_the_inner_sink(self) -> None:
        """The in-memory log is a report and the inner sink is the record of
        truth. A capture that forwarded some observations and not others would
        make the printed timeline disagree with the durable one."""
        from engine.options.orderstate import OrderLifecycleState

        inner = _Recording()
        capture = RecordingLifecycleSink(inner=inner)
        strategy_id = uuid4()
        for state in (
            OrderLifecycleState.SUBMITTED,
            OrderLifecycleState.SUBMITTED,
            OrderLifecycleState.ACKNOWLEDGED,
            OrderLifecycleState.FILLED,
        ):
            capture.observe(strategy_id, self._snapshot(state))

        assert len(capture.observations) == 4
        assert len(inner.seen) == 4

    def test_duplicates_are_kept_because_silence_is_the_symptom(self) -> None:
        """The store deduplicates because an event log records what changed.
        This is the opposite artefact: "the broker said nothing new for four
        polls" is exactly what an operator is looking for."""
        from engine.options.orderstate import OrderLifecycleState

        capture = RecordingLifecycleSink()
        strategy_id = uuid4()
        for _ in range(4):
            capture.observe(
                strategy_id, self._snapshot(OrderLifecycleState.SUBMITTED)
            )
        assert len(capture.timeline()) == 4

    def test_the_timeline_carries_every_field_the_proof_promises(self) -> None:
        from engine.options.orderstate import OrderLifecycleState

        capture = RecordingLifecycleSink()
        capture.observe(
            uuid4(),
            self._snapshot(
                OrderLifecycleState.PARTIALLY_FILLED,
                raw_status="Submitted",
                order_id=77,
                perm_id=987654321,
                filled=D("1"),
                remaining=D("2"),
                average_price=D("-0.30"),
                commission=D("1.30"),
                message="partially filled",
            ),
        )
        line = "\n".join(capture.timeline())
        for expected in (
            "order=77",
            "perm=987654321",
            "filled=1",
            "remaining=2",
            "avg=-0.30",
            "commission=1.30",
            "Submitted",
            "partially filled",
        ):
            assert expected in line, f"{expected!r} missing from:\n{line}"

    def test_the_capture_sink_cannot_send_or_authorize_anything(self) -> None:
        """It is handed to code running inside the polling loop, where the
        authorization token is long out of scope."""
        forbidden = {
            "place", "place_order", "placeOrder", "submit", "send",
            "cancel", "retry", "authorize", "authorize_open", "authorize_close",
            "build_combo",
        }
        public = {name for name in dir(RecordingLifecycleSink) if not name.startswith("_")}
        assert public & forbidden == set()


# ===========================================================================
# Through the real pipeline
# ===========================================================================


class TestTheProofReachesTheTransmissionDecision:
    """If these fail, every refusal assertion below is passing because the pass
    died early rather than because a bound held."""

    def test_an_armed_proof_sends_exactly_one_bounded_limit_order(
        self, tmp_path: Path
    ) -> None:
        report, preflight, capture, broker = run_proof(tmp_path, armed=True)

        assert report.blockers == [], report.blockers
        assert len(broker.ib.placed) == 1, report.errors
        assert report.entered is True

        _, order = broker.ib.placed[0]
        # A bounded limit, never a market order. A credit is submitted as a BUY
        # at a negative limit, so the sign is part of the assertion.
        assert order.orderType == "LMT"
        assert float(order.lmtPrice) == pytest.approx(-float(EXPECTED_CREDIT))
        assert order.totalQuantity == 1
        assert order.orderRef == str(report.candidate.strategy_id)

    def test_the_envelope_was_computed_before_the_order_went_out(
        self, tmp_path: Path
    ) -> None:
        _, preflight, _, broker = run_proof(tmp_path, armed=True)
        assert preflight.envelope is not None
        assert preflight.credit_at_send == EXPECTED_CREDIT
        assert preflight.envelope.contains(preflight.credit_at_send)
        assert preflight.refusal is None

    def test_the_iv_rank_filter_was_bypassed_and_said_so(self, tmp_path: Path) -> None:
        """The one filter the proof may skip. The refusal code is still recorded,
        so the journal says plainly that the strategy path would have refused."""
        report, _, _, broker = run_proof(tmp_path, armed=True)
        assert "OPTIONS_IV_RANK_FILTER_BYPASSED" in report.refusal_codes
        assert report.iv_rank is not None
        assert report.iv_rank.iv_rank < D("50")
        assert len(broker.ib.placed) == 1

    def test_the_candidate_is_stamped_with_the_proof_configuration(
        self, tmp_path: Path
    ) -> None:
        """A record carrying this value was produced by the proof and by nothing
        else, which is the whole reason the durable log can be read later."""
        report, _, _, _ = run_proof(tmp_path, armed=True)
        assert report.candidate.configuration_version == PROOF_CONFIGURATION_VERSION

    def test_the_structure_really_is_one_one_wide_spy_vertical(
        self, tmp_path: Path
    ) -> None:
        report, _, _, _ = run_proof(tmp_path, armed=True)
        candidate = report.candidate
        assert candidate.underlying == "SPY"
        assert candidate.quantity == 1
        assert len(candidate.legs) == 2
        assert vertical_width(candidate) == D("1")
        assert candidate.total_maximum_loss <= PROOF_MAXIMUM_DEFINED_LOSS

    def test_the_lifecycle_was_captured_and_persisted(self, tmp_path: Path) -> None:
        report, _, capture, _ = run_proof(tmp_path, armed=True)
        assert capture.observations, "no broker observations were captured"
        states = {snapshot.state.value for _, snapshot, _ in capture.observations}
        assert "ORDER_FILLED" in states
        assert report.transmissions[0].order_id == 77

    def test_the_session_spent_exactly_one_opening_order(self, tmp_path: Path) -> None:
        _, preflight, _, _ = run_proof(tmp_path, armed=True)
        assert preflight.budget.spent == 1
        assert preflight.budget.exhausted is True


class TestTheProofRefusesWhenABoundWouldBeBroken:
    def test_an_unarmed_proof_sends_nothing(self, tmp_path: Path) -> None:
        report, _, _, broker = run_proof(tmp_path, armed=False)
        assert broker.ib.placed == []
        assert report.entered is False
        assert any("arm" in blocker.lower() for blocker in report.blockers), (
            report.blockers
        )

    def test_a_book_that_drifts_out_of_the_envelope_refuses_before_sending(
        self, tmp_path: Path
    ) -> None:
        """The refusal happens before ``authorize_open``, so no authorization
        token for this candidate ever exists -- not merely an unused one."""
        report, preflight, capture, broker = run_proof(
            tmp_path, armed=True, market_data=DriftingMarketDataPort(drift_after=1)
        )
        assert broker.ib.placed == []
        assert capture.observations == []
        assert preflight.refusal is not None
        assert "outside the price envelope" in preflight.refusal
        assert "OPTIONS_ENTRY_PREFLIGHT_REFUSED" in report.refusal_codes
        assert preflight.budget.spent == 0

    def test_a_book_that_cannot_be_re_read_refuses(self, tmp_path: Path) -> None:
        """Fail closed. An unpriceable book is exactly when a limit order is
        most dangerous, not a reason to fall back on the stale price."""

        class Breaking(ProofMarketDataPort):
            def strategy_quotes(
                self, *, underlying_symbol: str, con_ids: Any, **kwargs: Any
            ) -> Any:
                if self.calls:
                    raise RuntimeError("subscription dropped")
                return super().strategy_quotes(
                    underlying_symbol=underlying_symbol, con_ids=con_ids, **kwargs
                )

        report, preflight, _, broker = run_proof(
            tmp_path, armed=True, market_data=Breaking()
        )
        assert broker.ib.placed == []
        assert preflight.refusal is not None
        assert "could not be re-read" in preflight.refusal

    def test_a_non_live_re_read_refuses(self, tmp_path: Path) -> None:
        report, preflight, _, broker = run_proof(
            tmp_path,
            armed=True,
            market_data=FakeMarketDataPort(reported=MarketDataType.DELAYED),
        )
        assert broker.ib.placed == []
        assert report.blockers, "delayed data reached the transmission decision"

    def test_a_five_wide_chain_cannot_produce_a_proof_order(
        self, tmp_path: Path
    ) -> None:
        """The strategy suite's own chain, which only lists five-point strikes.
        The proof refuses it rather than quietly sending a five-wide spread."""
        report, preflight, _, broker = run_proof(
            tmp_path, armed=True, ib=FakeIB()
        )
        assert broker.ib.placed == []
        assert report.blockers, "a five-wide vertical reached the broker"

    def test_the_kill_switch_stops_the_proof_before_anything_else(
        self, tmp_path: Path
    ) -> None:
        from engine.errors import HaltedError

        gate = gate_for(tmp_path)
        gate.config.halt_file.write_text("halted for a test", encoding="utf-8")
        with pytest.raises(HaltedError):
            run_once(
                FakeBroker(ib=ProofIB(low_iv=True)),
                gate=gate,
                journal=gate.journal,
                store=store_for(tmp_path),
                policy=proof_profile().derive_policy({}),
                armed=True,
                symbol="SPY",
                bias=Bias.BULLISH,
                market_data=FakeMarketDataPort(),
                portfolio=FakePortfolioPort(),
                now=NOW,
                today=TODAY,
                account="DU1234567",
                enforce_iv_rank=False,
                entry_preflight=ProofEntryPreflight(
                    profile=proof_profile(), budget=OpeningOrderBudget()
                ),
            )

    def test_a_preflight_that_raises_refuses_rather_than_escaping(
        self, tmp_path: Path
    ) -> None:
        """A broken bound must fail closed. A preflight exception escaping
        ``run_once`` would be an entry refused by traceback, which is the one
        outcome the report cannot describe."""

        def exploding(**_kwargs: Any) -> str | None:
            raise RuntimeError("the bound is broken")

        gate = gate_for(tmp_path)
        broker = FakeBroker(ib=ProofIB(low_iv=True))
        report = run_once(
            broker,
            gate=gate,
            journal=gate.journal,
            store=store_for(tmp_path),
            policy=proof_profile().derive_policy({}),
            armed=True,
            symbol="SPY",
            bias=Bias.BULLISH,
            # The penny book: the candidate must clear every real gate so the
            # refusal this test observes is the exploding preflight's, not a
            # liquidity refusal that fired before the preflight was reached.
            market_data=ProofMarketDataPort(),
            portfolio=FakePortfolioPort(),
            now=NOW,
            today=TODAY,
            account="DU1234567",
            enforce_iv_rank=False,
            entry_preflight=exploding,
        )
        assert broker.ib.placed == []
        assert "OPTIONS_ENTRY_PREFLIGHT_REFUSED" in report.refusal_codes


# ===========================================================================
# The command itself
# ===========================================================================


class TestTheExecutionProofCommand:
    def test_the_command_prints_the_bounds_the_identity_and_the_lifecycle(
        self, monkeypatch: pytest.MonkeyPatch, state_dir: Path, capsys: Any
    ) -> None:
        """One assertion per thing lane 3 promised to capture.

        Written against the printed output rather than the return value on
        purpose: what the operator can see *is* the deliverable here, and a
        proof whose evidence only exists inside a Python object has not proved
        anything to the person running it.
        """
        from engine import cli
        from engine.errors import EXIT_OK

        _patch_adapters(monkeypatch)
        _review_in_line(monkeypatch, state_dir.parent)
        code = cli.cmd_options_execution_proof(
            _proof_args(state_dir), broker_factory=_ContextBroker
        )
        assert code == EXIT_OK
        printed = capsys.readouterr().out

        for expected in (
            "EXECUTION PROOF",
            PROOF_AUDIT_TAG,
            PROOF_VERSION,
            "PRICE ENVELOPE",
            "ORDER IDENTITY",
            "orderRef",
            "orderId",
            "permId",
            "BROKER LIFECYCLE",
            "DURABLE STORE EVENTS",
            "RESTART RECONCILIATION",
            "SESSION BUDGET",
            "opening orders 1 of 1 used",
            proof_profile().fingerprint(),
        ):
            assert expected in printed, f"{expected!r} missing from the proof output"

    def test_the_envelope_is_printed_before_the_lifecycle_it_bounds(
        self, monkeypatch: pytest.MonkeyPatch, state_dir: Path, capsys: Any
    ) -> None:
        """Ordering is the whole point. An envelope printed after the fill is a
        description; printed before it, it is a control the operator can act on."""
        from engine import cli

        _patch_adapters(monkeypatch)
        _review_in_line(monkeypatch, state_dir.parent)
        cli.cmd_options_execution_proof(
            _proof_args(state_dir), broker_factory=_ContextBroker
        )
        printed = capsys.readouterr().out
        assert printed.index("PRICE ENVELOPE") < printed.index("BROKER LIFECYCLE")
        assert printed.index("WITHIN BAND") < printed.index("ORDER IDENTITY")

    def test_an_unarmed_proof_command_sends_nothing(
        self, monkeypatch: pytest.MonkeyPatch, state_dir: Path, capsys: Any
    ) -> None:
        from engine import cli

        _patch_adapters(monkeypatch)
        _review_in_line(monkeypatch, state_dir.parent)
        cli.cmd_options_execution_proof(
            _proof_args(state_dir, arm=False), broker_factory=_ContextBroker
        )
        printed = capsys.readouterr().out
        assert "armed          NO" in printed
        assert "opening orders 0 of 1 used" in printed

    def test_the_proof_journals_its_profile_and_its_observations(
        self, monkeypatch: pytest.MonkeyPatch, state_dir: Path
    ) -> None:
        """The durable half. A month from now the printed timeline is gone and
        this record is the only thing that can say what the run was bounded by."""
        import json

        from engine import cli

        _patch_adapters(monkeypatch)
        _review_in_line(monkeypatch, state_dir.parent)
        cli.cmd_options_execution_proof(
            _proof_args(state_dir), broker_factory=_ContextBroker
        )

        records = [
            json.loads(line)
            for line in (state_dir / "orders.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        proofs = [r for r in records if r.get("event") == "options_execution_proof"]
        assert len(proofs) == 1
        record = proofs[0]
        assert record["audit_tag"] == PROOF_AUDIT_TAG
        assert record["profile_fingerprint"] == proof_profile().fingerprint()
        assert record["profile"]["maximum_defined_loss"] == str(
            PROOF_MAXIMUM_DEFINED_LOSS
        )
        assert record["opening_orders_used"] == 1
        assert record["price_envelope"] is not None
        assert record["observations"], "no broker observations were journalled"
        assert record["restart_reconciliation"] is not None

    def test_a_live_port_never_reaches_the_profile_or_the_broker(
        self, state_dir: Path
    ) -> None:
        """Two independent refusals, and this asserts the outer one fires first.

        ``EngineConfig`` refuses a live port at construction, so the command
        stops before ``ExecutionProofProfile`` is built and long before a socket
        exists. The profile's own paper-port check is the second line, for a
        caller that builds one directly.
        """
        from engine import cli
        from engine.errors import UnsafeConfigError

        class Exploding:
            def __init__(self, *_a: Any, **_k: Any) -> None:
                raise AssertionError("the proof connected before checking the port")

        from engine.cli import build_parser

        args = build_parser().parse_args(
            [
                "--account",
                "DU1234567",
                "--state-dir",
                str(state_dir),
                "--port",
                str(LIVE_PORT),
                "--no-alerts",
                "options-verify-execution",
                "--execution-proof",
                "--arm",
            ]
        )
        with pytest.raises(UnsafeConfigError):
            cli.cmd_options_execution_proof(args, broker_factory=Exploding)

    def test_the_verify_command_dispatches_to_the_proof_only_when_flagged(
        self, monkeypatch: pytest.MonkeyPatch, state_dir: Path
    ) -> None:
        from engine import cli

        calls: list[str] = []
        monkeypatch.setattr(
            cli,
            "cmd_options_execution_proof",
            lambda _args, _factory=None: calls.append("proof") or 0,
        )
        cli.cmd_options_verify_execution(
            _proof_args(state_dir), broker_factory=_ContextBroker
        )
        assert calls == ["proof"]


# ===========================================================================
# The strategy path is untouched
# ===========================================================================


class TestTheStrategyPathIsUnchanged:
    def test_the_production_iv_rank_default_is_still_fifty(self) -> None:
        """This lane exists so that number never has to move."""
        from engine.cli import build_parser

        parser = build_parser()
        for command in ("options-run", "options-scan", "options-verify-execution"):
            args = parser.parse_args([command])
            assert args.min_iv_rank == 50.0, command

    def test_run_once_still_enforces_iv_rank_by_default(self, tmp_path: Path) -> None:
        """The bypass is opt-in. A default that skipped the filter would make
        every strategy run a proof run without saying so."""
        gate = gate_for(tmp_path)
        broker = FakeBroker(ib=ProofIB(low_iv=True))
        report = run_once(
            broker,
            gate=gate,
            journal=gate.journal,
            store=store_for(tmp_path),
            policy=proof_profile().derive_policy({}),
            armed=True,
            symbol="SPY",
            bias=Bias.BULLISH,
            market_data=FakeMarketDataPort(),
            portfolio=FakePortfolioPort(),
            now=NOW,
            today=TODAY,
            account="DU1234567",
        )
        assert broker.ib.placed == []
        assert any("IV Rank" in blocker for blocker in report.blockers), report.blockers

    def test_the_proof_flag_is_off_unless_asked_for(self) -> None:
        from engine.cli import build_parser

        parser = build_parser()
        assert parser.parse_args(["options-verify-execution"]).execution_proof is False
        assert (
            parser.parse_args(
                ["options-verify-execution", "--execution-proof"]
            ).execution_proof
            is True
        )

    def test_the_proof_flag_does_not_exist_on_the_non_transmitting_commands(
        self,
    ) -> None:
        from engine.cli import build_parser

        parser = build_parser()
        for command in ("options-scan", "probe-options-data"):
            with pytest.raises(SystemExit):
                parser.parse_args([command, "--execution-proof"])

    def test_the_ordinary_verify_command_still_runs_the_strategy_policy(
        self, monkeypatch: pytest.MonkeyPatch, state_dir: Path
    ) -> None:
        """Without ``--execution-proof`` the command is what it always was.

        Asserted by recording the policy ``run_once`` was handed: the strategy
        path must still get ``RiskPolicy.from_env()``, not a proof policy with
        $100 ceilings folded in.
        """
        from engine import cli
        from engine.options import runner as runner_module

        seen: list[RiskPolicy] = []

        def spy(*_args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs["policy"])
            raise _StopTheRun

        monkeypatch.setattr(runner_module, "run_once", spy)
        _patch_adapters(monkeypatch)

        args = _proof_args(state_dir, execution_proof=False)
        with pytest.raises(_StopTheRun):
            cli.cmd_options_verify_execution(args, broker_factory=_ContextBroker)

        assert seen[0] == RiskPolicy.from_env()
        assert seen[0].max_defined_loss_per_position != PROOF_MAXIMUM_DEFINED_LOSS

    def test_the_proof_module_declares_no_way_to_send_an_order(self) -> None:
        """The package-wide AST sweep in ``test_options_no_transmit`` already
        covers this file. Asserted again here because a reader of the proof
        should not have to go and find that out."""
        import ast
        from pathlib import Path as _Path

        tree = ast.parse(
            _Path(proof_module.__file__).read_text(encoding="utf-8"),
            filename=proof_module.__file__,
        )
        offenders = [
            f"{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"placeOrder", "place", "transmit"}
        ]
        assert offenders == []
