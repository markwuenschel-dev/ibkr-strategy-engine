"""The one door: exactly one transmitting call, and it cannot open unauthorized.

This file replaces the package-wide "zero placeOrder" proof that held until the
execution layer landed. That property is deliberately gone. What replaces it is
narrower and worth more:

* exactly **one** `placeOrder` exists in `engine.options`, it lives in
  `transmit.py`, and it is inside `place_combo`;
* `place_combo` takes a `TransmitAuthorization` as a **required** argument;
* a `TransmitAuthorization` cannot be constructed outside `transmit.py`;
* minting one runs every gate.

Together those mean "forgot to check the gates before sending" is not a mistake
that compiles.
"""

from __future__ import annotations

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from engine.config import EngineConfig
from engine.errors import HaltedError, RefusedError
from engine.journal import OrderJournal
from engine.options import transmit as transmit_module
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
from engine.options.orderstate import OrderLifecycleState
from engine.options.policy import RiskPolicy
from engine.options.portfolio import PortfolioSnapshot
from engine.options.risk import (
    CHECK_BROKER_MARGIN,
    CHECK_DEFINED_LOSS,
    CHECK_MARKET_DATA_ENTITLEMENT,
    CHECK_STRESS_LOSS,
    CandidateRiskAssessment,
    CheckResult,
    RiskRefusalReason,
)
from engine.options.transmit import (
    TransmitAuthorization,
    authorize_close,
    authorize_open,
    place_combo,
)
from engine.safety import SafetyGate

D = Decimal
NOW = dt.datetime(2026, 7, 29, 13, 0, tzinfo=dt.timezone.utc)
EXPIRY = dt.date(2026, 9, 18)

TRANSMIT_SOURCE = Path(transmit_module.__file__).resolve()
PACKAGE_DIR = TRANSMIT_SOURCE.parent


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


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
        maximum_loss_per_contract=(D("5") - D(credit)) * 100,
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
        results=tuple(
            CheckResult(check=name, approved=True, detail="ok")
            for name in (
                CHECK_MARKET_DATA_ENTITLEMENT,
                CHECK_DEFINED_LOSS,
                CHECK_BROKER_MARGIN,
                CHECK_STRESS_LOSS,
            )
        ),
    )


def refusing_risk(strategy_id: UUID) -> CandidateRiskAssessment:
    results = [
        CheckResult(check=name, approved=True, detail="ok")
        for name in (CHECK_DEFINED_LOSS, CHECK_BROKER_MARGIN, CHECK_STRESS_LOSS)
    ]
    results.insert(
        0,
        CheckResult(
            check=CHECK_MARKET_DATA_ENTITLEMENT,
            approved=False,
            reason=RiskRefusalReason.NO_MARKET_DATA_SNAPSHOT,
            detail="no live data",
        ),
    )
    return CandidateRiskAssessment(
        strategy_id=strategy_id,
        evaluated_at=NOW,
        policy_version="test",
        results=tuple(results),
    )


def approving_governor(intent: OptionStrategyIntent) -> Any:
    snapshot = PortfolioSnapshot(
        as_of=NOW, net_liquidation=D("1000000"), positions=()
    )
    return PortfolioGovernor(RiskPolicy()).evaluate(
        intent,
        snapshot=snapshot,
        margin=_margin(),
        decision_time=NOW,
    )


def refusing_governor(intent: OptionStrategyIntent) -> Any:
    return PortfolioGovernor(RiskPolicy()).evaluate(
        intent, snapshot=None, margin=None, decision_time=NOW
    )


def _margin() -> Any:
    from engine.options.execution import MarginAssessment

    return MarginAssessment(
        accepted=True,
        observed_at=NOW,
        initial_margin_change=D("500"),
        maintenance_margin_change=D("500"),
    )


def authorized(tmp_path: Path, intent: OptionStrategyIntent) -> TransmitAuthorization:
    return authorize_open(
        intent,
        gate=gate_for(tmp_path),
        risk=approving_risk(intent.strategy_id),
        governor=approving_governor(intent),
        armed=True,
        now=NOW,
    )


