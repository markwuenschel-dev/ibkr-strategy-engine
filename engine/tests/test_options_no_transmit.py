"""Proof that no options order-placement path exists.

Three independent kinds of evidence, because each one alone has a hole:

**Static (AST).** Every module in ``engine.options`` is parsed and searched for a
call that could transmit. A grep would match a docstring; the AST only matches
real syntax. This is the check that catches a future edit adding
``ib.placeOrder`` inside a strategy function.

**Structural (the ports).** ``engine.options.ports`` is where an execution
capability would have to be declared for the layers above the adapters to reach
it. No protocol there has a transmitting method, so strategy code has nothing to
call even if it wanted to.

**Behavioural (run it).** The scan is run end to end against a fake IB that
records every ``placeOrder``, and the recording is empty. The static checks prove
the call is not written; this proves the path taken at runtime does not reach one
by some route the AST could not see, such as ``getattr``.

Plus the CLI surface: neither options command accepts ``--arm``, and the equity
``trade`` command still does -- so this file also pins that the equity path was
not collaterally disarmed.
"""

from __future__ import annotations

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from engine import options as options_package
from engine.cli import build_parser
from engine.options import ports
from engine.options.scan import SelectionMethod, run_scan

D = Decimal

PACKAGE_DIR = Path(options_package.__file__).resolve().parent

# Names that transmit, or that arm a transmission. ``placeOrder`` is ib_async's;
# ``place`` is engine.broker.Broker's. Both are searched for as attribute access
# regardless of what they are called on, because the receiver's name is exactly
# the part a refactor is free to change.
TRANSMITTING_ATTRIBUTES = frozenset({"placeOrder", "place", "transmit"})

# ``cancelOrder`` is the second broker-mutating verb, and it gets the same
# treatment for the same reason: one call site, inside the chokepoint, behind a
# required token. It is kept in its own set rather than merged into the one
# above because the two are pinned to *different* functions -- ``place_combo``
# and ``cancel_combo`` -- and a single set could not say which.
#
# ``cancelMktData`` is deliberately absent. It cancels a subscription, not an
# order, and the adapters call it on every quote teardown.
CANCELLING_ATTRIBUTES = frozenset({"cancelOrder", "cancelOrderAsync"})

MUTATING_ATTRIBUTES = TRANSMITTING_ATTRIBUTES | CANCELLING_ATTRIBUTES


