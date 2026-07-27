"""Per-order safety gates.

:mod:`engine.config` decides *which broker* the engine may talk to. This module
decides *whether a particular order may go*. Every gate here answers "no" by
raising; there is no boolean return that a caller can forget to check.

Order of checks is cheapest-and-most-absolute first, so a halted engine spends
no time pricing an order it was never going to send:

1. kill switch      -- a file exists; nothing else matters
2. armed            -- dry-run is the default, always
3. symbol allowlist -- typo protection, and a hard scope limit
4. quantity sanity  -- positive whole number
5. position cap     -- this order plus what is already held
6. notional cap     -- needs a reference price
7. daily order cap  -- counted from the journal, so a crash loop cannot evade it
8. margin impact    -- checked separately, after ``whatIfOrder`` (see gate_margin)

Gate 8 is deliberately not in the same call: it needs a round trip to the broker,
and everything above it must have already passed before the engine asks the
broker anything about an order it intends to place.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import EngineConfig
from .errors import HaltedError, RefusedError
from .journal import OrderJournal

BUY = "BUY"
SELL = "SELL"
SIDES = (BUY, SELL)


@dataclass(frozen=True)
class OrderIntent:
    """What the caller wants to do, before any broker has been consulted."""

    symbol: str
    quantity: int
    side: str = BUY
    limit_price: float | None = None

    def normalized(self) -> "OrderIntent":
        return OrderIntent(
            symbol=self.symbol.strip().upper(),
            quantity=self.quantity,
            side=self.side.strip().upper(),
            limit_price=self.limit_price,
        )

    def describe(self) -> str:
        price = f" @ {self.limit_price}" if self.limit_price is not None else " @ market"
        return f"{self.side} {self.quantity} {self.symbol}{price}"


class SafetyGate:
    """Applies the gates. Construct once per run; call :meth:`check` per order."""

    def __init__(self, config: EngineConfig, journal: OrderJournal) -> None:
        self.config = config
        self.journal = journal

    # -- individual gates ------------------------------------------------

    def assert_not_halted(self) -> None:
        """The kill switch: a file whose mere existence stops everything.

        A file rather than a signal or an API call because it works when the
        engine is wedged, needs no IPC, survives a restart, and can be engaged
        from a phone over SSH by anyone who can type ``touch``.
        """
        halt = self.config.halt_file
        if halt.exists():
            try:
                reason = halt.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                reason = ""
            raise HaltedError(
                f"halted by {halt}" + (f": {reason}" if reason else ""),
                hint=f"delete {halt} to resume",
            )

    def gate_armed(self, *, armed: bool) -> None:
        if not armed:
            raise RefusedError(
                "not armed -- no order was placed",
                hint="this is the default. Pass --arm to actually transmit an order.",
            )

    def gate_symbol(self, intent: OrderIntent) -> None:
        if intent.symbol not in self.config.symbol_allowlist:
            raise RefusedError(
                f"{intent.symbol} is not in the symbol allowlist",
                hint=f"allowed: {', '.join(self.config.symbol_allowlist)}",
            )

    def gate_quantity(self, intent: OrderIntent) -> None:
        if not isinstance(intent.quantity, int) or isinstance(intent.quantity, bool):
            raise RefusedError(f"quantity must be a whole number, got {intent.quantity!r}")
        if intent.quantity <= 0:
            raise RefusedError(
                f"quantity must be positive, got {intent.quantity}",
                hint="to sell, use --side SELL; a negative quantity is not how that is expressed",
            )
        if intent.side not in SIDES:
            raise RefusedError(f"side must be one of {', '.join(SIDES)}, got {intent.side!r}")

    def gate_position(self, intent: OrderIntent, *, current_qty: int = 0) -> None:
        """Cap the resulting position, not just the order.

        Ten one-share orders reach the same place as one ten-share order, so the
        cap has to be applied to the destination.
        """
        signed = intent.quantity if intent.side == BUY else -intent.quantity
        resulting = abs(current_qty + signed)
        if resulting > self.config.max_position_qty:
            raise RefusedError(
                f"{intent.symbol} position would become {resulting}, "
                f"over the cap of {self.config.max_position_qty}",
                hint=f"currently holding {current_qty}",
            )

    def gate_notional(self, intent: OrderIntent, *, reference_price: float | None) -> None:
        if reference_price is None:
            raise RefusedError(
                f"no reference price for {intent.symbol}; cannot size the order against the cap",
                hint=(
                    "the engine refuses to place an order whose value it cannot bound. "
                    "Check market data is available (see `engine quote`)."
                ),
            )
        if reference_price <= 0:
            raise RefusedError(
                f"reference price for {intent.symbol} is {reference_price}, which is not usable"
            )
        notional = reference_price * intent.quantity
        if notional > self.config.max_order_notional:
            raise RefusedError(
                f"order notional {notional:,.2f} exceeds the cap of "
                f"{self.config.max_order_notional:,.2f}",
                hint=f"{intent.quantity} x {reference_price:,.2f}",
            )

    def gate_daily_count(self) -> None:
        placed = self.journal.orders_today()
        if placed >= self.config.max_orders_per_session:
            raise RefusedError(
                f"{placed} orders already placed today, at the cap of "
                f"{self.config.max_orders_per_session}",
                hint=(
                    "counted from the journal on disk, so restarting the engine does not "
                    "reset it. Raise IBKR_MAX_ORDERS_PER_SESSION deliberately if this is wrong."
                ),
            )

    def gate_margin(self, *, margin_impact: float | None) -> None:
        """Applied after ``whatIfOrder``, before transmitting.

        ``None`` means the broker did not tell us. That is treated as a refusal
        rather than a pass: an unknown margin impact is not a small one.
        """
        if margin_impact is None:
            raise RefusedError(
                "the broker returned no margin impact for this order",
                hint="refusing rather than assuming it is negligible",
            )
        if margin_impact > self.config.max_margin_impact:
            raise RefusedError(
                f"margin impact {margin_impact:,.2f} exceeds the cap of "
                f"{self.config.max_margin_impact:,.2f}"
            )

    # -- composite -------------------------------------------------------

    def check_preflight(self, intent: OrderIntent) -> OrderIntent:
        """The gates that need no broker. **Call these before connecting.**

        Everything here is answerable from local state, so it runs first: a
        halted engine must not open a socket to the broker at all, and a typo in
        a symbol should not cost a connection to discover.
        """
        self.assert_not_halted()
        normalized = intent.normalized()
        self.gate_quantity(normalized)
        self.gate_symbol(normalized)
        return normalized

    def check_tradeable(
        self,
        intent: OrderIntent,
        *,
        reference_price: float | None,
        current_qty: int = 0,
    ) -> OrderIntent:
        """The gates that need a price and a position, i.e. a live connection."""
        normalized = intent.normalized()
        self.gate_position(normalized, current_qty=current_qty)
        self.gate_notional(normalized, reference_price=reference_price)
        self.gate_daily_count()
        return normalized

    def check(
        self,
        intent: OrderIntent,
        *,
        armed: bool,
        reference_price: float | None = None,
        current_qty: int = 0,
    ) -> OrderIntent:
        """Every pre-transmit gate at once, for programmatic callers.

        The CLI does **not** use this: it interleaves the two halves around the
        broker connection so the local gates run first. See
        :func:`engine.cli.cmd_trade`.
        """
        normalized = self.check_preflight(intent)
        self.gate_armed(armed=armed)
        return self.check_tradeable(
            normalized, reference_price=reference_price, current_qty=current_qty
        )

    def check_preview(self, intent: OrderIntent) -> OrderIntent:
        """The subset that applies to a preview, which transmits nothing.

        Notably skips :meth:`gate_armed` -- previewing is the safe thing we
        actively want people to do -- but still honours the kill switch, because
        a halted engine should not be talking to the broker at all.
        """
        self.assert_not_halted()
        normalized = intent.normalized()
        self.gate_quantity(normalized)
        self.gate_symbol(normalized)
        return normalized