class RecordingIB:
    def __init__(self) -> None:
        self.placed: list[tuple[Any, Any]] = []

    def placeOrder(self, contract: Any, order: Any) -> Any:  # noqa: N802
        self.placed.append((contract, order))
        return _Trade()

    def sleep(self, _seconds: float) -> None:
        return None


class _Trade:
    def __init__(self) -> None:
        self.order = _Order()
        self.orderStatus = _Status()  # noqa: N815

    def isDone(self) -> bool:  # noqa: N802
        return True


class _Order:
    orderId = 77  # noqa: N815


class _Status:
    status = "Filled"
    filled = 1.0
    avgFillPrice = 1.5  # noqa: N815


# ===========================================================================
# The chokepoint is structurally unique
# ===========================================================================


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class TestExactlyOneDoor:
    def test_placeorder_appears_exactly_once_in_the_package(self) -> None:
        """The load-bearing structural claim. More than one transmitting call
        means more than one place the gates could be skipped."""
        sites: list[str] = []
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            for node in ast.walk(parse(path)):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "placeOrder"
                ):
                    sites.append(f"{path.name}:{node.lineno}")
        assert len(sites) == 1, f"expected exactly one transmitting call, found {sites}"
        assert sites[0].startswith("transmit.py:"), sites

    def test_the_only_placeorder_is_inside_place_combo(self) -> None:
        """Inside the gate-checking function, not merely in the same file."""
        enclosing: list[str] = []
        for node in ast.walk(parse(TRANSMIT_SOURCE)):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "placeOrder"
                ):
                    enclosing.append(node.name)
        assert enclosing == ["place_combo"], enclosing

    def test_only_transmit_arms_an_order(self) -> None:
        """``order.transmit = True`` is what arms an ib_async order."""
        offenders: list[str] = []
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "transmit":
                        if path.name != "transmit.py":
                            offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], offenders

    def test_place_combo_requires_an_authorization_argument(self) -> None:
        """Keyword-only and with NO default. A default of None would make the
        safety property depend on every caller remembering to pass it."""
        signature = None
        for node in ast.walk(parse(TRANSMIT_SOURCE)):
            if isinstance(node, ast.FunctionDef) and node.name == "place_combo":
                signature = node.args
        assert signature is not None
        names = [arg.arg for arg in signature.kwonlyargs]
        assert "authorization" in names
        index = names.index("authorization")
        assert signature.kw_defaults[index] is None, (
            "authorization must have no default value"
        )

    def test_no_module_outside_transmit_imports_the_equity_broker(self) -> None:
        offenders: list[str] = []
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                    "broker"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], offenders


# ===========================================================================
# The token cannot be forged
# ===========================================================================


class TestAuthorizationCannotBeForged:
    def test_direct_construction_is_refused(self) -> None:
        with pytest.raises(RefusedError, match="cannot be constructed directly"):
            TransmitAuthorization(
                strategy_id=uuid4(),
                action=StrategyAction.OPEN,
                authorized_at=NOW,
                armed=True,
            )

    def test_a_guessed_key_is_refused(self) -> None:
        """Compared by identity, so no plausible-looking value works."""
        for guess in (object(), "key", True, 1, "_AUTHORIZATION_KEY"):
            with pytest.raises(RefusedError):
                TransmitAuthorization(
                    strategy_id=uuid4(),
                    action=StrategyAction.OPEN,
                    authorized_at=NOW,
                    armed=True,
                    key=guess,
                )

    def test_place_combo_cannot_be_called_without_one(self) -> None:
        """Not a refusal -- a TypeError, because the argument is required."""
        with pytest.raises(TypeError):
            place_combo(RecordingIB(), spread())  # type: ignore[call-arg]

    def test_place_combo_rejects_a_non_authorization(self) -> None:
        ib = RecordingIB()
        with pytest.raises(RefusedError, match="requires a TransmitAuthorization"):
            place_combo(ib, spread(), authorization="yes")  # type: ignore[arg-type]
        assert ib.placed == []


# ===========================================================================
# Minting one runs every gate
# ===========================================================================


