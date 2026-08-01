"""Pulling and repricing a working order: the capability the first live send lacked.

The engine's first real order went ``Submitted`` and stayed there -- working,
unfilled, and unretractable. There was no ``cancelOrder`` anywhere in
``engine.src``, so a combo that neither filled nor rejected could not be pulled
programmatically; and reconciliation asked only ``broker.positions()``, so the
working order was invisible to it and got reported as one the broker was *not*
working. The first fact is what this file tests; the second lives in
``test_options_recovery.py``.

Four properties, and every one of them is mutation-checked -- the guard is
shown to refuse the bad input *and* to accept the good one, because a guard that
refuses everything passes half of these tests while being useless.

* **One cancel, behind one token.** ``cancel_combo`` is the only cancelling call
  in the package (pinned structurally in ``test_options_no_transmit.py``) and it
  requires a ``CancelAuthorization`` that only ``authorize_cancel`` can mint.
* **Cancelling is authorized more loosely than opening, deliberately.** No
  governor, no daily cap, no risk assessment -- the same asymmetry
  ``authorize_close`` has, for the same reason. The kill switch still applies.
* **A replace cannot leave the approved envelope, and cannot become a different
  order.** The price must sit inside the ``PriceEnvelope`` the risk gates ran
  against and on a valid tick; everything else is carried over from the
  structure that was authorized rather than supplied by the caller.
* **The ladder is bounded and ends flat.** At most four attempts, at most two
  minutes, and the last act is a cancellation rather than a resting order.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from engine.config import EngineConfig
from engine.errors import HaltedError, RefusedError
from engine.journal import OrderJournal
from engine.options.domain import (
    OptionLegIntent,
    OptionRight,
    OptionStrategyIntent,
    OrderAction,
    PriceEffect,
    StrategyAction,
    StrategyType,
)
from engine.options.governor import PortfolioGovernor
from engine.options.execution import MarginAssessment
from engine.options.orderstate import OrderLifecycleState
from engine.options.policy import RiskPolicy
from engine.options.portfolio import PortfolioSnapshot
from engine.options.positions import PositionStore
from engine.options.proof import PriceEnvelope, envelope_for
from engine.options.reprice import (
    MAXIMUM_ATTEMPTS,
    MAXIMUM_TIME_TO_LIVE,
    NOT_JOURNALLED,
    RECORDED_ELSEWHERE,
    RepriceLadder,
    RepriceStop,
    round_to_tick,
    tick_size,
    work_order,
)
from engine.options.risk import (
    REQUIRED_CHECKS,
    CandidateRiskAssessment,
    CheckResult,
)
from engine.options.sink import LifecycleRecorder, NullLifecycleSink
from engine.options.transmit import (
    CancelAuthorization,
    TransmitAuthorization,
    authorize_cancel,
    authorize_open,
    authorize_reprice,
    cancel_combo,
    place_combo,
    repricing_digest,
    structure_digest,
)
from engine.errors import ConfigError
from engine.safety import SafetyGate
from reviewer import packet, reviewed

D = Decimal
NOW = dt.datetime(2026, 7, 29, 13, 0, tzinfo=dt.timezone.utc)
EXPIRY = dt.date(2026, 9, 18)
WIDTH = D("5")
#: What the position store reserves for one lot of this spread.
BPR = D("500")


# ===========================================================================
# Builders
# ===========================================================================


def spread(
    *, underlying: str = "SPY", quantity: int = 1, credit: str = "1.50"
) -> OptionStrategyIntent:
    legs = (
        OptionLegIntent(
            con_id=1001,
            symbol=underlying,
            expiration=EXPIRY,
            strike=D("500"),
            right=OptionRight.PUT,
            action=OrderAction.SELL,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
        OptionLegIntent(
            con_id=1002,
            symbol=underlying,
            expiration=EXPIRY,
            strike=D("495"),
            right=OptionRight.PUT,
            action=OrderAction.BUY,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
    )
    return OptionStrategyIntent(
        strategy_id=uuid4(),
        strategy_type=StrategyType.PUT_CREDIT_SPREAD,
        strategy_action=StrategyAction.OPEN,
        underlying=underlying,
        quantity=quantity,
        legs=legs,
        expiration=EXPIRY,
        limit_price=D(credit),
        price_effect=PriceEffect.CREDIT,
        maximum_loss_per_contract=(WIDTH - D(credit)) * 100,
        configuration_version="test",
        created_at=NOW,
    )


def gate_for(tmp_path: Path, **overrides: Any) -> SafetyGate:
    settings: dict[str, Any] = {
        "account_id": "DU1234567",
        "port": 7497,
        "state_dir": tmp_path / "state",
        "symbol_allowlist": ("SPY", "AAPL"),
    }
    settings.update(overrides)
    config = EngineConfig(**settings)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return SafetyGate(config, OrderJournal(config.journal_path))


def approving_risk(strategy_id: UUID) -> CandidateRiskAssessment:
    return CandidateRiskAssessment(
        strategy_id=strategy_id,
        evaluated_at=NOW,
        policy_version="test",
        # Built from REQUIRED_CHECKS so a check added to the risk module is
        # automatically part of what this helper claims was approved.
        results=tuple(
            CheckResult(check=name, approved=True, detail="ok")
            for name in REQUIRED_CHECKS
        ),
    )


def approving_governor(intent: OptionStrategyIntent) -> Any:
    return PortfolioGovernor(RiskPolicy()).evaluate(
        intent,
        snapshot=PortfolioSnapshot(as_of=NOW, net_liquidation=D("1000000"), positions=()),
        margin=MarginAssessment(
            accepted=True,
            observed_at=NOW,
            initial_margin_change=D("500"),
            maintenance_margin_change=D("500"),
        ),
        decision_time=NOW,
    )


def review_root(gate: SafetyGate) -> Path:
    """A fresh collab for one review, beside the gate's own state.

    One per call rather than one per test: an approval is single-use per spec
    digest, and several of these tests mint two tokens for the same order at the
    same price -- which a shared ledger would refuse as already consumed.
    """
    return gate.config.state_dir.parent / "review" / uuid4().hex


def reviewing(gate: SafetyGate) -> tuple[Any, Any]:
    """``(verifier, context)`` -- what an *opening* reprice now requires.

    A reprice of an OPEN is a new opening order and gets its own review, because
    the protocol's invalidation rule names price. Closing reprices need neither
    and are not given one.
    """
    return reviewed(review_root(gate))


def authorized(gate: SafetyGate, intent: OptionStrategyIntent) -> TransmitAuthorization:
    """A real token, through the real gates. There is no other way to get one."""
    risk = approving_risk(intent.strategy_id)
    governor = approving_governor(intent)
    verifier, context = reviewing(gate)
    return authorize_open(
        intent,
        gate=gate,
        risk=risk,
        governor=governor,
        armed=True,
        now=NOW,
        verifier=verifier,
        packet=packet(intent, risk=risk, governor=governor, context=context, now=NOW),
    )


# ===========================================================================
# A stateful fake broker
# ===========================================================================


class _Order:
    def __init__(self, *, order_id: int, perm_id: int, ref: str, limit: float) -> None:
        self.orderId = order_id  # noqa: N815
        self.permId = perm_id  # noqa: N815
        self.orderRef = ref  # noqa: N815
        self.lmtPrice = limit  # noqa: N815


class _Status:
    def __init__(self, *, status: str, filled: float, remaining: float) -> None:
        self.status = status
        self.filled = filled
        self.remaining = remaining
        self.avgFillPrice = 0.0  # noqa: N815
        self.commission = None


class FakeTrade:
    """A working order that only changes when something acts on it.

    Deliberately *not* scripted by poll count: the point of these tests is that
    the ladder's cancel is what moves the order, so an order that resolved on
    its own timer would prove nothing about the cancel.
    """

    def __init__(self, *, order_id: int, perm_id: int, ref: str, limit: float) -> None:
        self.order = _Order(order_id=order_id, perm_id=perm_id, ref=ref, limit=limit)
        self.status = "Submitted"
        self.filled = 0.0
        self.remaining = 1.0
        self.done = False

    @property
    def orderStatus(self) -> _Status:  # noqa: N802
        return _Status(status=self.status, filled=self.filled, remaining=self.remaining)

    @property
    def log(self) -> list[Any]:
        return []

    def isDone(self) -> bool:  # noqa: N802
        return self.done


class LadderIB:
    """Records every send and every cancel, and lets a cancel be scripted.

    ``cancel_fills`` is how many lots the order turns out to have filled by the
    time the cancellation lands -- the race a real cancel can lose.
    """

    def __init__(
        self,
        *,
        cancel_fills: float = 0.0,
        cancel_error: str | None = None,
        cancel_status: str | None = None,
        fill_on_place: int | None = None,
    ) -> None:
        self.placed: list[Any] = []
        self.cancelled: list[Any] = []
        self.trades: list[FakeTrade] = []
        self.cancel_fills = cancel_fills
        self.cancel_error = cancel_error
        #: What the order reports after the cancel request is accepted. ``None``
        #: is the ordinary case: it dies. Anything else -- ``"PendingCancel"``,
        #: or the order simply carrying on as ``"Submitted"`` -- is the broker
        #: taking the request and leaving the order **working**, which is not a
        #: completed cancellation and must not license a replacement.
        self.cancel_status = cancel_status
        #: 1-based index of the placeOrder whose order fills immediately.
        self.fill_on_place = fill_on_place
        self.slept = 0.0

    def placeOrder(self, _contract: Any, order: Any) -> FakeTrade:  # noqa: N802
        self.placed.append(order)
        trade = FakeTrade(
            order_id=900 + len(self.trades),
            perm_id=1_151_642_162 + len(self.trades),
            ref=str(getattr(order, "orderRef", "")),
            limit=float(getattr(order, "lmtPrice", 0.0)),
        )
        if self.fill_on_place == len(self.placed):
            trade.status = "Filled"
            trade.filled = float(getattr(order, "totalQuantity", 1))
            trade.remaining = 0.0
            trade.orderStatus  # noqa: B018 - shape check only
            trade.done = True
        self.trades.append(trade)
        return trade

    def cancelOrder(self, order: Any) -> Any:  # noqa: N802
        self.cancelled.append(order)
        if self.cancel_error is not None:
            raise RuntimeError(self.cancel_error)
        for trade in self.trades:
            if trade.order is order:
                if self.cancel_status is not None:
                    # Request accepted, order still alive.
                    trade.status = self.cancel_status
                    return trade
                trade.status = "Cancelled"
                trade.filled = self.cancel_fills
                trade.remaining = max(0.0, 1.0 - self.cancel_fills)
                trade.done = True
                return trade
        return None

    def sleep(self, seconds: float) -> None:
        self.slept += seconds

    def isConnected(self) -> bool:  # noqa: N802
        return True


class Clock:
    """A clock that advances a fixed amount per read.

    Injected rather than patched so the two-minute deadline is a property of
    the test rather than of how fast the machine happens to be.
    """

    def __init__(self, *, step: dt.timedelta = dt.timedelta(seconds=1)) -> None:
        self.now = NOW
        self.step = step

    def __call__(self) -> dt.datetime:
        self.now = self.now + self.step
        return self.now


FAST = RepriceLadder(attempt_timeout=1.0, poll_seconds=0.5, cancel_timeout=1.0)


def work(
    ib: LadderIB,
    intent: OptionStrategyIntent,
    *,
    gate: SafetyGate,
    ladder: RepriceLadder = FAST,
    envelope: PriceEnvelope | None = None,
    clock: Clock | None = None,
    sink: Any = None,
    store: PositionStore | None = None,
    journal: Any = None,
    armed: bool = True,
) -> Any:
    """Send the entry through the real chokepoint, then work it.

    Wired the way ``run_once`` wires it -- store before the send, journal after
    -- so the helper cannot accidentally test a looser configuration than the
    engine actually runs.
    """
    authorization = authorized(gate, intent)
    sink = sink if sink is not None else NullLifecycleSink()
    result = place_combo(
        ib, intent, authorization=authorization, timeout=1.0, poll_seconds=0.5, sink=sink
    )
    if store is None:
        record = RECORDED_ELSEWHERE
    else:

        def record(replacement: OptionStrategyIntent) -> None:
            assert store is not None
            store.record_open_submitted(
                replacement, at=NOW, buying_power_reserved=BPR
            )

    if journal is None:
        journalled = NOT_JOURNALLED
    else:

        def journalled(sent: Any) -> None:
            journal.record("order_placed", **sent.to_record())

    # Every rung is an opening reprice, so the ladder needs a reviewer of its
    # own -- one review per price, which is what the invalidation rule demands.
    verifier, context = reviewing(gate)
    return work_order(
        ib,
        intent,
        result,
        authorization=authorization,
        gate=gate,
        armed=armed,
        started_at=NOW,
        clock=clock if clock is not None else Clock(),
        record_submission=record,
        record_transmission=journalled,
        ladder=ladder,
        envelope=envelope,
        sink=sink,
        verifier=verifier,
        approval_context=context,
    )


# ===========================================================================
# A. Tick increments
# ===========================================================================


class TestTickIncrements:
    @pytest.mark.parametrize("symbol", ["SPY", "QQQ", "IWM", "XSP", "spy"])
    @pytest.mark.parametrize("price", ["0.05", "2.99", "3.00", "12.50"])
    def test_the_all_price_penny_classes_are_a_penny_everywhere(
        self, symbol: str, price: str
    ) -> None:
        """Cboe Rule 5.4(a) lists QQQ, IWM and SPY (and XSP while SPY
        participates) at $0.01 for **all** prices, not just below $3.00. SPY is
        the only symbol this engine trades today, so getting this row wrong
        would round every reprice to a nickel."""
        assert tick_size(symbol, D(price)) == D("0.01")

    def test_an_unknown_class_falls_back_to_the_coarse_schedule(self) -> None:
        """Penny Program membership is rebalanced each January and April and
        published by OCC, which this process cannot read offline. The fallback
        is the non-penny row -- $0.05 below $3.00, $0.10 at or above -- because
        a coarser increment is an exact multiple of every finer one, so a price
        valid under it is valid under them too. Guessing the other way produces
        limits the exchange refuses."""
        assert tick_size("AAPL", D("2.99")) == D("0.05")
        assert tick_size("AAPL", D("3.00")) == D("0.10")
        assert tick_size("AAPL", D("7.40")) == D("0.10")

    def test_rounding_snaps_onto_the_grid_and_defaults_downward(self) -> None:
        """Down, because for a credit that means asking for less -- the
        direction that gets filled."""
        assert round_to_tick(D("1.487"), D("0.01")) == D("1.48")
        assert round_to_tick(D("1.487"), D("0.05")) == D("1.45")
        assert round_to_tick(D("1.487"), D("0.05"), up=True) == D("1.50")

    def test_a_non_positive_tick_is_refused_rather_than_dividing_by_zero(self) -> None:
        with pytest.raises(ConfigError):
            round_to_tick(D("1.50"), D("0"))


# ===========================================================================
# B. The cancel chokepoint
# ===========================================================================


class TestCancelChokepoint:
    def test_a_cancel_authorization_cannot_be_constructed_directly(self) -> None:
        """The same unforgeable construction ``TransmitAuthorization`` has. A
        test that could mint one would be testing a different program."""
        with pytest.raises(RefusedError, match="cannot be constructed directly"):
            CancelAuthorization(strategy_id=uuid4(), authorized_at=NOW, armed=True)

    def test_cancel_combo_refuses_an_opening_authorization(self, tmp_path: Path) -> None:
        """The two tokens grant different powers and are not interchangeable.

        Without this, "authorized to open" would silently be sufficient to
        cancel -- and, worse, the reverse question ("does a cancel token let me
        send?") would have no answer at all.
        """
        gate = gate_for(tmp_path)
        intent = spread()
        ib = LadderIB()
        result = place_combo(
            ib,
            intent,
            authorization=authorized(gate, intent),
            timeout=1.0,
            poll_seconds=0.5,
        )
        with pytest.raises(RefusedError, match="requires a CancelAuthorization"):
            cancel_combo(
                ib,
                result.trade,
                authorization=authorized(gate, intent),  # type: ignore[arg-type]
            )
        assert ib.cancelled == [], "the cancel reached the broker despite refusing"

    def test_place_combo_refuses_a_cancel_authorization(self, tmp_path: Path) -> None:
        """The inverse. A cheaply-obtained token must not be able to transmit."""
        gate = gate_for(tmp_path)
        intent = spread()
        ib = LadderIB()
        token = authorize_cancel(intent.strategy_id, gate=gate, armed=True, now=NOW)
        with pytest.raises(RefusedError, match="requires a TransmitAuthorization"):
            place_combo(ib, intent, authorization=token)  # type: ignore[arg-type]
        assert ib.placed == []

    def test_cancelling_needs_no_governor_no_risk_and_no_daily_cap(
        self, tmp_path: Path
    ) -> None:
        """The asymmetry, stated as a signature.

        ``authorize_cancel`` takes a strategy id, a gate, ``armed`` and a clock.
        There is nowhere to pass a risk assessment or a governor verdict, so
        "the book is too concentrated to let you cancel" is not a sentence this
        program can express. The daily cap is untouched too: cancelling is not
        an order the cap exists to count.
        """
        gate = gate_for(tmp_path, max_orders_per_session=1)
        gate.journal.record("order_placed", symbol="SPY")

        # The cap is genuinely exhausted -- an *open* would now refuse.
        with pytest.raises(RefusedError):
            gate.gate_daily_count()

        token = authorize_cancel(uuid4(), gate=gate, armed=True, now=NOW)
        assert isinstance(token, CancelAuthorization)

    def test_the_kill_switch_blocks_a_cancel(self, tmp_path: Path) -> None:
        """The one thing that stops a cancel, and it stops everything else too.

        ``HALT`` means the operator said stop. An engine that keeps touching the
        broker after a halt is not halted, and "but it was only reducing risk"
        is exactly the reasoning that makes a kill switch worthless.
        """
        gate = gate_for(tmp_path)
        gate.config.state_dir.mkdir(parents=True, exist_ok=True)
        (gate.config.state_dir / "HALT").write_text("stop", encoding="utf-8")

        with pytest.raises(HaltedError):
            authorize_cancel(uuid4(), gate=gate, armed=True, now=NOW)

    def test_an_unarmed_run_cannot_cancel(self, tmp_path: Path) -> None:
        """A dry run must not reach out and retract a real order."""
        gate = gate_for(tmp_path)
        with pytest.raises(RefusedError):
            authorize_cancel(uuid4(), gate=gate, armed=False, now=NOW)

    def test_the_kill_switch_stops_the_ladder_before_it_touches_the_broker(
        self, tmp_path: Path
    ) -> None:
        """The guard, in the place it actually has to hold.

        Mutation-checked against the very next test: same fake, same ladder, one
        file on disk different, and the cancel either does or does not reach the
        broker.
        """
        gate = gate_for(tmp_path)
        intent = spread()
        ib = LadderIB()
        authorization = authorized(gate, intent)
        result = place_combo(
            ib, intent, authorization=authorization, timeout=1.0, poll_seconds=0.5
        )

        (gate.config.state_dir / "HALT").write_text("stop", encoding="utf-8")

        verifier, context = reviewing(gate)
        outcome = work_order(
            ib,
            intent,
            result,
            authorization=authorization,
            gate=gate,
            armed=True,
            started_at=NOW,
            clock=Clock(),
            record_submission=RECORDED_ELSEWHERE,
            record_transmission=NOT_JOURNALLED,
            ladder=FAST,
            verifier=verifier,
            approval_context=context,
        )

        assert ib.cancelled == []
        assert len(ib.placed) == 1
        assert outcome.stop is RepriceStop.REFUSED
        assert "cancel refused" in outcome.detail

    def test_without_the_halt_the_same_ladder_does_cancel(self, tmp_path: Path) -> None:
        """The mutation half of the test above."""
        gate = gate_for(tmp_path)
        ib = LadderIB()
        outcome = work(ib, spread(), gate=gate)

        assert ib.cancelled, "the ladder never cancelled anything"
        assert outcome.cancelled is True
        assert outcome.stop is RepriceStop.EXHAUSTED


# ===========================================================================
# C. Repricing inside the envelope
# ===========================================================================


class TestRepriceStaysInsideTheEnvelope:
    def test_a_replace_inside_the_envelope_is_authorized(self, tmp_path: Path) -> None:
        gate = gate_for(tmp_path)
        intent = spread(credit="1.50")
        envelope = envelope_for(intent)
        assert envelope.minimum == D("1.35")

        verifier, context = reviewing(gate)
        repriced = authorize_reprice(
            authorized(gate, intent),
            intent,
            limit_price=D("1.40"),
            envelope=envelope,
            tick=D("0.01"),
            gate=gate,
            armed=True,
            now=NOW,
            verifier=verifier,
            context=context,
        )

        assert repriced.intent.limit_price == D("1.40")
        assert repriced.previous_price == D("1.50")
        # The maximum loss is re-derived, not carried over. On a credit spread
        # it is a function of the credit, and a repriced order carrying the old
        # figure would misreport its own risk to everything downstream.
        assert repriced.intent.maximum_loss_per_contract == (WIDTH - D("1.40")) * 100

    def test_a_replace_outside_the_envelope_is_refused(self, tmp_path: Path) -> None:
        """One cent past the floor. The mutation partner of the test above:
        1.40 is accepted and 1.34 is not, so the guard is a bound rather than a
        blanket refusal."""
        gate = gate_for(tmp_path)
        intent = spread(credit="1.50")
        envelope = envelope_for(intent)

        verifier, context = reviewing(gate)
        with pytest.raises(RefusedError, match="outside the approved envelope"):
            authorize_reprice(
                authorized(gate, intent),
                intent,
                limit_price=D("1.34"),
                envelope=envelope,
                tick=D("0.01"),
                gate=gate,
                armed=True,
                now=NOW,
                verifier=verifier,
                context=context,
            )

    def test_a_replace_above_the_envelope_ceiling_is_refused_too(
        self, tmp_path: Path
    ) -> None:
        """Bounded on both sides. A book that moved in our favour is still a
        book that moved, and every risk figure was computed against the old
        credit."""
        gate = gate_for(tmp_path)
        intent = spread(credit="1.50")

        verifier, context = reviewing(gate)
        with pytest.raises(RefusedError, match="outside the approved envelope"):
            authorize_reprice(
                authorized(gate, intent),
                intent,
                limit_price=D("1.70"),
                envelope=envelope_for(intent),
                tick=D("0.01"),
                gate=gate,
                armed=True,
                now=NOW,
                verifier=verifier,
                context=context,
            )

    def test_an_off_tick_replace_is_refused(self, tmp_path: Path) -> None:
        """A sub-tick limit is rejected or silently rounded by the exchange, and
        a silently rounded limit is an order at a price we did not choose.

        Mutation-checked on the nickel grid: 1.45 passes where 1.47 does not,
        so this is the grid being enforced rather than the value.
        """
        gate = gate_for(tmp_path)
        intent = spread(credit="1.50")
        envelope = envelope_for(intent)

        off_grid, off_grid_context = reviewing(gate)
        with pytest.raises(RefusedError, match="tick increment"):
            authorize_reprice(
                authorized(gate, intent),
                intent,
                limit_price=D("1.47"),
                envelope=envelope,
                tick=D("0.05"),
                gate=gate,
                armed=True,
                now=NOW,
                verifier=off_grid,
                context=off_grid_context,
            )

        verifier, context = reviewing(gate)
        assert (
            authorize_reprice(
                authorized(gate, intent),
                intent,
                limit_price=D("1.45"),
                envelope=envelope,
                tick=D("0.05"),
                gate=gate,
                armed=True,
                now=NOW,
                verifier=verifier,
                context=context,
            ).intent.limit_price
            == D("1.45")
        )

    def test_a_replace_that_changes_nothing_is_refused(self, tmp_path: Path) -> None:
        gate = gate_for(tmp_path)
        intent = spread(credit="1.50")
        verifier, context = reviewing(gate)
        with pytest.raises(RefusedError, match="does not change"):
            authorize_reprice(
                authorized(gate, intent),
                intent,
                limit_price=D("1.50"),
                envelope=envelope_for(intent),
                tick=D("0.01"),
                gate=gate,
                armed=True,
                now=NOW,
                verifier=verifier,
                context=context,
            )

    def test_the_kill_switch_blocks_a_replace(self, tmp_path: Path) -> None:
        gate = gate_for(tmp_path)
        intent = spread(credit="1.50")
        token = authorized(gate, intent)
        verifier, context = reviewing(gate)
        (gate.config.state_dir / "HALT").write_text("stop", encoding="utf-8")

        with pytest.raises(HaltedError):
            authorize_reprice(
                token,
                intent,
                limit_price=D("1.40"),
                envelope=envelope_for(intent),
                tick=D("0.01"),
                gate=gate,
                armed=True,
                now=NOW,
                verifier=verifier,
                context=context,
            )


# ===========================================================================
# D. A replace cannot become a different order
# ===========================================================================


class TestRepriceCannotBecomeADifferentOrder:
    def test_a_replace_whose_digest_no_longer_matches_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The binding at ``transmit.py``'s digest guard, re-checked one layer up.

        The authorization was minted for a 1-lot. Handing the repricer a 10-lot
        that shares the strategy id is exactly the "an approval for a 1-lot
        authorizes a 50-lot" failure the digest exists to catch, arriving down
        the reprice path instead of the send path.
        """
        gate = gate_for(tmp_path)
        approved = spread(quantity=1, credit="1.50")
        token = authorized(gate, approved)

        bigger = OptionStrategyIntent(
            strategy_id=approved.strategy_id,
            strategy_type=approved.strategy_type,
            strategy_action=approved.strategy_action,
            underlying=approved.underlying,
            quantity=10,
            legs=approved.legs,
            expiration=approved.expiration,
            limit_price=approved.limit_price,
            price_effect=approved.price_effect,
            maximum_loss_per_contract=approved.maximum_loss_per_contract,
            configuration_version=approved.configuration_version,
            created_at=approved.created_at,
        )
        assert bigger.strategy_id == approved.strategy_id
        assert structure_digest(bigger) != token.digest

        verifier, context = reviewing(gate)
        with pytest.raises(RefusedError, match="not the structure that was authorized"):
            authorize_reprice(
                token,
                bigger,
                limit_price=D("1.40"),
                envelope=envelope_for(approved),
                tick=D("0.01"),
                gate=gate,
                armed=True,
                now=NOW,
                verifier=verifier,
                context=context,
            )

    def test_the_same_call_succeeds_on_the_structure_that_was_authorized(
        self, tmp_path: Path
    ) -> None:
        """The mutation half. Identical call, correct reference."""
        gate = gate_for(tmp_path)
        approved = spread(quantity=1, credit="1.50")
        verifier, context = reviewing(gate)
        repriced = authorize_reprice(
            authorized(gate, approved),
            approved,
            limit_price=D("1.40"),
            envelope=envelope_for(approved),
            tick=D("0.01"),
            gate=gate,
            armed=True,
            now=NOW,
            verifier=verifier,
            context=context,
        )
        assert repriced.intent.quantity == 1

    def test_an_authorization_for_another_strategy_cannot_reprice_this_one(
        self, tmp_path: Path
    ) -> None:
        gate = gate_for(tmp_path)
        mine, other = spread(), spread()
        verifier, context = reviewing(gate)
        with pytest.raises(RefusedError, match="authorization is for strategy"):
            authorize_reprice(
                authorized(gate, other),
                mine,
                limit_price=D("1.40"),
                envelope=envelope_for(mine),
                tick=D("0.01"),
                gate=gate,
                armed=True,
                now=NOW,
                verifier=verifier,
                context=context,
            )

    def test_the_repriced_token_still_binds_the_send(self, tmp_path: Path) -> None:
        """The reprice authorization is not a looser token.

        It carries a full ``structure_digest`` of the repriced order, so
        ``place_combo`` still refuses to send anything else under it -- the
        reprice path does not create a token that authorizes a family of orders.
        """
        gate = gate_for(tmp_path)
        intent = spread(credit="1.50")
        verifier, context = reviewing(gate)
        repriced = authorize_reprice(
            authorized(gate, intent),
            intent,
            limit_price=D("1.40"),
            envelope=envelope_for(intent),
            tick=D("0.01"),
            gate=gate,
            armed=True,
            now=NOW,
            verifier=verifier,
            context=context,
        )
        ib = LadderIB()

        # The order it was minted for goes out.
        sent = place_combo(
            ib,
            repriced.intent,
            authorization=repriced.authorization,
            timeout=1.0,
            poll_seconds=0.5,
        )
        assert sent.transmitted is True

        # A different price under the same token does not.
        with pytest.raises(RefusedError, match="does not match the structure"):
            place_combo(ib, intent, authorization=repriced.authorization)
        assert len(ib.placed) == 1

    def test_the_repricing_digest_ignores_only_the_price_and_its_derived_loss(
        self,
    ) -> None:
        """What ``repricing_digest`` is allowed to be blind to, stated directly."""
        base = spread(credit="1.50")
        moved = OptionStrategyIntent(
            strategy_id=base.strategy_id,
            strategy_type=base.strategy_type,
            strategy_action=base.strategy_action,
            underlying=base.underlying,
            quantity=base.quantity,
            legs=base.legs,
            expiration=base.expiration,
            limit_price=D("1.40"),
            price_effect=base.price_effect,
            maximum_loss_per_contract=(WIDTH - D("1.40")) * 100,
            configuration_version=base.configuration_version,
            created_at=base.created_at,
        )
        assert repricing_digest(moved) == repricing_digest(base)
        assert structure_digest(moved) != structure_digest(base)

        resized = OptionStrategyIntent(
            strategy_id=base.strategy_id,
            strategy_type=base.strategy_type,
            strategy_action=base.strategy_action,
            underlying=base.underlying,
            quantity=3,
            legs=base.legs,
            expiration=base.expiration,
            limit_price=base.limit_price,
            price_effect=base.price_effect,
            maximum_loss_per_contract=base.maximum_loss_per_contract,
            configuration_version=base.configuration_version,
            created_at=base.created_at,
        )
        assert repricing_digest(resized) != repricing_digest(base)


# ===========================================================================
# E. The ladder: bounded, and it ends flat
# ===========================================================================


class TestTheLadderIsBounded:
    def test_a_ladder_cannot_be_configured_past_the_module_ceilings(self) -> None:
        """The bound is structural, in the style of ``ExecutionProofProfile``:
        the worst case of a ladder is knowable by reading one file."""
        assert MAXIMUM_ATTEMPTS == 4
        assert MAXIMUM_TIME_TO_LIVE == dt.timedelta(minutes=2)

        with pytest.raises(ConfigError, match="exceeds the ceiling"):
            RepriceLadder(maximum_attempts=5)
        with pytest.raises(ConfigError, match="exceeds the ceiling"):
            RepriceLadder(time_to_live=dt.timedelta(minutes=3))
        with pytest.raises(ConfigError):
            RepriceLadder(maximum_attempts=0)

    def test_four_attempts_then_a_cancel(self, tmp_path: Path) -> None:
        """The headline bound. Four replaces, and the fifth act is a cancel.

        Five ``placeOrder`` calls in total -- the original plus four rungs --
        and five cancels, because every rung cancels the order it replaces and
        the ladder cancels once more on the way out. Nothing is left working.
        """
        gate = gate_for(tmp_path)
        ib = LadderIB()
        outcome = work(ib, spread(credit="1.50"), gate=gate)

        assert outcome.attempts == 4
        assert outcome.stop is RepriceStop.EXHAUSTED
        assert outcome.cancelled is True
        assert "all 4 attempts were used" in outcome.detail
        assert len(ib.placed) == 5
        assert len(ib.cancelled) == 5
        assert outcome.state is OrderLifecycleState.CANCELLED

    def test_every_rung_lowers_the_credit_and_stays_inside_the_envelope(
        self, tmp_path: Path
    ) -> None:
        """A ladder that walked *up* would be asking for more from a book that
        already refused less, and one that walked outside the envelope would be
        sending an order whose arithmetic nobody checked."""
        gate = gate_for(tmp_path)
        intent = spread(credit="1.50")
        envelope = envelope_for(intent)
        ib = LadderIB()

        outcome = work(ib, intent, gate=gate)

        assert list(outcome.prices) == sorted(outcome.prices, reverse=True)
        assert outcome.prices[0] < intent.limit_price
        for price in outcome.prices:
            assert envelope.contains(price), f"{price} left {envelope.describe()}"
        # And the broker saw exactly those prices, as negative limits -- the
        # credit convention build_combo encodes.
        assert [D(str(-order.lmtPrice)) for order in ib.placed[1:]] == list(outcome.prices)

    def test_the_deadline_cancels_before_the_attempts_are_used(
        self, tmp_path: Path
    ) -> None:
        """Two minutes, whichever comes first. The clock jumps a full minute per
        read, so the deadline binds on the second rung rather than the fourth."""
        gate = gate_for(tmp_path)
        ib = LadderIB()

        outcome = work(
            ib, spread(credit="1.50"), gate=gate, clock=Clock(step=dt.timedelta(minutes=1))
        )

        assert outcome.attempts < MAXIMUM_ATTEMPTS
        assert outcome.stop is RepriceStop.EXHAUSTED
        assert "deadline expired" in outcome.detail
        assert outcome.cancelled is True

    def test_the_ladder_cancels_rather_than_step_below_the_envelope_floor(
        self, tmp_path: Path
    ) -> None:
        """A band with room for two rungs, and a ladder budgeted for four.

        The bound that binds first wins, and the ladder ends by cancelling
        rather than by sending the rung that would have left the band.
        """
        gate = gate_for(tmp_path)
        intent = spread(credit="1.50")
        narrow = PriceEnvelope(
            reference=D("1.50"), minimum=D("1.48"), maximum=D("1.65"), width=WIDTH
        )
        ib = LadderIB()

        outcome = work(ib, intent, gate=gate, envelope=narrow)

        assert list(outcome.prices) == [D("1.49"), D("1.48")]
        assert outcome.attempts == 2
        assert outcome.stop is RepriceStop.ENVELOPE
        assert outcome.cancelled is True
        assert "below the envelope floor" in outcome.detail

    def test_an_order_that_fills_on_its_own_is_never_worked(
        self, tmp_path: Path
    ) -> None:
        """The control. Without it, every assertion above could be produced by
        the ladder running unconditionally rather than by the order resting."""
        gate = gate_for(tmp_path)
        ib = LadderIB(fill_on_place=1)
        outcome = work(ib, spread(credit="1.50"), gate=gate)

        assert outcome.stop is RepriceStop.RESOLVED
        assert outcome.attempts == 0
        assert ib.cancelled == []
        assert len(ib.placed) == 1

    def test_a_fill_that_beats_the_cancel_stops_the_ladder(
        self, tmp_path: Path
    ) -> None:
        """A cancel races a fill and can lose. Replacing an order that filled
        while being cancelled is how one intended position becomes two."""
        gate = gate_for(tmp_path)
        ib = LadderIB(cancel_fills=1.0)
        outcome = work(ib, spread(credit="1.50"), gate=gate)

        assert outcome.stop is RepriceStop.FILLED_DURING_CANCEL
        assert outcome.has_position is True
        assert len(ib.placed) == 1, "a replacement was sent on top of a filled order"
        assert len(ib.cancelled) == 1

    def test_an_order_that_already_filled_some_is_never_worked(
        self, tmp_path: Path
    ) -> None:
        """A partial fill is ``is_working``, and that is not enough.

        Contracts are already in the book. Cancelling the remainder and
        re-sending at a lower credit would open a second position on top of a
        real one, at a price the filled half was never sized against -- so the
        ladder leaves it entirely alone and the position goes to management.
        """
        from engine.options.orderstate import snapshot_from_trade
        from engine.options.transmit import TransmitResult

        gate = gate_for(tmp_path)
        intent = spread(quantity=3, credit="1.50")
        authorization = authorized(gate, intent)
        ib = LadderIB()
        sent = place_combo(
            ib, intent, authorization=authorization, timeout=1.0, poll_seconds=0.5
        )

        # One lot of three arrives, which is an ordinary thing to observe
        # moments after a combo goes out.
        ib.trades[0].filled = 1.0
        ib.trades[0].remaining = 2.0
        partial = TransmitResult(
            strategy_id=intent.strategy_id,
            action=intent.strategy_action,
            transmitted=True,
            snapshot=snapshot_from_trade(
                sent.trade, observed_at=NOW, quantity=intent.quantity
            ),
            trade=sent.trade,
        )
        assert partial.state is OrderLifecycleState.PARTIALLY_FILLED
        assert partial.snapshot is not None and partial.snapshot.is_working is True

        verifier, context = reviewing(gate)
        outcome = work_order(
            ib,
            intent,
            partial,
            authorization=authorization,
            gate=gate,
            armed=True,
            started_at=NOW,
            clock=Clock(),
            record_submission=RECORDED_ELSEWHERE,
            record_transmission=NOT_JOURNALLED,
            ladder=FAST,
            verifier=verifier,
            approval_context=context,
        )

        assert outcome.stop is RepriceStop.RESOLVED
        assert outcome.attempts == 0
        assert ib.cancelled == []
        assert len(ib.placed) == 1

    @pytest.mark.parametrize("still_alive", ["PendingCancel", "Submitted"])
    def test_nothing_is_sent_until_the_cancel_is_confirmed_terminal(
        self, tmp_path: Path, still_alive: str
    ) -> None:
        """The duplicate-order defect, pinned.

        The broker accepts the cancel request and the order keeps working --
        ``PendingCancel`` is a working state, and an order that simply carries
        on reporting ``Submitted`` is the timeout case. Neither is a dead order.

        Before this guard the ladder read "the cancel call returned" as "the
        order is gone" and went straight on to ``place_combo``, producing the
        broker sequence ``cancelOrder, placeOrder, cancelOrder`` against an
        order still reporting ``Submitted``: **two live orders for one approved
        structure**, created by the code written to prevent duplicates.

        The assertion that matters is ``len(ib.placed) == 1``.
        """
        gate = gate_for(tmp_path)
        ib = LadderIB(cancel_status=still_alive)

        outcome = work(ib, spread(credit="1.50"), gate=gate)

        assert len(ib.placed) == 1, (
            "a replacement was transmitted while the original may still be working"
        )
        assert len(ib.cancelled) == 1, "the ladder kept cancelling after a failure"
        assert outcome.stop is RepriceStop.REFUSED
        assert outcome.cancelled is False
        assert "did not complete" in outcome.detail
        assert outcome.attempts == 0

    def test_the_same_ladder_proceeds_once_the_cancel_does_complete(
        self, tmp_path: Path
    ) -> None:
        """The mutation half of the test above.

        Identical fake and identical ladder; the only difference is that the
        cancellation reaches a terminal state. Without this, the guard above
        would read as "the ladder never replaces anything".
        """
        gate = gate_for(tmp_path)
        ib = LadderIB(cancel_status=None)

        outcome = work(ib, spread(credit="1.50"), gate=gate)

        assert len(ib.placed) == 5
        assert outcome.attempts == 4
        assert outcome.cancelled is True

    def test_the_final_cancel_must_also_confirm(self, tmp_path: Path) -> None:
        """The exhaustion path gets the same treatment.

        A ladder that reported ``EXHAUSTED  CANCELLED`` on an unconfirmed
        cancellation would tell the operator the order had been pulled when it
        may still be resting -- which is the same false claim, one layer up,
        that this lane removed from the reconciler.
        """
        gate = gate_for(tmp_path)
        ib = LadderIB()
        # One rung's cancel succeeds; the ladder is then budgeted to a single
        # attempt, so the next cancel is the closing one -- and it does not
        # complete.
        ladder = RepriceLadder(
            maximum_attempts=1, attempt_timeout=1.0, poll_seconds=0.5, cancel_timeout=1.0
        )

        outcome = work(ib, spread(credit="1.50"), gate=gate, ladder=ladder)
        assert outcome.attempts == 1 and outcome.cancelled is True

        ib2 = LadderIB(cancel_status="PendingCancel")
        outcome2 = work(ib2, spread(credit="1.50"), gate=gate, ladder=ladder)

        assert outcome2.stop is RepriceStop.REFUSED
        assert outcome2.cancelled is False
        assert "may still be working" in outcome2.detail

    @pytest.mark.parametrize(
        ("state", "filled", "worked"),
        [
            (OrderLifecycleState.ACKNOWLEDGED, "0", True),
            (OrderLifecycleState.SUBMITTED, "0", True),
            # Working, but contracts are already in the book.
            (OrderLifecycleState.PARTIALLY_FILLED, "1", False),
            (OrderLifecycleState.ACKNOWLEDGED, "1", False),
            # Not statements about the order: we stopped waiting, or the socket
            # dropped. Reaching for a broker we cannot hear from is guessing.
            (OrderLifecycleState.TIMED_OUT, "0", False),
            (OrderLifecycleState.UNKNOWN, "0", False),
            (OrderLifecycleState.FILLED, "1", False),
            (OrderLifecycleState.CANCELLED, "0", False),
            (OrderLifecycleState.REJECTED, "0", False),
        ],
    )
    def test_which_states_the_runner_will_hand_to_the_ladder_at_all(
        self, state: OrderLifecycleState, filled: str, worked: bool
    ) -> None:
        """The runner's own gate, tested apart from the ladder's.

        ``work_order`` refuses a partially filled order too, so a behavioural
        test cannot tell which of the two guards did the refusing -- and the
        runner-side one could be deleted with a green suite. Enumerated here
        directly, all nine states, so both layers are pinned independently.
        """
        from engine.options.orderstate import BrokerOrderSnapshot
        from engine.options.runner import _still_working
        from engine.options.transmit import TransmitResult

        result = TransmitResult(
            strategy_id=uuid4(),
            action=StrategyAction.OPEN,
            transmitted=True,
            snapshot=BrokerOrderSnapshot(
                state=state, observed_at=NOW, order_id=900, filled=D(filled)
            ),
            trade=object(),
        )
        assert _still_working(result) is worked

    def test_the_runner_will_not_work_an_order_with_no_broker_handle(self) -> None:
        """A result carrying no ``trade`` has nothing to cancel, so the ladder
        would refuse on its first line. Refused one layer earlier instead."""
        from engine.options.orderstate import BrokerOrderSnapshot
        from engine.options.runner import _still_working
        from engine.options.transmit import TransmitResult

        assert (
            _still_working(
                TransmitResult(
                    strategy_id=uuid4(),
                    action=StrategyAction.OPEN,
                    transmitted=True,
                    snapshot=BrokerOrderSnapshot(
                        state=OrderLifecycleState.ACKNOWLEDGED,
                        observed_at=NOW,
                        order_id=900,
                    ),
                    trade=None,
                )
            )
            is False
        )

    def test_a_broker_that_refuses_the_cancel_stops_the_ladder(
        self, tmp_path: Path
    ) -> None:
        """A ladder that could not cancel must not carry on placing. Reported,
        not retried."""
        gate = gate_for(tmp_path)
        ib = LadderIB(cancel_error="10148: cannot cancel a filled order")
        outcome = work(ib, spread(credit="1.50"), gate=gate)

        assert outcome.stop is RepriceStop.REFUSED
        assert "could not be sent" in outcome.detail
        assert len(ib.placed) == 1


# ===========================================================================
# F. Persistence: observed, not summarised
# ===========================================================================


class TestEveryStateChangePersistsAsObserved:
    def test_every_order_the_ladder_sends_reaches_disk_as_it_happens(
        self, tmp_path: Path
    ) -> None:
        """Not from a final snapshot.

        Five orders go out and five are recorded, each submitted before it is
        sent and each acknowledged as the broker answers. A ladder that
        persisted only its last order would leave four sends with no durable
        record, and a crash mid-ladder would then leave a live order nothing
        knows about -- the exact failure both the store's write ordering and the
        lifecycle sink exist to prevent.
        """
        gate = gate_for(tmp_path)
        store = PositionStore(tmp_path / "state" / "positions.jsonl")
        intent = spread(credit="1.50")
        store.record_open_submitted(intent, at=NOW, buying_power_reserved=D("500"))
        recorder = LifecycleRecorder(store)
        ib = LadderIB()

        outcome = work(ib, intent, gate=gate, sink=recorder, store=store)

        events = [str(line.get("event")) for line in store.events()]
        assert outcome.attempts == 4
        assert len(ib.placed) == 5
        # One submission per order, and one acknowledgement per order: the
        # identifiers of every send are durable, not just the last one's.
        assert events.count("OPEN_SUBMITTED") == 5, events
        assert events.count("OPEN_ACKNOWLEDGED") == 5, events
        # Five cancels, none of which filled, so five honest OPEN_FAILEDs.
        assert events.count("OPEN_FAILED") == 5, events

        # Each acknowledgement carries a *different* permId, in send order.
        acked = [
            line.get("perm_id")
            for line in store.events()
            if line.get("event") == "OPEN_ACKNOWLEDGED"
        ]
        assert acked == [trade.order.permId for trade in ib.trades], acked

        # And the book replays cleanly: a cancel/replace ladder must not leave
        # a log the reconciler will call CORRUPT.
        assert store.integrity_errors() == ()
        assert store.get(intent.strategy_id) is None, (
            "nothing filled, so nothing should be left open"
        )

    def test_the_buying_power_reservation_is_restored_before_each_replacement(
        self, tmp_path: Path
    ) -> None:
        """The reservation the cancel released must be back before the resend.

        A confirmed cancel drives the sink to ``record_open_failed``, whose
        replay drops the position **and its buying-power reservation**. If the
        replacement then goes out without a fresh ``OPEN_SUBMITTED``, the
        governor sizes its next decision against a book that believes that
        capital is free while a real order rests in the market -- which is how
        a bounded account ends up over-committed by exactly the amount it just
        re-sent.

        Measured at the only moment that matters: immediately before each send.
        """
        gate = gate_for(tmp_path)
        store = PositionStore(tmp_path / "state" / "positions.jsonl")
        intent = spread(credit="1.50")
        store.record_open_submitted(intent, at=NOW, buying_power_reserved=BPR)
        recorder = LifecycleRecorder(store)
        ib = LadderIB()

        reserved_at_send: list[Any] = []
        released: list[Any] = []

        def record(replacement: OptionStrategyIntent) -> None:
            # What the book looked like *after* the cancel and *before* this
            # record -- the window the defect lived in.
            before = store.get(replacement.strategy_id)
            released.append(before.buying_power_reserved if before else None)
            store.record_open_submitted(
                replacement, at=NOW, buying_power_reserved=BPR
            )
            after = store.get(replacement.strategy_id)
            reserved_at_send.append(after.buying_power_reserved if after else None)

        verifier, context = reviewing(gate)
        outcome = work_order(
            ib,
            intent,
            place_combo(
                ib,
                intent,
                authorization=authorized(gate, intent),
                timeout=1.0,
                poll_seconds=0.5,
                sink=recorder,
            ),
            authorization=authorized(gate, intent),
            gate=gate,
            armed=True,
            started_at=NOW,
            clock=Clock(),
            record_submission=record,
            record_transmission=NOT_JOURNALLED,
            ladder=FAST,
            sink=recorder,
            verifier=verifier,
            approval_context=context,
        )

        assert outcome.attempts == 4
        # The cancel really did release it -- otherwise this test would pass
        # without the restoration doing anything.
        assert released == [None, None, None, None], released
        # And every send went out against a book that had it reserved again.
        assert reserved_at_send == [BPR] * 4, reserved_at_send

    def test_every_transmission_is_journalled_so_the_order_cap_can_see_it(
        self, tmp_path: Path
    ) -> None:
        """``gate_daily_count`` counts ``order_placed`` records.

        An order that never reaches the journal is an order the session cap
        cannot see, and four invisible orders per ladder is a cap that does not
        bind. A ladder is one logical *entry*; it is up to five real
        transmissions, and the cap counts transmissions.
        """
        gate = gate_for(tmp_path)
        ib = LadderIB()
        intent = spread(credit="1.50")

        # The first send is the runner's responsibility, exactly as in run_once.
        before = gate.journal.orders_today()
        outcome = work(ib, intent, gate=gate, journal=gate.journal)

        assert outcome.attempts == 4
        assert len(ib.placed) == 5
        # Four replacements, four new journal entries. The first send is not
        # counted here because this helper -- like run_once -- journals it
        # outside the ladder.
        assert gate.journal.orders_today() == before + 4

    def test_a_ladder_can_exhaust_the_session_order_cap(self, tmp_path: Path) -> None:
        """The consequence, stated rather than discovered later.

        With the default cap of five orders per session, one fully-worked entry
        is a session's budget -- because five orders really did go to the
        broker. The next entry is refused by the ordinary gate, with the
        ordinary message.
        """
        gate = gate_for(tmp_path, max_orders_per_session=5)
        gate.journal.record("order_placed", symbol="SPY")
        ib = LadderIB()

        work(ib, spread(credit="1.50"), gate=gate, journal=gate.journal)

        assert gate.journal.orders_today() == 5
        with pytest.raises(RefusedError, match="at the cap of 5"):
            gate.gate_daily_count()

    def test_persistence_is_ordered_before_the_send(self, tmp_path: Path) -> None:
        """A replacement that cannot be recorded is never placed.

        Same contract ``run_once`` honours for the first order. The mutation is
        the recorder itself: it raises on the first replacement, and the count
        of ``placeOrder`` calls is what proves the ordering.
        """
        gate = gate_for(tmp_path)
        intent = spread(credit="1.50")
        authorization = authorized(gate, intent)
        ib = LadderIB()
        result = place_combo(
            ib, intent, authorization=authorization, timeout=1.0, poll_seconds=0.5
        )

        def refuse(_replacement: OptionStrategyIntent) -> None:
            raise RuntimeError("the position store is unwritable")

        verifier, context = reviewing(gate)
        with pytest.raises(RuntimeError, match="unwritable"):
            work_order(
                ib,
                intent,
                result,
                authorization=authorization,
                gate=gate,
                armed=True,
                started_at=NOW,
                clock=Clock(),
                record_submission=refuse,
                record_transmission=NOT_JOURNALLED,
                ladder=FAST,
                verifier=verifier,
                approval_context=context,
            )

        assert len(ib.placed) == 1, "a replacement was sent without being recorded"
        assert len(ib.cancelled) == 1, "the rung's cancel should still have happened"

    def test_a_replacements_first_callback_is_not_read_as_a_stale_one(
        self, tmp_path: Path
    ) -> None:
        """The staleness guard is about one order, not about one strategy.

        A replacement's first ``Submitted`` ranks *below* the cancellation that
        retired the order it replaces, so ranking the two against each other
        classified it as old news -- and the new order's identifiers then did
        not reach disk until something later happened to carry them. A crash in
        that window left a live order at the broker the store could not name.

        Asserted as a property of the sink itself, with two hand-built
        observations, so it holds regardless of what the ladder does.
        """
        from engine.options.orderstate import BrokerOrderSnapshot

        store = PositionStore(tmp_path / "state" / "positions.jsonl")
        intent = spread(credit="1.50")
        store.record_open_submitted(intent, at=NOW, buying_power_reserved=D("500"))
        recorder = LifecycleRecorder(store)

        cancelled = BrokerOrderSnapshot(
            state=OrderLifecycleState.CANCELLED,
            observed_at=NOW,
            order_id=900,
            perm_id=1_151_642_162,
        )
        replacement = BrokerOrderSnapshot(
            state=OrderLifecycleState.ACKNOWLEDGED,
            observed_at=NOW,
            order_id=901,
            perm_id=1_151_642_163,
        )

        assert recorder.observe(intent.strategy_id, cancelled) is True
        assert recorder.observe(intent.strategy_id, replacement) is True, (
            "the replacement's identifiers never reached disk"
        )

        acked = [
            line.get("perm_id")
            for line in store.events()
            if line.get("event") == "OPEN_ACKNOWLEDGED"
        ]
        assert acked == [1_151_642_162, 1_151_642_163], acked

    def test_a_re_delivered_callback_for_the_same_order_is_still_stale(
        self, tmp_path: Path
    ) -> None:
        """The mutation half. Same identifiers, lower rank: still old news.

        Without this, the fix above would read as "the staleness guard was
        removed" rather than "it was scoped to the order it is about".
        """
        from engine.options.orderstate import BrokerOrderSnapshot

        store = PositionStore(tmp_path / "state" / "positions.jsonl")
        intent = spread(credit="1.50")
        store.record_open_submitted(intent, at=NOW, buying_power_reserved=D("500"))
        recorder = LifecycleRecorder(store)

        ids = {"order_id": 900, "perm_id": 1_151_642_162}
        assert (
            recorder.observe(
                intent.strategy_id,
                BrokerOrderSnapshot(
                    state=OrderLifecycleState.CANCELLED, observed_at=NOW, **ids
                ),
            )
            is True
        )
        assert (
            recorder.observe(
                intent.strategy_id,
                BrokerOrderSnapshot(
                    state=OrderLifecycleState.ACKNOWLEDGED, observed_at=NOW, **ids
                ),
            )
            is False
        )

    def test_a_null_sink_still_leaves_the_ladder_correct(self, tmp_path: Path) -> None:
        """The sink records; it cannot act. Every assertion about *what the
        ladder did* must hold with persistence switched off, or the sink is
        doing control flow."""
        gate = gate_for(tmp_path)
        sink = NullLifecycleSink()
        ib = LadderIB()
        outcome = work(ib, spread(credit="1.50"), gate=gate, sink=sink)

        assert outcome.attempts == 4
        assert outcome.cancelled is True
        assert sink.observed, "nothing was observed at all"