def options_modules() -> list[Path]:
    """Every module in the package, **recursively**.

    This was ``glob("*.py")``, which walks one directory. A subpackage --
    ``engine/options/strategies/`` say -- would have been invisible to every
    static check in this file, and the checks would have kept passing while
    saying nothing about the new code. ``rglob`` costs nothing today (the
    package is flat, pinned by ``test_the_package_is_flat_or_the_walk_is_deep``)
    and is what makes the checks survive the directory that gets added later.
    """
    return sorted(
        path
        for path in PACKAGE_DIR.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def spread_for_combo() -> Any:
    """A minimal valid credit spread, for the build_combo arming check."""
    import datetime as _dt
    from uuid import uuid4

    from engine.options.domain import (
        OptionLegIntent,
        OptionRight,
        OptionStrategyIntent,
        OrderAction,
        PriceEffect,
        StrategyAction,
        StrategyType,
    )

    expiry = _dt.date(2026, 9, 18)
    now = _dt.datetime(2026, 7, 29, 13, 0, tzinfo=_dt.timezone.utc)
    legs = (
        OptionLegIntent(
            con_id=1001,
            symbol="SPY",
            expiration=expiry,
            strike=D("500"),
            right=OptionRight.PUT,
            action=OrderAction.SELL,
            ratio=1,
            multiplier=100,
            exchange="SMART",
        ),
        OptionLegIntent(
            con_id=1002,
            symbol="SPY",
            expiration=expiry,
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
        underlying="SPY",
        quantity=1,
        legs=legs,
        expiration=expiry,
        limit_price=D("1.50"),
        price_effect=PriceEffect.CREDIT,
        maximum_loss_per_contract=D("350"),
        configuration_version="test",
        created_at=now,
    )


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ===========================================================================
# Static: nothing in the package names a transmitting call
# ===========================================================================


class TestNoTransmittingCallSites:
    def test_the_package_has_modules_to_check(self) -> None:
        """A glob that silently matched nothing would make every test below
        vacuously true -- which is the way this kind of proof usually rots."""
        names = {path.name for path in options_modules()}
        assert "scan.py" in names
        assert "execution.py" in names
        assert "risk.py" in names
        assert "governor.py" in names
        assert len(names) >= 10

    def test_no_module_except_transmit_calls_a_transmitting_method(self) -> None:
        """Every module *other than* the chokepoint is inert.

        The package-wide "zero placeOrder" assertion this replaces was true until
        the execution layer landed and is now the wrong question. The right one
        -- exactly one transmitting call, inside ``place_combo``, behind a
        required authorization token -- is proved in ``test_options_transmit.py``.
        What is still worth pinning here is that no *other* module has grown one.
        """
        offenders: list[str] = []
        for path in options_modules():
            if path.name == "transmit.py":
                continue
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in MUTATING_ATTRIBUTES:
                    offenders.append(f"{path.name}:{node.lineno} .{func.attr}()")
                elif isinstance(func, ast.Name) and func.id in MUTATING_ATTRIBUTES:
                    offenders.append(f"{path.name}:{node.lineno} {func.id}()")
        assert offenders == [], f"a non-chokepoint module can transmit: {offenders}"

    def test_no_module_except_transmit_assigns_a_transmit_flag(self) -> None:
        """``order.transmit = True`` is how an ib_async order is armed. Only the
        chokepoint may arm one; ``build_combo`` still leaves the flag alone."""
        offenders: list[str] = []
        for path in options_modules():
            if path.name == "transmit.py":
                continue
            for node in ast.walk(parse(path)):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "transmit":
                        offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], f"a non-chokepoint module arms an order: {offenders}"

    def test_the_combo_builder_still_does_not_arm(self) -> None:
        """``build_combo`` is called by the what-if path, which must never send.
        It leaves ``transmit`` at the library default and the chokepoint sets it
        explicitly, so an order can only be armed on the path that checked gates."""
        from engine.options.execution import build_combo

        _, order = build_combo(spread_for_combo())
        assert "transmit" not in vars(order) or order.transmit is not False

    def test_no_module_reaches_a_transmitting_method_by_getattr(self) -> None:
        """getattr(ib, "placeOrder") would defeat the attribute search above."""
        offenders: list[str] = []
        for path in options_modules():
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if node.value in MUTATING_ATTRIBUTES:
                    offenders.append(f"{path.name}:{node.lineno} {node.value!r}")
        assert offenders == [], f"a transmitting name appears as a string: {offenders}"

    def test_the_package_is_flat_or_the_walk_is_deep(self) -> None:
        """Pins the fact that made the old one-level glob adequate.

        The static checks now recurse, so this is no longer load-bearing for
        their correctness -- it is here to fail loudly the day a subpackage
        appears, as the review prompt to confirm the deep walk really reached
        it rather than to forbid the directory.
        """
        flat = {path for path in PACKAGE_DIR.glob("*.py")}
        deep = set(options_modules())
        assert flat <= deep, "the recursive walk lost a top-level module"
        subpackaged = sorted(str(p.relative_to(PACKAGE_DIR)) for p in deep - flat)
        assert subpackaged == [], (
            "engine.options grew a subpackage; the static checks below now cover "
            f"it via rglob -- confirm they still pass meaningfully for {subpackaged}"
        )

    def test_no_module_except_transmit_names_a_transmitting_attribute_at_all(
        self,
    ) -> None:
        """Not just in call position. ``send = ib.placeOrder`` then ``send(...)``.

        The sibling check above inspects ``Call.func``, so it sees
        ``ib.placeOrder(...)`` and nothing else. A bound method stashed in a
        variable, a dict value, a ``functools.partial`` or a decorator argument
        is an ``ast.Attribute`` in *load* position, which that check walks past.
        This one flags the name wherever it appears in real syntax -- the
        binding is the thing worth forbidding, because a name that is never
        bound can never be called.
        """
        offenders: list[str] = []
        for path in options_modules():
            if path.name == "transmit.py":
                continue
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Attribute) and node.attr in MUTATING_ATTRIBUTES:
                    offenders.append(f"{path.name}:{node.lineno} .{node.attr}")
                elif isinstance(node, ast.Name) and node.id in MUTATING_ATTRIBUTES:
                    offenders.append(f"{path.name}:{node.lineno} {node.id}")
        assert offenders == [], f"a transmitting name is bound outside the chokepoint: {offenders}"

    def test_exactly_one_transmitting_call_in_the_package_and_it_is_in_place_combo(
        self,
    ) -> None:
        """The whole claim, as one assertion: one sender, and it is the one we
        think it is. A second ``placeOrder`` inside ``transmit.py`` -- a retry
        path, a cancel-replace -- would satisfy every other check in this file
        and would be a second door."""
        from engine.options import transmit as transmit_module

        tree = parse(Path(transmit_module.__file__).resolve())
        sends = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "placeOrder"
        ]
        assert len(sends) == 1, f"transmit.py has {len(sends)} placeOrder calls"

        owners = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(inner is sends[0] for inner in ast.walk(node))
        ]
        assert owners == ["place_combo"], f"the send lives in {owners}"

    def test_exactly_one_cancelling_call_in_the_package_and_it_is_in_cancel_combo(
        self,
    ) -> None:
        """The same claim for the second broker-mutating verb.

        ``cancelOrder`` was added because a working combo that neither filled
        nor rejected could not be pulled programmatically -- the engine could
        get into a position it had no way out of. It gets the same shape as the
        send: one call site, in a named function, behind a token with no public
        constructor. A cancel appearing inside the reprice ladder, the runner or
        a retry helper would satisfy every other check in this file and would be
        a second door.
        """
        from engine.options import transmit as transmit_module

        tree = parse(Path(transmit_module.__file__).resolve())
        cancels = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in CANCELLING_ATTRIBUTES
        ]
        assert len(cancels) == 1, f"transmit.py has {len(cancels)} cancelOrder calls"

        owners = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(inner is cancels[0] for inner in ast.walk(node))
        ]
        assert owners == ["cancel_combo"], f"the cancel lives in {owners}"

    def test_the_cancel_token_is_checked_before_the_cancel(self) -> None:
        """Ordering, as with the digest guard on the send.

        A ``CancelAuthorization`` check written *after* ``cancelOrder`` returned
        would pass a test that only asserted the check exists, while the request
        was already at the broker.
        """
        from engine.options import transmit as transmit_module

        tree = parse(Path(transmit_module.__file__).resolve())
        cancel_fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "cancel_combo"
        )
        cancel_line = next(
            node.lineno
            for node in ast.walk(cancel_fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in CANCELLING_ATTRIBUTES
        )
        guards = [
            node.lineno
            for node in ast.walk(cancel_fn)
            if isinstance(node, ast.If)
            for inner in ast.walk(node.test)
            if isinstance(inner, ast.Name) and inner.id == "CancelAuthorization"
        ]
        assert guards, "cancel_combo does not check the authorization type"
        assert max(guards) < cancel_line, (
            f"the token guard at {guards} does not precede the cancel at "
            f"{cancel_line}"
        )

    def test_only_the_two_authorize_functions_hold_the_authorization_key(self) -> None:
        """The private sentinel is what makes both tokens unforgeable.

        Four functions may name it -- the two that mint a
        ``TransmitAuthorization``, the one that mints a ``CancelAuthorization``,
        and the reprice minter -- plus the ``__post_init__`` checks that compare
        against it. Anything else naming it is a way to mint a token without
        running a gate.
        """
        from engine.options import transmit as transmit_module

        tree = parse(Path(transmit_module.__file__).resolve())
        holders: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and inner.id == "_AUTHORIZATION_KEY":
                    holders.add(node.name)
        assert holders == {
            "__post_init__",
            "authorize_open",
            "authorize_close",
            "authorize_cancel",
            "authorize_reprice",
        }, holders

    def test_the_structure_digest_is_checked_before_the_send(self) -> None:
        """Ordering, not merely presence.

        ``place_combo`` compares the digest of the intent it is about to send
        against the digest the authorization carries. A digest checked *after*
        ``placeOrder`` returned would still pass a test that only asserted the
        comparison exists, while the order was already at the broker.
        """
        from engine.options import transmit as transmit_module

        tree = parse(Path(transmit_module.__file__).resolve())
        combo = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "place_combo"
        )
        send_line = next(
            node.lineno
            for node in ast.walk(combo)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "placeOrder"
        )
        digest_guards = [
            node.lineno
            for node in ast.walk(combo)
            if isinstance(node, ast.If)
            for cmp_node in ast.walk(node.test)
            if isinstance(cmp_node, ast.Attribute) and cmp_node.attr == "digest"
        ]
        assert digest_guards, "place_combo does not compare the authorization digest"
        assert max(digest_guards) < send_line, (
            f"the digest guard at {digest_guards} does not precede the send at "
            f"{send_line} -- an order can be transmitted before it is matched "
            "against what was authorized"
        )

    def test_no_module_forwards_arbitrary_attributes(self) -> None:
        """``__getattr__`` on a class holding a raw ib client re-exposes every
        method the client has, ``placeOrder`` included, under a name no static
        check in this file would recognise. There is no such wrapper today and
        this is what keeps it that way."""
        offenders: list[str] = []
        for path in options_modules():
            for node in ast.walk(parse(path)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                    "__getattr__",
                    "__getattribute__",
                }:
                    offenders.append(f"{path.name}:{node.lineno} {node.name}")
        assert offenders == [], f"an attribute-forwarding wrapper exists: {offenders}"

    def test_no_module_imports_the_equity_broker(self) -> None:
        """engine.broker owns the only placeOrder in the tree. The options
        package reaching it would be the shortest route to an options order."""
        offenders: list[str] = []
        for path in options_modules():
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("broker"):
                    offenders.append(f"{path.name}:{node.lineno}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.endswith("broker"):
                            offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], f"options code imports the equity broker: {offenders}"


