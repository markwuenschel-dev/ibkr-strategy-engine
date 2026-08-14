"""Freshness classes: what may be cached, for how long, and what never counts.

"Second scan of a day = zero broker requests" was the cache's original sales
pitch, and it is wrong for market observations: a quote from this morning is
not a quote. The correction (2026-08-01 audit) splits everything cacheable
into three explicit classes:

``SESSION_METADATA``
    Facts about *contracts*, not markets: identity (conId), the expiration
    catalog, multiplier, standard/nonstandard status, sector/correlation
    classification. Stable within a trading session by construction --
    exchanges do not relist option classes intraday -- so these are cached
    for the session and keyed to its date.

``SLOW_OBSERVATION``
    Market facts that move on hours-to-days scales: open interest (updated
    overnight by OCC), recent volume, realized-volatility history, the IV
    Rank input series. Cached with an explicit ``observed_at``/``expires_at``
    pair under configurable TTLs. Reused while unexpired; refreshed after.

``PERISHABLE``
    The market itself: underlying and option quotes, current IV, greeks,
    skew, term structure, midpoint/natural prices. **Never cached across
    passes.** A scan pass may hold them for ranking within itself; nothing
    perishable may feed a binding authorization once it is older than the
    quote-staleness policy the entitlement gate already enforces.

Every cached record carries an :class:`ObservationEnvelope` binding symbol,
session date, observed/expiry instants, source, market-data type,
subscription generation and configuration version -- the same discipline the
approval digest applies to orders, applied to data: a number whose provenance
cannot be stated cannot be reused.
"""

from __future__ import annotations

import datetime as dt
import contextlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from ..errors import ConfigError

__all__ = [
    "FreshnessClass",
    "ObservationEnvelope",
    "FreshnessPolicy",
    "SymbolSessionMetadata",
    "SessionMetadataStore",
]

ENV_PREFIX = "IBKR_OPTIONS_FRESHNESS_"

FRESHNESS_VERSION = "freshness/1"


