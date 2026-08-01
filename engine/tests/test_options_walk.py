"""The price walk: what it sends, what it refuses, and what it does with a fill.

The fakes here are a small model of a broker rather than stubs, because almost
every property worth asserting is about *sequence* -- what was cancelled before
what was sent, which quantity was replaced, whether a fill that landed during a
cancellation was counted. A stub that returns a canned snapshot cannot express
any of those, and a test built on one would pass whichever order the code ran the
steps in.

The reverifier is the **real** :class:`PolicyReverifier` in every test where the
risk budget matters, driven by a fake what-if whose margin tracks
``width - credit`` the way IBKR's does. That is deliberate: the failure this lane
exists to prevent is a walk that re-prices four times having checked its risk
once, and a scripted reverifier that always approves would let exactly that bug
through while the suite stayed green.

The clock is fake and every sleep advances it, so a two-minute walk runs in
microseconds and the dwell is still a real number the code has to respect.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

import pytest

from engine.config import EngineConfig
from engine.errors import RefusedError
from engine.journal import OrderJournal
from engine.options.execution import MarginAssessment
from engine.options.marketdata import (
    MarketDataProvenance,
    MarketDataType,
    OptionGreeks,
    OptionQuote,
    UnderlyingQuote,
)
from engine.options.orderstate import BrokerOrderSnapshot, classify
from engine.options.policy import RiskPolicy
from engine.options.portfolio import PortfolioSnapshot
from engine.options.ports import StrategyQuoteSnapshot
from engine.options.pricing import midpoint_credit, natural_credit
from engine.options.proof import envelope_for
from engine.options.transmit import structure_digest
from engine.options.walk import (
    OrderCancellationPort,
    PolicyReverifier,
    PriceWalk,
    Reverification,
    RiskReverifier,
    WalkPolicy,
    WalkRefusal,
    WalkState,
    reprice,
)
from engine.safety import SafetyGate

from reviewer import reviewed  # noqa: E402 - sibling test module, see docstring
from test_options_pricing import (  # noqa: E402 - sibling test module, see docstring
    EXPECTED_RUNGS,
    LONG_ASK,
    LONG_BID,
    LONG_CON_ID,
    NOW,
    SHORT_ASK,
    SHORT_BID,
    SHORT_CON_ID,
    vertical,
)

D = Decimal
ZERO = D("0")

#: Net liquidation big enough that no fractional cap binds, so a refusal in a
#: test is always the cap the test set and never one it forgot about.
NET_LIQUIDATION = D("100000")


# ---------------------------------------------------------------------------
# A clock every pause advances
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self, start: dt.datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=float(seconds))


# ---------------------------------------------------------------------------
# A book that moves, and is always stamped at the current time
# ---------------------------------------------------------------------------


@dataclass
class FakeMarketData:
    """Quotes stamped at *now*, so a walk that takes two minutes stays fresh.

    ``shift`` moves both legs together, which is how a test walks the market
    away from the price the entry was authorized at without touching the intent.
    """

    clock: Clock
    shift: Decimal = ZERO
    liveness: MarketDataType = MarketDataType.LIVE
    calls: list[tuple[str, tuple[int, ...]]] = field(default_factory=list)
    widen_long_ask: Decimal = ZERO

    def _provenance(self, generation: UUID) -> MarketDataProvenance:
        at = self.clock()
        return MarketDataProvenance(
            requested_type=int(MarketDataType.LIVE),
            subscription_generation=generation,
            subscribed_at=at,
            reported_type=int(self.liveness),
            callback_received=True,
            last_provider_event_at=at,
            last_local_receive_at=at,
        )

    def strategy_quotes(
        self, *, underlying_symbol: str, con_ids: Any
    ) -> StrategyQuoteSnapshot:
        ids = tuple(int(c) for c in con_ids)
        self.calls.append((underlying_symbol, ids))
        short_gen, long_gen, under_gen = uuid4(), uuid4(), uuid4()
        # Greeks carry the *same* generation as their quote: the provenance gate
        # refuses greeks from a superseded subscription, and a mismatch here
        # would refuse every candidate for a reason no test in this file is
        # about.
        legs = (
            OptionQuote(
                con_id=SHORT_CON_ID,
                provenance=self._provenance(short_gen),
                bid=SHORT_BID + self.shift,
                ask=SHORT_ASK + self.shift,
                greeks=OptionGreeks(
                    received_at=self.clock(),
                    subscription_generation=short_gen,
                    delta=D("-0.30"),
                ),
            ),
            OptionQuote(
                con_id=LONG_CON_ID,
                provenance=self._provenance(long_gen),
                bid=LONG_BID,
                ask=LONG_ASK + self.widen_long_ask,
                greeks=OptionGreeks(
                    received_at=self.clock(),
                    subscription_generation=long_gen,
                    delta=D("-0.27"),
                ),
            ),
        )
        return StrategyQuoteSnapshot(
            underlying=UnderlyingQuote(
                symbol=underlying_symbol,
                provenance=self._provenance(under_gen),
                bid=D("449.90"),
                ask=D("450.10"),
            ),
            legs=legs,
            generations=(
                ("underlying", under_gen),
                (str(SHORT_CON_ID), short_gen),
                (str(LONG_CON_ID), long_gen),
            ),
        )


# ---------------------------------------------------------------------------
# A broker with one resting order at a time
# ---------------------------------------------------------------------------


@dataclass
class _Resting:
    order_id: int
    perm_id: int
    quantity: int
    limit: float
    filled: Decimal = ZERO
    status: str = "Submitted"

    @property
    def remaining(self) -> Decimal:
        return Decimal(self.quantity) - self.filled


class _Status:
    def __init__(self, resting: _Resting) -> None:
        self.status = resting.status
        self.filled = float(resting.filled)
        self.remaining = float(resting.remaining)
        # A net credit fills at a NEGATIVE average price. Zero would read as an
        # unpopulated field, which is what snapshot_from_trade treats it as.
        self.avgFillPrice = -resting.limit if resting.filled > ZERO else 0.0  # noqa: N815
        self.permId = resting.perm_id  # noqa: N815
        self.commission = 1.02


class _OrderRef:
    def __init__(self, resting: _Resting) -> None:
        self.orderId = resting.order_id  # noqa: N815
        self.permId = resting.perm_id  # noqa: N815


class FakeTrade:
    def __init__(self, resting: _Resting) -> None:
        self._resting = resting
        self.log: tuple[Any, ...] = ()

    @property
    def orderStatus(self) -> _Status:  # noqa: N802 - mirrors ib_async
        return _Status(self._resting)

    @property
    def order(self) -> _OrderRef:
        return _OrderRef(self._resting)

    def isDone(self) -> bool:  # noqa: N802 - mirrors ib_async
        return self._resting.status in {"Filled", "Cancelled"}


class FakeIB:
    """Accepts orders, never fills one on its own initiative.

    Fills are scripted by rung, so a test says "the second order fills one of
    three" rather than racing a timer. ``sleep`` advances the shared clock, which
    is what makes ``place_combo``'s dwell loop terminate instantly.
    """

    def __init__(
        self, clock: Clock, *, fill_on_send: dict[int, Decimal] | None = None
    ) -> None:
        self.clock = clock
        self.orders: list[_Resting] = []
        self.sent: list[Any] = []
        self.fill_on_send = fill_on_send or {}

    def isConnected(self) -> bool:  # noqa: N802 - mirrors ib_async
        return True

    def sleep(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def placeOrder(self, bag: Any, order: Any) -> FakeTrade:  # noqa: N802
        rung = len(self.orders) + 1
        resting = _Resting(
            order_id=100 + rung,
            perm_id=900 + rung,
            quantity=int(order.totalQuantity),
            limit=float(order.lmtPrice),
        )
        scripted = self.fill_on_send.get(rung)
        if scripted:
            resting.filled = Decimal(scripted)
            if resting.filled >= Decimal(resting.quantity):
                resting.status = "Filled"
        self.orders.append(resting)
        self.sent.append(order)
        return FakeTrade(resting)

    # -- what the assertions read ------------------------------------------

    @property
    def credits(self) -> list[Decimal]:
        """The credit each order was sent at, as a positive magnitude.

        ``build_combo`` submits a credit as a BUY at a negative limit, so the
        sign flip here is the same one the adapter performs -- asserting on the
        raw ``lmtPrice`` would bake the wire convention into every test.
        """
        return [D(str(-order.lmtPrice)) for order in self.sent]

    @property
    def quantities(self) -> list[int]:
        return [int(order.totalQuantity) for order in self.sent]


@dataclass
class FakeCancellation:
    """The seam another lane owns, faked. Confirms unless told not to."""

    ib: FakeIB
    clock: Clock
    confirms: bool = True
    fill_during_cancel: dict[int, Decimal] = field(default_factory=dict)
    requests: list[int | None] = field(default_factory=list)
    observations: int = 0

    def _find(self, order_id: int | None) -> _Resting | None:
        for resting in self.ib.orders:
            if resting.order_id == order_id:
                return resting
        return None

    def request_cancellation(
        self, *, strategy_id: UUID, order_id: int | None, perm_id: int | None
    ) -> None:
        self.requests.append(order_id)
        resting = self._find(order_id)
        if resting is None:
            return
        arriving = self.fill_during_cancel.get(order_id)
        if arriving:
            resting.filled += Decimal(arriving)
        if not self.confirms:
            return
        resting.status = (
            "Filled" if resting.filled >= Decimal(resting.quantity) else "Cancelled"
        )

    def observe_order(
        self, *, strategy_id: UUID, order_id: int | None, perm_id: int | None
    ) -> BrokerOrderSnapshot | None:
        self.observations += 1
        resting = self._find(order_id)
        if resting is None:
            return None
        return BrokerOrderSnapshot(
            state=classify(
                resting.status,
                filled=resting.filled,
                remaining=resting.remaining,
                quantity=resting.quantity,
            ),
            observed_at=self.clock(),
            raw_status=resting.status,
            order_id=resting.order_id,
            perm_id=resting.perm_id,
            filled=resting.filled,
            remaining=resting.remaining,
            average_price=(
                D(str(-resting.limit)) if resting.filled > ZERO else None
            ),
        )


@dataclass
class FakeWhatIf:
    """Margin that tracks ``width - credit``, the way a defined-risk spread does.

    Recording every call is the point: if the walk re-prices without re-asking,
    the recorded credits will not match the credits that were sent, and the test
    that compares them fails.
    """

    asked: list[tuple[Decimal, int]] = field(default_factory=list)
    width: Decimal = D("1")
    multiplier: Decimal = D("100")

    def what_if(self, intent: Any, *, observed_at: dt.datetime) -> MarginAssessment:
        self.asked.append((intent.limit_price, intent.quantity))
        reserved = (
            (self.width - intent.limit_price) * self.multiplier * intent.quantity
        )
        return MarginAssessment(
            accepted=True,
            observed_at=observed_at,
            initial_margin_change=reserved,
            maintenance_margin_change=reserved,
            commission=D("1.02"),
        )


@dataclass
class FakePortfolio:
    clock: Clock
    net_liquidation: Decimal = NET_LIQUIDATION

    def snapshot(self, *, as_of: dt.datetime) -> PortfolioSnapshot:
        return PortfolioSnapshot(as_of=as_of, net_liquidation=self.net_liquidation)


@dataclass
class ScriptedReverifier:
    """Approves or refuses on demand, for the cases where the gate itself is
    not what is under test. Wraps a real reverifier so an approval still
    carries genuine verdicts -- ``authorize_open`` checks them."""

    inner: RiskReverifier
    approve: Callable[[Any], bool] = lambda intent: True
    seen: list[tuple[Decimal, int]] = field(default_factory=list)

    def reverify(self, intent: Any, *, quotes: Any, now: dt.datetime):
        self.seen.append((intent.limit_price, intent.quantity))
        if not self.approve(intent):
            return Reverification(
                approved=False, refusals=("scripted refusal for this rung",)
            )
        return self.inner.reverify(intent, quotes=quotes, now=now)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def gate_for(tmp_path: Path) -> SafetyGate:
    config = EngineConfig(
        account_id="DU1234567",
        port=7497,
        state_dir=tmp_path / "state",
        symbol_allowlist=("SPY",),
    )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return SafetyGate(config, OrderJournal(config.journal_path))


def policy_for(**overrides: Any) -> RiskPolicy:
    settings: dict[str, Any] = {
        "max_defined_loss_per_position": D("500"),
        "max_broker_margin_per_position": D("500"),
        "max_stress_loss_per_position": D("500"),
    }
    settings.update(overrides)
    return RiskPolicy(**settings)


def walk_for(
    tmp_path: Path,
    *,
    clock: Clock | None = None,
    market: FakeMarketData | None = None,
    ib: FakeIB | None = None,
    cancellation: FakeCancellation | None = None,
    reverifier: Any = None,
    policy: RiskPolicy | None = None,
    what_if: FakeWhatIf | None = None,
    walk_policy: WalkPolicy | None = None,
    fills: list[tuple[UUID, Decimal]] | None = None,
    releases: list[tuple[UUID, Decimal]] | None = None,
) -> PriceWalk:
    clock = clock or Clock()
    market = market or FakeMarketData(clock)
    ib = ib or FakeIB(clock)
    cancellation = cancellation or FakeCancellation(ib, clock)
    what_if = what_if or FakeWhatIf()
    reverifier = reverifier or PolicyReverifier(
        policy=policy or policy_for(),
        what_if=what_if,
        portfolio=FakePortfolio(clock),
    )
    # A walk with no independent verifier authorizes nothing, so every test here
    # would refuse at rung one for a reason none of them are about. The gate is
    # the real one over a temp collab, with the reviewer seat answering as each
    # rung's request is filed -- and each rung is a new price, so a new spec, so
    # its own request and its own answer.
    verifier, context = reviewed(tmp_path)
    return PriceWalk(
        ib=ib,
        market_data=market,
        cancellation=cancellation,
        reverifier=reverifier,
        gate=gate_for(tmp_path),
        # Two-second dwell and cancellation window: the shape of the walk is
        # unchanged, and every fake sleep advances the fake clock, so the whole
        # four-rung walk runs in well under a millisecond.
        policy=walk_policy
        or WalkPolicy(
            dwell=dt.timedelta(seconds=2),
            cancellation_timeout=dt.timedelta(seconds=2),
            cancellation_poll=dt.timedelta(seconds=1),
        ),
        clock=clock,
        pause=clock.advance,
        verifier=verifier,
        approval_context=context,
        on_fill=(
            (lambda sid, qty, snap: fills.append((sid, qty)))
            if fills is not None
            else None
        ),
        on_release=(
            (lambda sid, qty: releases.append((sid, qty)))
            if releases is not None
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Repricing
# ---------------------------------------------------------------------------


class TestReprice:
    def test_a_lower_credit_raises_the_maximum_loss(self) -> None:
        """The whole reason step 5 exists, as arithmetic: giving up three cents
        of credit does not merely earn less, it puts $3 more per contract at
        risk."""
        original = vertical(credit="0.20")
        cheaper = reprice(
            original, credit=D("0.17"), quantity=1, created_at=NOW
        )
        assert original.maximum_loss_per_contract == D("80")
        assert cheaper.maximum_loss_per_contract == D("83")
        assert cheaper.total_maximum_loss > original.total_maximum_loss

    def test_the_lineage_is_preserved_and_the_digest_is_not(self) -> None:
        original = vertical(credit="0.20")
        cheaper = reprice(original, credit=D("0.17"), quantity=1, created_at=NOW)
        assert cheaper.strategy_id == original.strategy_id
        assert cheaper.legs == original.legs
        assert structure_digest(cheaper) != structure_digest(original)

    def test_a_smaller_quantity_also_changes_the_digest(self) -> None:
        original = vertical(credit="0.20", quantity=3)
        remainder = reprice(original, credit=D("0.20"), quantity=2, created_at=NOW)
        assert remainder.quantity == 2
        assert structure_digest(remainder) != structure_digest(original)

    def test_the_remainder_can_never_grow(self) -> None:
        with pytest.raises(RefusedError, match="never grows it"):
            reprice(vertical(quantity=1), credit=D("0.19"), quantity=2, created_at=NOW)

    def test_a_zero_quantity_is_refused(self) -> None:
        with pytest.raises(RefusedError, match="positive int"):
            reprice(vertical(quantity=2), credit=D("0.19"), quantity=0, created_at=NOW)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


class TestTheFourStepWalk:
    def test_it_sends_exactly_the_expected_monotone_credit_sequence(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        ib = FakeIB(clock)
        walk = walk_for(tmp_path, clock=clock, ib=ib)
        intent = vertical(credit="0.20")

        outcome = walk.run(intent)

        assert ib.credits == list(EXPECTED_RUNGS) == [
            D("0.20"),
            D("0.19"),
            D("0.18"),
            D("0.17"),
        ]
        assert outcome.credits == EXPECTED_RUNGS
        # Monotone, and genuinely so -- four distinct prices, each strictly
        # below the one before it.
        assert len(set(ib.credits)) == 4
        assert all(b < a for a, b in zip(ib.credits, ib.credits[1:], strict=False))

    def test_the_first_rung_is_the_midpoint_and_the_last_is_the_natural(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        ib = FakeIB(clock)
        market = FakeMarketData(clock)
        intent = vertical(credit="0.20")
        walk_for(tmp_path, clock=clock, ib=ib, market=market).run(intent)

        quotes = market.strategy_quotes(underlying_symbol="SPY", con_ids=(450, 449))
        assert ib.credits[0] == midpoint_credit(intent, quotes)
        assert ib.credits[-1] == natural_credit(intent, quotes)

    def test_every_rung_re_reads_the_book(self, tmp_path: Path) -> None:
        """Repricing off the quotes that justified the previous rung is
        repricing into a market that has moved."""
        clock = Clock()
        market = FakeMarketData(clock)
        walk_for(tmp_path, clock=clock, market=market).run(vertical(credit="0.20"))
        assert len(market.calls) >= 4
        assert all(call[1] == (SHORT_CON_ID, LONG_CON_ID) for call in market.calls)

    def test_every_rung_is_authorized_against_its_own_structure(
        self, tmp_path: Path
    ) -> None:
        """``place_combo`` recomputes the digest and compares it to the token, so
        four sends that all succeeded is proof that four separate tokens were
        minted for four separate structures. A carried-over authorization would
        have raised at ``transmit.py:405``."""
        clock = Clock()
        ib = FakeIB(clock)
        walk_for(tmp_path, clock=clock, ib=ib).run(vertical(credit="0.20"))
        assert len(ib.sent) == 4
        assert len({order.lmtPrice for order in ib.sent}) == 4

    def test_the_broker_what_if_is_re_asked_at_every_new_credit(
        self, tmp_path: Path
    ) -> None:
        """Margin on a defined-risk spread tracks ``width - credit``. Reusing
        the midpoint's what-if would compare the old margin against the cap
        while sending the new credit."""
        clock = Clock()
        ib = FakeIB(clock)
        what_if = FakeWhatIf()
        walk_for(tmp_path, clock=clock, ib=ib, what_if=what_if).run(
            vertical(credit="0.20")
        )
        asked = [credit for credit, _ in what_if.asked]
        assert asked == list(EXPECTED_RUNGS)

    def test_it_terminates_and_cancels_after_the_fourth_attempt(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        ib = FakeIB(clock)
        cancellation = FakeCancellation(ib, clock)
        releases: list[tuple[UUID, Decimal]] = []
        walk = walk_for(
            tmp_path, clock=clock, ib=ib, cancellation=cancellation, releases=releases
        )
        intent = vertical(credit="0.20")

        outcome = walk.run(intent)

        assert outcome.state is WalkState.EXHAUSTED
        assert outcome.filled == ZERO
        assert len(ib.sent) == 4
        # Three replacements plus the final pull: every order sent is accounted
        # for, and none is left resting.
        assert cancellation.requests == [101, 102, 103, 104]
        assert all(order.status == "Cancelled" for order in ib.orders)
        # The reserved risk is released, once, for the whole unfilled size.
        assert releases == [(intent.strategy_id, D("1"))]
        assert outcome.released == D("1")

    def test_the_whole_walk_fits_inside_its_time_budget(self, tmp_path: Path) -> None:
        """Four rungs at the configured dwell, plus the cancellations. The
        assertion is that the walk is *bounded*, which is the property the
        101-minute order lacked."""
        clock = Clock()
        walk = walk_for(
            tmp_path,
            clock=clock,
            walk_policy=WalkPolicy(
                dwell=dt.timedelta(seconds=30),
                cancellation_timeout=dt.timedelta(seconds=20),
                cancellation_poll=dt.timedelta(seconds=1),
            ),
        )
        walk.run(vertical(credit="0.20"))
        elapsed = clock.now - NOW
        assert elapsed <= dt.timedelta(minutes=3)
        assert elapsed >= dt.timedelta(minutes=2)


# ---------------------------------------------------------------------------
# Step 5: risk at the new credit
# ---------------------------------------------------------------------------


class TestRiskAtTheNewCredit:
    def test_a_rung_that_would_breach_max_loss_is_refused_at_that_step(
        self, tmp_path: Path
    ) -> None:
        """The subtlest failure in the lane, made concrete.

        A 1-wide spread at 0.20 risks $80. The cap is $82. Rungs one to three
        (0.20, 0.19, 0.18) risk 80, 81 and 82 and are all fine. The fourth, at
        0.17, risks $83 -- and is refused *at step 5*, before an order is built,
        because a walk that ignores this walks straight out of the approved risk
        budget while every individual step looks harmless.
        """
        clock = Clock()
        ib = FakeIB(clock)
        cancellation = FakeCancellation(ib, clock)
        walk = walk_for(
            tmp_path,
            clock=clock,
            ib=ib,
            cancellation=cancellation,
            policy=policy_for(max_defined_loss_per_position=D("82")),
        )

        outcome = walk.run(vertical(credit="0.20"))

        assert ib.credits == [D("0.20"), D("0.19"), D("0.18")]
        assert outcome.state is WalkState.REFUSED
        last = outcome.attempts[-1]
        assert last.index == 4
        assert last.sent is False
        assert last.credit == D("0.17")
        assert last.refusal is WalkRefusal.RISK_AT_NEW_CREDIT
        assert "OPTIONS_MAX_DEFINED_LOSS_EXCEEDED" not in last.detail  # prose, not code
        assert "maximum defined loss 83" in last.detail
        # The order resting from rung three is still pulled. A refusal must not
        # leave risk in the book.
        assert cancellation.requests == [101, 102, 103]
        assert all(order.status == "Cancelled" for order in ib.orders)

    def test_a_breach_of_the_broker_margin_cap_is_refused_the_same_way(
        self, tmp_path: Path
    ) -> None:
        """Margin rises with the same arithmetic as maximum loss, so a cap of
        $82 of margin bites in exactly the same place."""
        clock = Clock()
        ib = FakeIB(clock)
        walk = walk_for(
            tmp_path,
            clock=clock,
            ib=ib,
            policy=policy_for(max_broker_margin_per_position=D("82")),
        )
        outcome = walk.run(vertical(credit="0.20"))
        assert ib.credits == [D("0.20"), D("0.19"), D("0.18")]
        assert outcome.attempts[-1].refusal is WalkRefusal.RISK_AT_NEW_CREDIT
        assert "broker margin 83" in outcome.attempts[-1].detail

    def test_a_breach_of_the_stress_cap_is_refused_the_same_way(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        ib = FakeIB(clock)
        walk = walk_for(
            tmp_path,
            clock=clock,
            ib=ib,
            policy=policy_for(max_stress_loss_per_position=D("82")),
        )
        outcome = walk.run(vertical(credit="0.20"))
        assert ib.credits == [D("0.20"), D("0.19"), D("0.18")]
        assert "stress loss 83" in outcome.attempts[-1].detail

    def test_the_first_rung_can_be_refused_and_nothing_is_sent(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        ib = FakeIB(clock)
        cancellation = FakeCancellation(ib, clock)
        walk = walk_for(
            tmp_path,
            clock=clock,
            ib=ib,
            cancellation=cancellation,
            policy=policy_for(max_defined_loss_per_position=D("79")),
        )
        outcome = walk.run(vertical(credit="0.20"))
        assert ib.sent == []
        assert cancellation.requests == []
        assert outcome.state is WalkState.REFUSED
        assert outcome.filled == ZERO

    def test_an_approval_carrying_a_refusing_verdict_cannot_be_constructed(
        self,
    ) -> None:
        """The seam is a Protocol, so a bad reverifier is a real possibility.
        This is what stops one being believed."""
        with pytest.raises(ValueError, match="must carry both"):
            Reverification(approved=True)
        with pytest.raises(ValueError, match="must say why"):
            Reverification(approved=False)

    def test_a_reverifier_that_raises_refuses_rather_than_escapes(
        self, tmp_path: Path
    ) -> None:
        class Broken:
            def reverify(self, intent, *, quotes, now):
                raise RuntimeError("the risk service is down")

        clock = Clock()
        ib = FakeIB(clock)
        outcome = walk_for(tmp_path, clock=clock, ib=ib, reverifier=Broken()).run(
            vertical(credit="0.20")
        )
        assert ib.sent == []
        assert outcome.state is WalkState.REFUSED
        assert "the risk service is down" in outcome.attempts[-1].detail


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------


class TestTheEnvelope:
    def test_a_replacement_outside_the_envelope_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The book falls away after the first rung. The midpoint drops to 0.05,
        below the authorized band's floor of 0.15, and the walk stops rather
        than chasing it down."""
        clock = Clock()
        ib = FakeIB(clock)
        market = FakeMarketData(clock)
        cancellation = FakeCancellation(ib, clock)
        walk = walk_for(
            tmp_path, clock=clock, ib=ib, market=market, cancellation=cancellation
        )
        intent = vertical(credit="0.20")
        envelope = envelope_for(intent)
        assert envelope.minimum == D("0.15")

        original_quotes = market.strategy_quotes
        state = {"n": 0}

        def moving(*, underlying_symbol: str, con_ids: Any) -> StrategyQuoteSnapshot:
            state["n"] += 1
            if state["n"] > 1:
                market.shift = D("-0.15")
            return original_quotes(
                underlying_symbol=underlying_symbol, con_ids=con_ids
            )

        market.strategy_quotes = moving  # type: ignore[method-assign]

        outcome = walk.run(intent)

        assert ib.credits == [D("0.20")]
        assert outcome.state is WalkState.REFUSED
        assert outcome.attempts[-1].refusal is WalkRefusal.OUTSIDE_ENVELOPE
        assert outcome.attempts[-1].credit == D("0.05")
        assert cancellation.requests == [101]

    def test_a_book_that_moved_in_our_favour_is_still_a_book_that_moved(
        self, tmp_path: Path
    ) -> None:
        """Bounded on both sides. The risk figures were computed against the old
        credit, so a *better* price is still a different trade."""
        clock = Clock()
        ib = FakeIB(clock)
        market = FakeMarketData(clock)
        walk = walk_for(tmp_path, clock=clock, ib=ib, market=market)
        intent = vertical(credit="0.20")
        assert envelope_for(intent).maximum == D("0.25")

        original_quotes = market.strategy_quotes
        state = {"n": 0}

        def moving(*, underlying_symbol: str, con_ids: Any) -> StrategyQuoteSnapshot:
            state["n"] += 1
            if state["n"] > 1:
                market.shift = D("0.20")
            return original_quotes(
                underlying_symbol=underlying_symbol, con_ids=con_ids
            )

        market.strategy_quotes = moving  # type: ignore[method-assign]

        outcome = walk.run(intent)
        assert ib.credits == [D("0.20")]
        assert outcome.attempts[-1].refusal is WalkRefusal.OUTSIDE_ENVELOPE
        assert outcome.attempts[-1].credit == D("0.40")

    def test_the_envelope_is_anchored_to_the_authorized_intent(
        self, tmp_path: Path
    ) -> None:
        """Every rung is inside the band derived from the *original* credit, not
        a band re-derived from the rung. A band that followed the walk down
        would contain every price trivially."""
        clock = Clock()
        ib = FakeIB(clock)
        intent = vertical(credit="0.20")
        outcome = walk_for(tmp_path, clock=clock, ib=ib).run(intent)
        anchored = envelope_for(intent)
        assert outcome.envelope == anchored
        assert all(anchored.contains(credit) for credit in ib.credits)
        # The fourth rung, 0.17, is outside a band re-derived from the third
        # rung's own 0.18 only if the tolerance were tighter -- so instead pin
        # the thing that actually matters: the floor never moved.
        assert outcome.ladder is not None
        assert outcome.ladder.floor == anchored.minimum

    def test_a_stale_or_delayed_book_stops_the_walk(self, tmp_path: Path) -> None:
        clock = Clock()
        ib = FakeIB(clock)
        market = FakeMarketData(clock, liveness=MarketDataType.DELAYED)
        outcome = walk_for(tmp_path, clock=clock, ib=ib, market=market).run(
            vertical(credit="0.20")
        )
        assert ib.sent == []
        assert outcome.attempts[-1].refusal is WalkRefusal.NOT_LIVE

    def test_a_market_data_outage_stops_the_walk(self, tmp_path: Path) -> None:
        class Dead:
            def strategy_quotes(self, *, underlying_symbol: str, con_ids: Any):
                raise ConnectionError("the subscription dropped")

        clock = Clock()
        ib = FakeIB(clock)
        outcome = walk_for(tmp_path, clock=clock, ib=ib, market=Dead()).run(
            vertical(credit="0.20")
        )
        assert ib.sent == []
        assert outcome.attempts[-1].refusal is WalkRefusal.NO_QUOTES


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------