class TestAuthorizeOpen:
    def test_a_fully_gated_open_is_authorized(self, tmp_path: Path) -> None:
        intent = spread()
        auth = authorized(tmp_path, intent)
        assert auth.strategy_id == intent.strategy_id
        assert auth.action is StrategyAction.OPEN
        assert auth.armed is True

    def test_the_kill_switch_refuses(self, tmp_path: Path) -> None:
        gate = gate_for(tmp_path)
        gate.config.halt_file.write_text("stopped by hand", encoding="utf-8")
        intent = spread()
        with pytest.raises(HaltedError):
            authorize_open(
                intent,
                gate=gate,
                risk=approving_risk(intent.strategy_id),
                governor=approving_governor(intent),
                armed=True,
                now=NOW,
            )

    def test_an_unarmed_run_refuses(self, tmp_path: Path) -> None:
        intent = spread()
        with pytest.raises(RefusedError, match="not armed"):
            authorize_open(
                intent,
                gate=gate_for(tmp_path),
                risk=approving_risk(intent.strategy_id),
                governor=approving_governor(intent),
                armed=False,
                now=NOW,
            )

    def test_a_symbol_off_the_allowlist_refuses(self, tmp_path: Path) -> None:
        intent = spread(underlying="MSFT")
        with pytest.raises(RefusedError, match="allowlist"):
            authorize_open(
                intent,
                gate=gate_for(tmp_path),
                risk=approving_risk(intent.strategy_id),
                governor=approving_governor(intent),
                armed=True,
                now=NOW,
            )

    def test_a_refused_risk_assessment_refuses(self, tmp_path: Path) -> None:
        intent = spread()
        with pytest.raises(RefusedError, match="candidate risk refused"):
            authorize_open(
                intent,
                gate=gate_for(tmp_path),
                risk=refusing_risk(intent.strategy_id),
                governor=approving_governor(intent),
                armed=True,
                now=NOW,
            )

    def test_a_refused_governor_refuses(self, tmp_path: Path) -> None:
        intent = spread()
        with pytest.raises(RefusedError, match="governor refused"):
            authorize_open(
                intent,
                gate=gate_for(tmp_path),
                risk=approving_risk(intent.strategy_id),
                governor=refusing_governor(intent),
                armed=True,
                now=NOW,
            )

    def test_the_daily_cap_refuses(self, tmp_path: Path) -> None:
        gate = gate_for(tmp_path, max_orders_per_session=2)
        for _ in range(2):
            gate.journal.record("order_placed", symbol="SPY")
        intent = spread()
        with pytest.raises(RefusedError, match="already placed today"):
            authorize_open(
                intent,
                gate=gate,
                risk=approving_risk(intent.strategy_id),
                governor=approving_governor(intent),
                armed=True,
                now=NOW,
            )

    def test_an_approval_for_another_strategy_is_refused(self, tmp_path: Path) -> None:
        """An approval is for one specific structure. Without this, an approved
        1-lot could authorize a 10-lot."""
        intent = spread(quantity=1)
        other = spread(quantity=10)
        with pytest.raises(RefusedError, match="risk assessment is for"):
            authorize_open(
                intent,
                gate=gate_for(tmp_path),
                risk=approving_risk(other.strategy_id),
                governor=approving_governor(intent),
                armed=True,
                now=NOW,
            )

    def test_arming_is_checked_last(self, tmp_path: Path) -> None:
        """An unarmed run must still surface a real problem rather than stopping
        at 'not armed' and hiding it until the next run."""
        intent = spread(underlying="MSFT")
        with pytest.raises(RefusedError, match="allowlist"):
            authorize_open(
                intent,
                gate=gate_for(tmp_path),
                risk=approving_risk(intent.strategy_id),
                governor=approving_governor(intent),
                armed=False,
                now=NOW,
            )


# ===========================================================================
# Closes are authorized differently, on purpose
# ===========================================================================


def closing_intent_for(intent: OptionStrategyIntent) -> OptionStrategyIntent:
    return intent.closing_intent(
        strategy_id=uuid4(),
        limit_price=D("0.75"),
        created_at=NOW,
        configuration_version="test",
        quantity=intent.quantity,
    )


