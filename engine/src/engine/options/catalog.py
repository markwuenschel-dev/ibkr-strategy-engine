"""Versioned, hashable universe catalog primitives.

The strategy seed table predates unattended operation and intentionally knows
only about symbols and coarse concentration labels.  A worker needs a more
explicit contract: what is being scanned, where it is listed, whether it is
optionable, and whether the catalog itself is the artifact the operator
approved.  This module adds that contract without changing the seed table or
requiring the scanner to know how a catalog was obtained.

Catalog entries are descriptive data.  ``entry_eligible`` is a catalog-level
allowance, not an order authorization; lease, risk, entitlement, reviewer and
freshness gates still have to pass before anything can be sent.  Unknown
symbols are deliberately represented as scan-only entries.  That makes them
visible to discovery and diagnostics while making an accidental promotion to
an automated entry impossible at this layer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .universe_data import UNIVERSE_VERSION, UniverseEntry, augment, seed_universe

__all__ = [
    "CATALOG_SCHEMA",
    "BrokerContractIdentity",
    "CatalogEntry",
    "CatalogSnapshot",
    "UniverseCatalog",
]

CATALOG_SCHEMA = "ibkr.universe-catalog/1"


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return _freeze_value(value or {})


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class BrokerContractIdentity:
    """Optional broker identity recorded by a catalog importer.

    A catalog may be useful before qualification has happened, so every field
    is optional except the symbol.  The identity is advisory and is never
    treated as proof that the current broker contract is still valid.
    """

    symbol: str
    con_id: int | None = None
    exchange: str | None = None
    primary_exchange: str | None = None
    local_symbol: str | None = None
    trading_class: str | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("broker contract identity must name a symbol")
        object.__setattr__(self, "symbol", symbol)
        if self.con_id is not None and self.con_id <= 0:
            raise ValueError("broker contract con_id must be positive")

    def to_record(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "con_id": self.con_id,
            "exchange": self.exchange,
            "primary_exchange": self.primary_exchange,
            "local_symbol": self.local_symbol,
            "trading_class": self.trading_class,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "BrokerContractIdentity":
        raw_con_id = record.get("con_id")
        return cls(
            symbol=str(record["symbol"]),
            con_id=int(raw_con_id) if raw_con_id is not None else None,
            exchange=str(record["exchange"]) if record.get("exchange") else None,
            primary_exchange=(
                str(record["primary_exchange"])
                if record.get("primary_exchange")
                else None
            ),
            local_symbol=(
                str(record["local_symbol"]) if record.get("local_symbol") else None
            ),
            trading_class=(
                str(record["trading_class"])
                if record.get("trading_class")
                else None
            ),
        )


@dataclass(frozen=True)
class CatalogEntry:
    """One immutable catalog row.

    ``optionability`` is nullable because an importer may not have qualified
    the symbol yet.  Nullable is important: unknown is not the same as false,
    and both must be prevented from automated entry until a later gate proves
    the fact.
    """

    symbol: str
    security_type: str = "STK"
    listing_venue: str = "UNKNOWN"
    currency: str = "USD"
    active: bool = True
    optionability: bool | None = None
    sector: str | None = None
    correlation_group: str | None = None
    scan_eligible: bool = True
    entry_eligible: bool = False
    entitlement: Mapping[str, Any] = field(default_factory=dict)
    broker_contract: BrokerContractIdentity | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("catalog entries must name a symbol")
        if symbol != self.symbol:
            raise ValueError(f"catalog symbols are stored uppercase; got {self.symbol!r}")
        for name in ("security_type", "listing_venue", "currency"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{symbol}: {name} must be non-empty")
        if self.entry_eligible and not self.classified:
            raise ValueError(
                f"{symbol}: an unclassified symbol cannot be entry eligible"
            )
        if self.entry_eligible and not self.active:
            raise ValueError(f"{symbol}: an inactive symbol cannot be entry eligible")
        if self.entry_eligible and not self.scan_eligible:
            raise ValueError(f"{symbol}: an entry-ineligible scan row is contradictory")
        if self.broker_contract is not None and self.broker_contract.symbol != symbol:
            raise ValueError(f"{symbol}: broker identity names another symbol")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "security_type", self.security_type.strip().upper())
        object.__setattr__(self, "listing_venue", self.listing_venue.strip().upper())
        object.__setattr__(self, "currency", self.currency.strip().upper())
        object.__setattr__(self, "entitlement", _freeze_mapping(self.entitlement))

    @property
    def classified(self) -> bool:
        return bool(self.sector and self.correlation_group)

    @property
    def automated_entry_allowed(self) -> bool:
        """Static catalog eligibility; runtime safety gates remain separate."""
        return bool(
            self.active
            and self.scan_eligible
            and self.entry_eligible
            and self.optionability is True
            and self.classified
        )

    @property
    def entitlement_metadata(self) -> Mapping[str, Any]:
        """Compatibility spelling used by operator-facing serializers."""
        return self.entitlement

    def to_record(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "security_type": self.security_type,
            "listing_venue": self.listing_venue,
            "currency": self.currency,
            "active": self.active,
            "optionability": self.optionability,
            "sector": self.sector,
            "correlation_group": self.correlation_group,
            "scan_eligible": self.scan_eligible,
            "entry_eligible": self.entry_eligible,
            "entitlement": _jsonable(self.entitlement),
            "broker_contract": (
                self.broker_contract.to_record() if self.broker_contract else None
            ),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "CatalogEntry":
        raw_contract = record.get("broker_contract", record.get("broker_contract_identity"))
        contract = (
            BrokerContractIdentity.from_record(raw_contract)
            if isinstance(raw_contract, Mapping)
            else None
        )
        raw_entitlement = record.get("entitlement", record.get("entitlement_metadata", {}))
        if not isinstance(raw_entitlement, Mapping):
            raise ValueError("catalog entitlement metadata must be an object")
        return cls(
            symbol=str(record["symbol"]),
            security_type=str(record.get("security_type", "STK")),
            listing_venue=str(record.get("listing_venue", "UNKNOWN")),
            currency=str(record.get("currency", "USD")),
            active=bool(record.get("active", True)),
            optionability=(
                bool(record["optionability"])
                if record.get("optionability") is not None
                else None
            ),
            sector=(str(record["sector"]) if record.get("sector") is not None else None),
            correlation_group=(
                str(record["correlation_group"])
                if record.get("correlation_group") is not None
                else None
            ),
            scan_eligible=bool(record.get("scan_eligible", True)),
            entry_eligible=bool(record.get("entry_eligible", False)),
            entitlement=raw_entitlement,
            broker_contract=contract,
        )


@dataclass(frozen=True)
class CatalogSnapshot:
    """An immutable catalog manifest used by scans and approvals."""

    version: str
    entries: tuple[CatalogEntry, ...]
    source: str = "unknown"
    artifact_sha256: str | None = None
    schema: str = CATALOG_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CATALOG_SCHEMA:
            raise ValueError(f"unsupported catalog schema {self.schema!r}")
        if not self.version.strip():
            raise ValueError("catalog version must be non-empty")
        if not self.entries:
            raise ValueError("catalog cannot be empty")
        symbols = [entry.symbol for entry in self.entries]
        if len(symbols) != len(set(symbols)):
            raise ValueError("catalog contains duplicate symbols")
        if self.artifact_sha256 is not None:
            digest = self.artifact_sha256.lower().strip()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("catalog artifact_sha256 must be a 64-character hex digest")
            object.__setattr__(self, "artifact_sha256", digest)
        object.__setattr__(self, "entries", tuple(self.entries))

    @property
    def expected_count(self) -> int:
        return len(self.entries)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(entry.symbol for entry in self.entries)

    @property
    def catalog_version(self) -> str:
        """Explicit spelling for receipt and refresh-queue callers."""
        return self.version

    @property
    def digest(self) -> str:
        return _sha256_bytes(_canonical_bytes(self.to_manifest(include_artifact=False)))

    @property
    def catalog_hash(self) -> str:
        """Hash to put in a policy/fingerprint.

        A pinned artifact uses the exact operator-approved file hash.  A seed
        or programmatic snapshot uses its canonical manifest hash instead.
        """
        return self.artifact_sha256 or self.digest

    def entry(self, symbol: str) -> CatalogEntry | None:
        wanted = symbol.strip().upper()
        return next((entry for entry in self.entries if entry.symbol == wanted), None)

    def to_manifest(self, *, include_artifact: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "version": self.version,
            "source": self.source,
            "entries": [entry.to_record() for entry in self.entries],
        }
        if include_artifact and self.artifact_sha256:
            payload["artifact_sha256"] = self.artifact_sha256
        return payload

    def to_record(self) -> dict[str, Any]:
        return self.to_manifest()

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "CatalogSnapshot":
        entries = record.get("entries")
        if not isinstance(entries, list):
            raise ValueError("catalog artifact entries must be a list")
        return cls(
            schema=str(record.get("schema", CATALOG_SCHEMA)),
            version=str(record["version"]),
            source=str(record.get("source", "artifact")),
            artifact_sha256=(
                str(record["artifact_sha256"])
                if record.get("artifact_sha256")
                else None
            ),
            entries=tuple(
                CatalogEntry.from_record(entry)
                for entry in entries
                if isinstance(entry, Mapping)
            ),
        )


class UniverseCatalog:
    """Provider for a versioned catalog and its immutable snapshots."""

    def __init__(
        self,
        entries: Iterable[CatalogEntry | UniverseEntry] | None = None,
        *,
        version: str = UNIVERSE_VERSION,
        source: str = "programmatic",
        artifact_sha256: str | None = None,
    ) -> None:
        if entries is None:
            entries = seed_universe()
        normalized: list[CatalogEntry] = []
        for entry in entries:
            if isinstance(entry, CatalogEntry):
                normalized.append(entry)
            elif isinstance(entry, UniverseEntry):
                normalized.append(
                    self._from_seed_entry(entry)
                    if entry.classified
                    else self._from_unknown_entry(entry)
                )
            else:
                raise TypeError("catalog entries must be CatalogEntry or UniverseEntry")
        self._snapshot = CatalogSnapshot(
            version=version,
            entries=tuple(normalized),
            source=source,
            artifact_sha256=artifact_sha256,
        )

    @classmethod
    def from_seed(cls, extra_symbols: Iterable[str] = ()) -> "UniverseCatalog":
        """Wrap the existing 80-symbol seed without changing its order/data."""
        entries = [cls._from_seed_entry(entry) for entry in seed_universe()]
        known = {entry.symbol for entry in entries}
        for entry in augment(seed_universe(), extra_symbols):
            if entry.symbol in known:
                continue
            entries.append(cls._from_unknown_entry(entry))
            known.add(entry.symbol)
        return cls(entries, version=UNIVERSE_VERSION, source="seed")

    @staticmethod
    def _from_seed_entry(entry: UniverseEntry) -> CatalogEntry:
        return CatalogEntry(
            symbol=entry.symbol,
            security_type="STK",
            listing_venue="UNKNOWN",
            currency="USD",
            active=True,
            optionability=True,
            sector=entry.sector,
            correlation_group=entry.correlation_group,
            scan_eligible=True,
            entry_eligible=True,
            entitlement={"readiness": "UNVERIFIED"},
        )

    @staticmethod
    def _from_unknown_entry(entry: UniverseEntry) -> CatalogEntry:
        return CatalogEntry(
            symbol=entry.symbol,
            optionability=None,
            sector=None,
            correlation_group=None,
            scan_eligible=True,
            entry_eligible=False,
            entitlement={"readiness": "UNCLASSIFIED"},
        )

    @classmethod
    def from_artifact(
        cls,
        path: Path,
        *,
        expected_sha256: str | None = None,
        expected_version: str | None = None,
    ) -> "UniverseCatalog":
        """Load an operator artifact and verify its byte hash before parsing."""
        raw = Path(path).read_bytes()
        actual = _sha256_bytes(raw)
        if expected_sha256 is not None and actual != expected_sha256.lower().strip():
            raise ValueError(
                f"catalog hash mismatch: expected {expected_sha256}, got {actual}"
            )
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, Mapping):
            raise ValueError("catalog artifact must contain a JSON object")
        snapshot = CatalogSnapshot.from_record(decoded)
        if expected_version is not None and snapshot.version != expected_version:
            raise ValueError(
                f"catalog version mismatch: expected {expected_version}, got {snapshot.version}"
            )
        return cls(
            snapshot.entries,
            version=snapshot.version,
            source=str(path),
            artifact_sha256=actual,
        )

    load = from_artifact
    from_file = from_artifact
    from_json = from_artifact

    def snapshot(self) -> CatalogSnapshot:
        return self._snapshot

    @property
    def version(self) -> str:
        return self._snapshot.version

    @property
    def catalog_hash(self) -> str:
        return self._snapshot.catalog_hash

    @property
    def catalog_version(self) -> str:
        return self._snapshot.version

    @property
    def entries(self) -> tuple[CatalogEntry, ...]:
        return self._snapshot.entries

    def entry(self, symbol: str) -> CatalogEntry | None:
        return self._snapshot.entry(symbol)

    def eligible_for_scan(self) -> tuple[CatalogEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.active and entry.scan_eligible
        )

    def eligible_for_entry(self) -> tuple[CatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.automated_entry_allowed)

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)