def _utc(value: dt.datetime) -> dt.datetime:
    """Normalize aware instants at the value boundary.

    Comparing two aware datetimes with different timezone objects is legal but
    easy to misuse around DST transitions.  Persisting UTC here makes the
    cache's ordering and expiry comparisons unambiguous for every caller.
    """
    if value.tzinfo is None:
        raise ValueError("freshness instants must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


class FreshnessClass(str, Enum):
    SESSION_METADATA = "SESSION_METADATA"
    SLOW_OBSERVATION = "SLOW_OBSERVATION"
    PERISHABLE = "PERISHABLE"


@dataclass(frozen=True)
class ObservationEnvelope:
    """The provenance every cached record must carry to be reusable."""

    symbol: str
    session_date: dt.date
    observed_at: dt.datetime
    expires_at: dt.datetime
    source: str
    freshness_class: FreshnessClass
    configuration_version: str
    #: The reported market-data type when the record came off a live
    #: subscription; ``None`` for historical pulls, which have no type
    #: callback -- absence recorded, never invented.
    market_data_type: int | None = None
    #: The subscription generation for live-sourced records; a fetch-scoped
    #: UUID for historical pulls, so two fetches are distinguishable.
    subscription_generation: UUID | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("an envelope must name its symbol")
        observed_at = _utc(self.observed_at)
        expires_at = _utc(self.expires_at)
        if expires_at <= observed_at:
            raise ValueError(
                f"{symbol}: expires_at {expires_at.isoformat()} is not "
                f"after observed_at {observed_at.isoformat()}"
            )
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "expires_at", expires_at)

    def fresh(self, *, now: dt.datetime, session_date: dt.date) -> bool:
        """Whether this record may be *reused* right now.

        SESSION_METADATA lives and dies with its session date. Everything
        else lives until its expiry. PERISHABLE records additionally never
        outlive their session, whatever their expiry claims -- a generous
        TTL cannot resurrect yesterday's quote.
        """
        current = _utc(now)
        if self.freshness_class is FreshnessClass.SESSION_METADATA:
            return self.session_date == session_date
        if self.freshness_class is FreshnessClass.PERISHABLE:
            return self.session_date == session_date and current < self.expires_at
        return current < self.expires_at

    @property
    def cacheable(self) -> bool:
        """Whether this envelope may cross a pass boundary."""
        return self.freshness_class is not FreshnessClass.PERISHABLE

    def to_record(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "session_date": self.session_date.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "source": self.source,
            "freshness_class": self.freshness_class.value,
            "configuration_version": self.configuration_version,
            "market_data_type": self.market_data_type,
            "subscription_generation": (
                str(self.subscription_generation)
                if self.subscription_generation
                else None
            ),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ObservationEnvelope":
        raw_generation = record.get("subscription_generation")
        return cls(
            symbol=str(record["symbol"]),
            session_date=dt.date.fromisoformat(str(record["session_date"])),
            observed_at=dt.datetime.fromisoformat(str(record["observed_at"])),
            expires_at=dt.datetime.fromisoformat(str(record["expires_at"])),
            source=str(record["source"]),
            freshness_class=FreshnessClass(str(record["freshness_class"])),
            configuration_version=str(record["configuration_version"]),
            market_data_type=(
                int(record["market_data_type"])
                if record.get("market_data_type") is not None
                else None
            ),
            subscription_generation=UUID(raw_generation) if raw_generation else None,
        )


def _ttl(source: Mapping[str, str], key: str, default_seconds: Decimal) -> dt.timedelta:
    raw = (source.get(f"{ENV_PREFIX}{key}") or "").strip()
    if not raw:
        seconds = default_seconds
    else:
        try:
            seconds = Decimal(raw)
        except InvalidOperation:
            raise ConfigError(f"{ENV_PREFIX}{key}={raw!r} is not a number of seconds") from None
    if seconds <= 0:
        raise ConfigError(
            f"{ENV_PREFIX}{key} must be positive, got {seconds}",
            hint="a TTL of zero would expire every record at birth",
        )
    return dt.timedelta(seconds=float(seconds))


@dataclass(frozen=True)
class FreshnessPolicy:
    """Configurable TTLs for the slow class. Perishable takes none: its bound
    for authorization purposes is ``RiskPolicy.quote_maximum_age``, enforced
    where it always was (the entitlement gate), and no cache TTL may extend it.
    """

    #: The IV Rank input series and realized-vol history: refreshed daily.
    iv_history_ttl: dt.timedelta = dt.timedelta(hours=20)
    #: Open interest: OCC publishes overnight; intraday it barely moves.
    open_interest_ttl: dt.timedelta = dt.timedelta(hours=4)
    #: Recent volume: meaningful on the hour scale during a session.
    volume_ttl: dt.timedelta = dt.timedelta(hours=1)
    version: str = FRESHNESS_VERSION

    def ttl_for(
        self, freshness_class: FreshnessClass | str
    ) -> dt.timedelta | None:
        """Return the configured TTL, or ``None`` for perishable data.

        A perishable quote has no cache TTL by design.  Its usable lifetime is
        the live quote policy at the final decision door, not this cache.
        """
        if isinstance(freshness_class, str) and not isinstance(
            freshness_class, FreshnessClass
        ):
            normalized = freshness_class.strip().upper()
            if normalized in {"IV", "IV_HISTORY", "REALIZED_VOLATILITY"}:
                freshness_class = FreshnessClass.SLOW_OBSERVATION
            elif normalized in {"OI", "OPEN_INTEREST"}:
                return self.open_interest_ttl
            elif normalized in {"VOLUME", "RECENT_VOLUME"}:
                return self.volume_ttl
            else:
                with contextlib.suppress(ValueError):
                    freshness_class = FreshnessClass(normalized)
        if freshness_class is FreshnessClass.SESSION_METADATA:
            return None
        if freshness_class is FreshnessClass.SLOW_OBSERVATION:
            return self.iv_history_ttl
        return None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FreshnessPolicy":
        source: Mapping[str, str] = os.environ if env is None else env
        return cls(
            iv_history_ttl=_ttl(source, "IV_HISTORY_TTL_SECONDS", Decimal("72000")),
            open_interest_ttl=_ttl(source, "OPEN_INTEREST_TTL_SECONDS", Decimal("14400")),
            volume_ttl=_ttl(source, "VOLUME_TTL_SECONDS", Decimal("3600")),
        )


# ---------------------------------------------------------------------------
# session metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolSessionMetadata:
    """One symbol's contract facts for one session."""

    envelope: ObservationEnvelope
    con_id: int
    expirations: tuple[str, ...]
    multiplier: int
    standard: bool
    sector: str | None
    correlation_group: str | None

    def to_record(self) -> dict[str, Any]:
        return {
            "envelope": self.envelope.to_record(),
            "con_id": self.con_id,
            "expirations": list(self.expirations),
            "multiplier": self.multiplier,
            "standard": self.standard,
            "sector": self.sector,
            "correlation_group": self.correlation_group,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "SymbolSessionMetadata":
        return cls(
            envelope=ObservationEnvelope.from_record(record["envelope"]),
            con_id=int(record["con_id"]),
            expirations=tuple(str(e) for e in record.get("expirations", [])),
            multiplier=int(record["multiplier"]),
            standard=bool(record["standard"]),
            sector=record.get("sector"),
            correlation_group=record.get("correlation_group"),
        )


class SessionMetadataStore:
    """Per-session contract metadata, one JSON file per session date.

    Keyed by session date in the filename, so a stale file cannot masquerade
    as today's: reading always states which session it is reading for, and a
    record whose envelope disagrees with the file's own session is dropped.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, session_date: dt.date) -> Path:
        return self.root / f"metadata-{session_date.isoformat()}.json"

    def read(
        self, symbol: str, *, session_date: dt.date, now: dt.datetime
    ) -> SymbolSessionMetadata | None:
        """Today's cached metadata for ``symbol``, or ``None`` -- meaning fetch."""
        try:
            payload = json.loads(
                self._path(session_date).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        record = payload.get(symbol.strip().upper()) if isinstance(payload, dict) else None
        if not isinstance(record, dict):
            return None
        try:
            metadata = SymbolSessionMetadata.from_record(record)
        except (KeyError, ValueError, TypeError):
            return None
        if not metadata.envelope.fresh(now=now, session_date=session_date):
            return None
        return metadata

    def write(self, metadata: SymbolSessionMetadata) -> Path:
        session_date = metadata.envelope.session_date
        path = self._path(session_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                payload = {}
        except (OSError, ValueError):
            payload = {}
        payload[metadata.envelope.symbol.strip().upper()] = metadata.to_record()
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
        return path