# ===========================================================================
# Structural: no port declares a way to transmit
# ===========================================================================


class TestPortsCannotTransmit:
    def test_no_port_protocol_declares_a_transmitting_method(self) -> None:
        """The ports are the whole vocabulary the strategy layers have for
        reaching the outside world. If none of them can send an order, the
        layers above the adapters have nothing to call."""
        forbidden = {"place", "place_order", "placeOrder", "submit", "transmit", "send"}
        for name in ports.__all__:
            port = getattr(ports, name)
            if not isinstance(port, type):
                continue
            declared = {
                attribute
                for attribute in vars(port)
                if not attribute.startswith("_")
            }
            overlap = declared & forbidden
            assert overlap == set(), f"{name} declares {overlap}"

    def test_the_lifecycle_sink_has_no_transmission_capability(self) -> None:
        """The sink is handed to code running *inside* the polling loop, where
        the authorization token is no longer in scope. If it could transmit,
        cancel, retry or construct an order, it would be a second door reachable
        from a context that has already passed the first one.

        Asserted against the protocol and both concrete implementations, so
        adding the capability anywhere fails here.
        """
        from engine.options.sink import (
            LifecycleRecorder,
            NullLifecycleSink,
            OrderLifecycleSink,
        )

        forbidden = {
            "place", "place_order", "placeOrder", "submit", "transmit", "send",
            "cancel", "retry", "authorize", "authorize_open", "authorize_close",
            "build_combo",
        }
        for owner in (OrderLifecycleSink, LifecycleRecorder, NullLifecycleSink):
            public = {name for name in dir(owner) if not name.startswith("_")}
            assert public & forbidden == set(), f"{owner.__name__}: {public & forbidden}"

        declared = {name for name in vars(OrderLifecycleSink) if not name.startswith("_")}
        assert declared == {"observe"}, declared

    def test_the_sink_module_contains_no_transmitting_call(self) -> None:
        """Structural, not merely by-name: the module that runs inside the poll
        loop must not reach the broker's order API at all."""
        from engine.options import sink as sink_module

        offenders: list[str] = []
        for node in ast.walk(parse(Path(sink_module.__file__).resolve())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in MUTATING_ATTRIBUTES:
                    offenders.append(f"sink.py:{node.lineno} .{node.func.attr}()")
        assert offenders == [], offenders

    def test_the_what_if_port_is_the_only_broker_order_surface(self) -> None:
        """whatIfOrder asks what an order would cost. It is the non-transmitting
        half of the order API, and it is deliberately all that is exposed."""
        assert hasattr(ports.BrokerWhatIfPort, "what_if")
        assert not hasattr(ports.BrokerWhatIfPort, "place")


# ===========================================================================
# CLI: no options command can be armed
# ===========================================================================


class TestOptionsCliIsNotArmable:
    @pytest.mark.parametrize(
        "command", ["options-scan", "probe-options-data", "options-mark"]
    )
    def test_options_commands_reject_arm(self, command: str) -> None:
        """--arm is the flag that transmits. An options command accepting it --
        even if it currently ignored it -- is the affordance this milestone must
        not create."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([command, "--arm"])

    def test_the_equity_trade_command_still_accepts_arm(self) -> None:
        """The inverse assertion. A change that disarmed equity trading while
        making this file pass would be a regression these tests exist to catch,
        not a success."""
        parser = build_parser()
        args = parser.parse_args(["trade", "--symbol", "SPY", "--qty", "1", "--arm"])
        assert args.arm is True

    def test_no_options_command_handler_calls_a_transmitting_method(self) -> None:
        """Checked in the CLI itself, because the AST sweep above only covers
        the options package -- the handler that wires it lives in cli.py."""
        import engine.cli as cli_module

        tree = parse(Path(cli_module.__file__).resolve())
        handlers = {"cmd_options_scan", "cmd_probe_options_data", "cmd_options_mark"}
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in handlers:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    if inner.func.attr in MUTATING_ATTRIBUTES:
                        offenders.append(f"{node.name}:{inner.lineno} .{inner.func.attr}()")
        assert offenders == [], f"an options CLI handler transmits: {offenders}"


# ===========================================================================
# Behavioural: run the scan and prove nothing was placed
# ===========================================================================


class RecordingIB:
    """A fake IB that answers the scan's reads and records any transmission.

    ``placeOrder`` is implemented rather than omitted on purpose. Leaving it off
    would make an attempted call raise ``AttributeError``, and a test that passes
    because of an AttributeError proves the fake is incomplete, not that the code
    is safe.
    """

    def __init__(self) -> None:
        self.placed: list[tuple[object, object]] = []
        self.what_ifs: list[tuple[object, object]] = []
        self.errorEvent = _Event()  # noqa: N815 - ib_async's spelling

    # -- reads the scan performs -----------------------------------------

    def qualifyContracts(self, *contracts: object) -> list[object]:  # noqa: N802
        qualified = []
        for index, contract in enumerate(contracts, start=1):
            if getattr(contract, "secType", "") == "STK":
                qualified.append(_Qualified(con_id=9000, strike=0.0, right=""))
                continue
            qualified.append(
                _Qualified(
                    con_id=1000 + index,
                    strike=float(getattr(contract, "strike", 0.0)),
                    right=getattr(contract, "right", "P"),
                )
            )
        return qualified

    def reqHistoricalData(self, *_args: object, **_kwargs: object) -> list[object]:  # noqa: N802
        start = dt.date(2025, 8, 1)
        return [
            _Bar(start + dt.timedelta(days=index), 0.10 + (index % 40) * 0.005)
            for index in range(260)
        ]

    def reqSecDefOptParams(self, *_args: object, **_kwargs: object) -> list[object]:  # noqa: N802
        return [_Chain()]

    def reqContractDetails(self, contract: object) -> list[object]:  # noqa: N802
        # Wide enough that the shadow selector can step a short offset and a
        # wing width off the at-the-money anchor without running off the end --
        # a chain too narrow to build a spread would abort the scan before the
        # what-if, and this test would then prove nothing.
        return [_Detail(float(strike)) for strike in range(400, 601, 5)]

    def whatIfOrder(self, contract: object, order: object) -> object:  # noqa: N802
        self.what_ifs.append((contract, order))
        return _OrderState()

    # -- the thing that must never be called -----------------------------

    def placeOrder(self, contract: object, order: object) -> object:  # noqa: N802
        self.placed.append((contract, order))
        raise AssertionError("the options scan transmitted an order")


class _Event:
    def __iadd__(self, _handler: object) -> "_Event":
        return self

    def __isub__(self, _handler: object) -> "_Event":
        return self


class _Qualified:
    def __init__(self, *, con_id: int, strike: float, right: str) -> None:
        self.conId = con_id  # noqa: N815
        self.strike = strike
        self.right = right
        self.multiplier = "100"
        self.exchange = "SMART"
        self.tradingClass = "SPY"  # noqa: N815


class _Bar:
    def __init__(self, when: dt.date, close: float) -> None:
        self.date = when
        self.close = close


class _Chain:
    def __init__(self) -> None:
        self.strikes = [float(s) for s in range(400, 601, 5)]
        self.expirations = [
            (dt.date.today() + dt.timedelta(days=days)).strftime("%Y%m%d")
            for days in (14, 45, 80)
        ]


class _Detail:
    def __init__(self, strike: float) -> None:
        self.contract = _Qualified(con_id=int(strike), strike=strike, right="P")


class _OrderState:
    initMarginChange = 500.0  # noqa: N815
    maintMarginChange = 500.0  # noqa: N815
    equityWithLoanChange = -2.0  # noqa: N815
    commission = 1.30
    warningText = ""  # noqa: N815


class _Quote:
    price = 500.0
    source = "delayed"


class RecordingBroker:
    def __init__(self) -> None:
        self.ib = RecordingIB()

    def quote(self, _symbol: str) -> _Quote:
        return _Quote()


class TestScanTransmitsNothing:
    def test_a_full_scan_places_no_order(self) -> None:
        """End to end, through chain discovery, the what-if, the risk checks and
        the governor. The what-if runs -- proving the path was really exercised
        and not aborted early -- and placeOrder is never reached."""
        broker = RecordingBroker()
        report = run_scan(broker, symbol="SPY", account="DU1234567")

        assert broker.ib.placed == []
        assert broker.ib.what_ifs, (
            "the what-if never ran, so this test proved nothing about the path "
            f"that follows it; scan errors were {report.errors}"
        )

    def test_a_scan_with_no_ports_is_never_tradeable(self) -> None:
        """The default call supplies no market-data port and no portfolio port.
        Both fail closed, so the answer must be NO with explicit codes."""
        report = run_scan(RecordingBroker(), symbol="SPY", account="DU1234567")

        assert report.tradeable is False
        assert report.selection_method is SelectionMethod.SHADOW_STRIKE_OFFSET
        assert "OPTIONS_NO_MARKET_DATA_SNAPSHOT" in report.refusal_codes
        assert "GOVERNOR_PORTFOLIO_STATE_UNAVAILABLE" in report.refusal_codes