class TestFills:
    def test_a_partial_fill_mid_walk_replaces_only_the_remainder(
        self, tmp_path: Path
    ) -> None:
        """One of three fills on the first rung. Every subsequent rung must
        replace two, not three -- re-sending the full size would open two
        contracts nobody approved."""
        clock = Clock()
        ib = FakeIB(clock, fill_on_send={1: D("1")})
        cancellation = FakeCancellation(ib, clock)
        fills: list[tuple[UUID, Decimal]] = []
        walk = walk_for(
            tmp_path, clock=clock, ib=ib, cancellation=cancellation, fills=fills
        )
        intent = vertical(credit="0.20", quantity=3)

        outcome = walk.run(intent)

        assert ib.quantities == [3, 2, 2, 2]
        assert ib.credits == list(EXPECTED_RUNGS)
        assert outcome.filled == D("1")
        assert outcome.remaining == D("2")
        assert outcome.state is WalkState.PARTIALLY_FILLED
        assert outcome.has_position is True
        assert fills == [(intent.strategy_id, D("1"))]

    def test_the_risk_of_the_remainder_is_re_verified_at_the_smaller_size(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        ib = FakeIB(clock, fill_on_send={1: D("1")})
        what_if = FakeWhatIf()
        walk_for(tmp_path, clock=clock, ib=ib, what_if=what_if).run(
            vertical(credit="0.20", quantity=3)
        )
        assert what_if.asked == [
            (D("0.20"), 3),
            (D("0.19"), 2),
            (D("0.18"), 2),
            (D("0.17"), 2),
        ]

    def test_a_full_fill_on_the_first_rung_ends_the_walk(self, tmp_path: Path) -> None:
        clock = Clock()
        ib = FakeIB(clock, fill_on_send={1: D("1")})
        cancellation = FakeCancellation(ib, clock)
        walk = walk_for(tmp_path, clock=clock, ib=ib, cancellation=cancellation)

        outcome = walk.run(vertical(credit="0.20"))

        assert outcome.state is WalkState.FILLED
        assert outcome.filled == D("1")
        assert len(ib.sent) == 1
        assert cancellation.requests == []

    def test_a_fill_during_cancellation_is_recorded_as_a_position(
        self, tmp_path: Path
    ) -> None:
        """A real race, not a hypothetical: IBKR reports the order terminal with
        a positive ``filled``, and a walk that read only the final status would
        record a live spread as a cancelled order and then send another one."""
        clock = Clock()
        ib = FakeIB(clock)
        cancellation = FakeCancellation(
            ib, clock, fill_during_cancel={101: D("1")}
        )
        fills: list[tuple[UUID, Decimal]] = []
        walk = walk_for(
            tmp_path, clock=clock, ib=ib, cancellation=cancellation, fills=fills
        )
        intent = vertical(credit="0.20")

        outcome = walk.run(intent)

        assert outcome.state is WalkState.FILLED
        assert outcome.filled == D("1")
        assert outcome.has_position is True
        assert fills == [(intent.strategy_id, D("1"))]
        # And crucially: the replacement was never sent on top of it.
        assert len(ib.sent) == 1
        assert ib.orders[0].status == "Filled"

    def test_a_partial_fill_during_cancellation_shrinks_the_next_rung(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        ib = FakeIB(clock)
        cancellation = FakeCancellation(ib, clock, fill_during_cancel={101: D("2")})
        what_if = FakeWhatIf()
        walk = walk_for(
            tmp_path,
            clock=clock,
            ib=ib,
            cancellation=cancellation,
            what_if=what_if,
        )

        outcome = walk.run(vertical(credit="0.20", quantity=3))

        assert ib.quantities[0] == 3
        assert ib.quantities[1] == 1
        assert outcome.filled == D("2")
        # Step 5 ran twice for rung two -- once at the size before the fill and
        # again at the size after it -- because the structure changed.
        assert (D("0.19"), 3) in what_if.asked
        assert (D("0.19"), 1) in what_if.asked

    def test_the_same_cumulative_fill_is_never_counted_twice(
        self, tmp_path: Path
    ) -> None:
        """``filled`` on a snapshot is cumulative for that order, and the walk
        observes the same order several times per rung."""
        clock = Clock()
        ib = FakeIB(clock, fill_on_send={1: D("1")})
        fills: list[tuple[UUID, Decimal]] = []
        outcome = walk_for(tmp_path, clock=clock, ib=ib, fills=fills).run(
            vertical(credit="0.20", quantity=3)
        )
        assert outcome.filled == D("1")
        assert len(fills) == 1


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_an_unconfirmed_cancellation_stops_the_walk(self, tmp_path: Path) -> None:
        """The one thing worse than an unfilled spread is two of them. An order
        we cannot prove is dead may still be working."""
        clock = Clock()
        ib = FakeIB(clock)
        cancellation = FakeCancellation(ib, clock, confirms=False)
        walk = walk_for(tmp_path, clock=clock, ib=ib, cancellation=cancellation)

        outcome = walk.run(vertical(credit="0.20"))

        assert len(ib.sent) == 1
        assert outcome.state is WalkState.UNCERTAIN
        assert outcome.attempts[-1].refusal is WalkRefusal.CANCELLATION_UNCONFIRMED
        assert ib.orders[0].status == "Submitted"

    def test_the_cancellation_wait_is_bounded(self, tmp_path: Path) -> None:
        clock = Clock()
        ib = FakeIB(clock)
        cancellation = FakeCancellation(ib, clock, confirms=False)
        walk = walk_for(tmp_path, clock=clock, ib=ib, cancellation=cancellation)
        walk.run(vertical(credit="0.20"))
        # One dwell plus at most one cancellation window, and no more.
        assert clock.now - NOW <= dt.timedelta(seconds=10)

    def test_a_cancellation_port_that_raises_is_uncertainty_not_permission(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        ib = FakeIB(clock)

        class Broken(FakeCancellation):
            def request_cancellation(self, **kwargs: Any) -> None:
                raise RuntimeError("the cancel could not be routed")

        walk = walk_for(
            tmp_path, clock=clock, ib=ib, cancellation=Broken(ib, clock)
        )
        outcome = walk.run(vertical(credit="0.20"))
        assert len(ib.sent) == 1
        assert outcome.state is WalkState.UNCERTAIN

    def test_the_port_is_a_runtime_checkable_protocol(self, tmp_path: Path) -> None:
        clock = Clock()
        assert isinstance(FakeCancellation(FakeIB(clock), clock), OrderCancellationPort)
        assert not isinstance(object(), OrderCancellationPort)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class TestReporting:
    def test_the_outcome_records_every_rung_and_its_reason(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        walk = walk_for(
            tmp_path,
            clock=clock,
            policy=policy_for(max_defined_loss_per_position=D("82")),
        )
        outcome = walk.run(vertical(credit="0.20"))
        record = outcome.to_record()
        assert record["state"] == "WALK_REFUSED"
        assert [a["credit"] for a in record["attempts"]] == [
            "0.20",
            "0.19",
            "0.18",
            "0.17",
        ]
        assert record["attempts"][-1]["refusal"] == "WALK_RISK_REFUSED_AT_NEW_CREDIT"
        assert record["ladder"]["rungs"] == ["0.20", "0.19", "0.18", "0.17"]
        assert "PRICE WALK" in outcome.describe()

    def test_an_unarmed_walk_sends_nothing(self, tmp_path: Path) -> None:
        clock = Clock()
        ib = FakeIB(clock)
        outcome = walk_for(tmp_path, clock=clock, ib=ib).run(
            vertical(credit="0.20"), armed=False
        )
        assert ib.sent == []
        assert outcome.attempts[-1].refusal is WalkRefusal.NOT_AUTHORIZED
        assert outcome.state is WalkState.REFUSED

    def test_a_halted_engine_stops_the_walk(self, tmp_path: Path) -> None:
        clock = Clock()
        ib = FakeIB(clock)
        walk = walk_for(tmp_path, clock=clock, ib=ib)
        walk.gate.config.halt_file.parent.mkdir(parents=True, exist_ok=True)
        walk.gate.config.halt_file.write_text("stop", encoding="utf-8")
        outcome = walk.run(vertical(credit="0.20"))
        assert ib.sent == []
        assert outcome.state is WalkState.REFUSED
        assert "halted" in outcome.attempts[-1].detail

    def test_a_symbol_off_the_allowlist_never_reaches_the_market(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        ib = FakeIB(clock)
        outcome = walk_for(tmp_path, clock=clock, ib=ib).run(
            vertical(credit="0.20", underlying="TSLA")
        )
        assert ib.sent == []
        assert outcome.state is WalkState.REFUSED


# ---------------------------------------------------------------------------
# Scripted control, for the cases the real gates cannot express
# ---------------------------------------------------------------------------


class TestScriptedRefusals:
    def test_a_refusal_on_the_third_rung_leaves_nothing_resting(
        self, tmp_path: Path
    ) -> None:
        clock = Clock()
        ib = FakeIB(clock)
        cancellation = FakeCancellation(ib, clock)
        inner = PolicyReverifier(
            policy=policy_for(), what_if=FakeWhatIf(), portfolio=FakePortfolio(clock)
        )
        scripted = ScriptedReverifier(
            inner=inner, approve=lambda intent: intent.limit_price > D("0.18")
        )
        walk = walk_for(
            tmp_path,
            clock=clock,
            ib=ib,
            cancellation=cancellation,
            reverifier=scripted,
        )

        outcome = walk.run(vertical(credit="0.20"))

        assert ib.credits == [D("0.20"), D("0.19")]
        assert outcome.state is WalkState.REFUSED
        assert cancellation.requests == [101, 102]
        assert all(order.status == "Cancelled" for order in ib.orders)