class TestAuthorizeClose:
    def test_a_close_needs_no_governor_verdict(self, tmp_path: Path) -> None:
        """Refusing to close because the book is concentrated is backwards --
        closing is what reduces concentration."""
        closing = closing_intent_for(spread())
        auth = authorize_close(
            closing, gate=gate_for(tmp_path), armed=True, now=NOW
        )
        assert auth.action is StrategyAction.CLOSE
        assert auth.governor is None

    def test_a_close_is_exempt_from_the_daily_cap(self, tmp_path: Path) -> None:
        """A cap that can trap you in a position is not a safety feature."""
        gate = gate_for(tmp_path, max_orders_per_session=1)
        for _ in range(5):
            gate.journal.record("order_placed", symbol="SPY")
        closing = closing_intent_for(spread())
        auth = authorize_close(closing, gate=gate, armed=True, now=NOW)
        assert auth.action is StrategyAction.CLOSE

    def test_the_kill_switch_still_blocks_a_close(self, tmp_path: Path) -> None:
        """The one case where the operator said stop, and the engine obeys."""
        gate = gate_for(tmp_path)
        gate.config.halt_file.write_text("stop", encoding="utf-8")
        with pytest.raises(HaltedError):
            authorize_close(
                closing_intent_for(spread()), gate=gate, armed=True, now=NOW
            )

    def test_an_unarmed_close_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(RefusedError, match="not armed"):
            authorize_close(
                closing_intent_for(spread()), gate=gate_for(tmp_path), armed=False, now=NOW
            )

    def test_an_open_intent_is_refused_by_authorize_close(self, tmp_path: Path) -> None:
        with pytest.raises(RefusedError, match="received a OPEN intent"):
            authorize_close(spread(), gate=gate_for(tmp_path), armed=True, now=NOW)


# ===========================================================================
# Transmission itself
# ===========================================================================


class TestPlaceCombo:
    def test_an_authorized_open_transmits_once(self, tmp_path: Path) -> None:
        intent = spread()
        ib = RecordingIB()
        result = place_combo(
            ib, intent, authorization=authorized(tmp_path, intent), account="DU1234567"
        )
        assert len(ib.placed) == 1
        assert result.transmitted is True
        assert result.order_id == 77
        # A lifecycle state, not a raw broker string. The string collapsed nine
        # outcomes into two and read a partial fill as a failure.
        assert result.state is OrderLifecycleState.FILLED
        assert result.is_filled is True
        assert result.has_position is True
        assert result.is_uncertain is False

    def test_a_credit_is_sent_as_a_buy_at_a_negative_limit(self, tmp_path: Path) -> None:
        """IBKR rejects SELL at a positive price as a riskless combination."""
        intent = spread(credit="1.50")
        ib = RecordingIB()
        place_combo(ib, intent, authorization=authorized(tmp_path, intent))
        _, order = ib.placed[0]
        assert order.action == "BUY"
        assert order.lmtPrice == -1.5
        assert order.transmit is True
        assert order.tif == "DAY"

    def test_an_authorization_for_another_strategy_cannot_transmit(
        self, tmp_path: Path
    ) -> None:
        intent = spread()
        other = spread()
        ib = RecordingIB()
        with pytest.raises(RefusedError, match="authorization is for strategy"):
            place_combo(ib, other, authorization=authorized(tmp_path, intent))
        assert ib.placed == []

    def test_an_open_authorization_cannot_transmit_a_close(self, tmp_path: Path) -> None:
        intent = spread()
        auth = authorized(tmp_path, intent)
        closing = intent.closing_intent(
            strategy_id=intent.strategy_id,
            limit_price=D("0.75"),
            created_at=NOW,
            configuration_version="test",
            quantity=intent.quantity,
        )
        ib = RecordingIB()
        with pytest.raises(RefusedError, match="authorization is for OPEN"):
            place_combo(ib, closing, authorization=auth)
        assert ib.placed == []


class TestUnarmedCannotReachTheBroker:
    def test_the_whole_path_refuses_before_any_socket_call(self, tmp_path: Path) -> None:
        """End to end: an unarmed run cannot obtain a token, and without a token
        there is nothing to hand place_combo."""
        intent = spread()
        ib = RecordingIB()
        with pytest.raises(RefusedError):
            auth = authorize_open(
                intent,
                gate=gate_for(tmp_path),
                risk=approving_risk(intent.strategy_id),
                governor=approving_governor(intent),
                armed=False,
                now=NOW,
            )
            place_combo(ib, intent, authorization=auth)
        assert ib.placed == []
