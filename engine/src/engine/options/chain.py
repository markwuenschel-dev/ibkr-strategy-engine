"""Option-chain discovery: expiry selection, strike enumeration, qualification.

Runs today with no market-data subscription — chain enumeration returned 34
expirations across 489 strikes on the delayed-only paper account.

Two IBKR traps are handled here rather than left for a caller to rediscover:

**Never take strikes from the chain's strike list.** ``reqSecDefOptParams``
returns the *union* of strikes across every expiration, so a strike from it
frequently does not exist for the expiry you picked and fails qualification with
error 200. Strikes come from ``reqContractDetails`` for one expiration.

**Never pick the chain by ``exchange == "SMART"``.** That can match a degenerate
entry carrying a handful of strikes. The chain with the most strikes is the real
one.

The lifecycle is explicit because each step can fail for a different reason and
"it qualified" is routinely mistaken for "it can be traded". A contract becomes
:attr:`ContractStatus.STRATEGY_ELIGIBLE` only after live greeks and liquidity
pass, which is a later gate than anything in this module.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

__all__ = [
    "ContractStatus",
    "ExpirySelection",
    "QualifiedOption",
    "select_expiration",
    "discover_expirations",
    "enumerate_strikes",
    "qualify_strikes",
    "narrow_strikes",
]


class ContractStatus(str, Enum):
    """How far a contract has got. Qualifying is not eligibility."""

    DISCOVERED = "DISCOVERED"
    QUALIFIED = "QUALIFIED"
    QUOTED = "QUOTED"
    GREEKS_VALID = "GREEKS_VALID"
    STRATEGY_ELIGIBLE = "STRATEGY_ELIGIBLE"


@dataclass(frozen=True)
class ExpirySelection:
    expiry: str  # IBKR's YYYYMMDD form
    expiration: dt.date
    dte: int
    considered: int
    in_window: int

    def describe(self) -> str:
        return (
            f"{self.expiration}  {self.dte} DTE "
            f"({self.in_window} of {self.considered} expirations in window)"
        )


@dataclass(frozen=True)
class QualifiedOption:
    """A contract IBKR has confirmed, carrying the fields the domain needs.

    ``multiplier`` and ``trading_class`` come from the qualified contract rather
    than being assumed, which is the whole point of qualifying before building.
    """

    con_id: int
    symbol: str
    expiration: dt.date
    strike: Decimal
    right: str
    multiplier: int
    exchange: str
    trading_class: str | None
    status: ContractStatus = ContractStatus.QUALIFIED


def select_expiration(
    expirations: Sequence[str],
    *,
    today: dt.date,
    target_dte: int = 45,
    minimum_dte: int = 35,
    maximum_dte: int = 55,
) -> ExpirySelection | None:
    """The expiration nearest the target, within the window. ``None`` if none.

    Ties break toward the longer-dated expiry: at equal distance from 45 the
    further one decays more slowly, so there is more room before the 21-DTE
    management point.
    """
    candidates: list[tuple[int, str]] = []
    for raw in expirations:
        try:
            when = dt.datetime.strptime(raw, "%Y%m%d").date()
        except (ValueError, TypeError):
            continue
        dte = (when - today).days
        if minimum_dte <= dte <= maximum_dte:
            candidates.append((dte, raw))

    if not candidates:
        return None

    dte, raw = min(candidates, key=lambda c: (abs(c[0] - target_dte), -c[0]))
    return ExpirySelection(
        expiry=raw,
        expiration=dt.datetime.strptime(raw, "%Y%m%d").date(),
        dte=dte,
        considered=len(expirations),
        in_window=len(candidates),
    )


def discover_expirations(ib: Any, symbol: str, underlying_con_id: int) -> list[str]:
    """Every expiration IBKR lists, from the richest chain."""
    chains = ib.reqSecDefOptParams(symbol, "", "STK", underlying_con_id)
    if not chains:
        return []
    chain = max(chains, key=lambda c: len(getattr(c, "strikes", ()) or ()))
    return sorted(getattr(chain, "expirations", ()) or ())


def enumerate_strikes(ib: Any, symbol: str, expiry: str, right: str = "P") -> list[Decimal]:
    """Strikes actually listed for THIS expiration.

    Deliberately not the chain's strike list, which is the union across all
    expirations and will hand back strikes that do not exist here.
    """
    from ib_async import Option  # noqa: PLC0415 - optional dependency

    details = ib.reqContractDetails(Option(symbol, expiry, 0, right, "SMART", currency="USD"))
    strikes: set[Decimal] = set()
    for detail in details:
        contract = getattr(detail, "contract", None)
        strike = getattr(contract, "strike", None)
        if strike:
            strikes.add(Decimal(str(strike)))
    return sorted(strikes)


def narrow_strikes(
    strikes: Sequence[Decimal],
    *,
    reference_price: Decimal | None,
    width: int,
) -> list[Decimal]:
    """Cut the universe down before subscribing to quotes.

    The reference price is used **only** to choose which strikes to look at.
    Final selection is delta-based; picking strikes by distance from spot would
    be a different strategy wearing this one's name.
    """
    if not strikes:
        return []
    if reference_price is None:
        middle = len(strikes) // 2
    else:
        middle = min(range(len(strikes)), key=lambda i: abs(strikes[i] - reference_price))
    half = max(1, width // 2)
    return list(strikes[max(0, middle - half) : middle + half + 1])


def qualify_strikes(
    ib: Any,
    symbol: str,
    expiry: str,
    strikes: Sequence[Decimal],
    right: str,
) -> list[QualifiedOption]:
    """Qualify each candidate and read its real multiplier and trading class.

    Deduplicated by contract id: IBKR can return the same contract for more than
    one request, and a duplicated leg would break the domain's distinct-contract
    invariant much later and much less clearly.
    """
    from ib_async import Option  # noqa: PLC0415 - optional dependency

    if not strikes:
        return []

    requested = [
        Option(symbol, expiry, float(strike), right, "SMART", currency="USD")
        for strike in strikes
    ]
    qualified = ib.qualifyContracts(*requested)

    seen: set[int] = set()
    results: list[QualifiedOption] = []
    for contract in qualified:
        con_id = getattr(contract, "conId", 0)
        if not con_id or con_id in seen:
            continue
        seen.add(con_id)
        raw_multiplier = getattr(contract, "multiplier", "") or ""
        try:
            multiplier = int(float(raw_multiplier))
        except (TypeError, ValueError):
            # No multiplier means the contract is not usable: the domain refuses
            # to assume 100, and this is where that refusal has to start.
            continue
        if multiplier <= 0:
            continue
        results.append(
            QualifiedOption(
                con_id=con_id,
                symbol=symbol,
                expiration=dt.datetime.strptime(expiry, "%Y%m%d").date(),
                strike=Decimal(str(getattr(contract, "strike", 0))),
                right=getattr(contract, "right", right),
                multiplier=multiplier,
                exchange=getattr(contract, "exchange", "SMART") or "SMART",
                trading_class=getattr(contract, "tradingClass", None) or None,
            )
        )
    results.sort(key=lambda o: o.strike)
    return results
