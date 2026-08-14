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
import math
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

_CATALOG_KEYS = frozenset(
    {"schema", "version", "source", "entries", "artifact_sha256"}
)
_CATALOG_REQUIRED_KEYS = frozenset({"schema", "version", "source", "entries"})
_CATALOG_ENTRY_KEYS = frozenset(
    {
        "symbol",
        "security_type",
        "listing_venue",
        "currency",
        "active",
        "optionability",
        "sector",
        "correlation_group",
        "scan_eligible",
        "entry_eligible",
        "entitlement",
        "broker_contract",
    }
)
_CATALOG_ENTRY_REQUIRED_KEYS = frozenset(
    {
        "symbol",
        "security_type",
        "listing_venue",
        "currency",
        "active",
        "optionability",
        "sector",
        "correlation_group",
        "scan_eligible",
        "entry_eligible",
        "entitlement",
    }
)
_BROKER_CONTRACT_KEYS = frozenset(
    {
        "symbol",
        "con_id",
        "exchange",
        "primary_exchange",
        "local_symbol",
        "trading_class",
    }
)
_BROKER_CONTRACT_REQUIRED_KEYS = frozenset({"symbol"})


def _format_keys(keys: Iterable[Any]) -> str:
    return ", ".join(repr(key) for key in sorted(keys, key=repr))


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _validate_keys(
    record: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    label: str,
) -> None:
    keys = set(record)
    missing = required - keys
    unknown = keys - allowed
    if missing:
        raise ValueError(f"{label} missing required keys: {_format_keys(missing)}")
    if unknown:
        raise ValueError(f"{label} contains unknown keys: {_format_keys(unknown)}")


def _require_string(value: Any, label: str, *, non_empty: bool = True) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    if non_empty and not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, label)


def _require_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _require_optional_bool(value: Any, label: str) -> bool | None:
    if value is None:
        return None
    return _require_bool(value, label)


def _validate_json_value(value: Any, label: str) -> None:
    """Reject non-JSON metadata instead of normalizing it into the artifact."""

    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} must not contain non-finite numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{label}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _require_string(key, f"{label} metadata key")
            _validate_json_value(item, f"{label}.{key}")
        return
    raise ValueError(f"{label} contains an unsupported value type")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"catalog artifact contains non-standard JSON constant {value}")


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
        record = _require_mapping(record, "broker_contract")
        _validate_keys(
            record,
            allowed=_BROKER_CONTRACT_KEYS,
            required=_BROKER_CONTRACT_REQUIRED_KEYS,
            label="broker_contract",
        )
        raw_con_id = record.get("con_id")
        if raw_con_id is not None and (
            type(raw_con_id) is not int or raw_con_id <= 0
        ):
            raise ValueError("broker_contract.con_id must be a positive integer or null")
        return cls(
            symbol=_require_string(record["symbol"], "broker_contract.symbol"),
            con_id=raw_con_id,
            exchange=_require_optional_string(
                record.get("exchange"), "broker_contract.exchange"
            ),
            primary_exchange=_require_optional_string(
                record.get("primary_exchange"), "broker_contract.primary_exchange"
            ),
            local_symbol=_require_optional_string(
                record.get("local_symbol"), "broker_contract.local_symbol"
            ),
            trading_class=_require_optional_string(
                record.get("trading_class"), "broker_contract.trading_class"
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
            and self.venue_verified
            and self.entitlement_allows_entry
        )

    @property
    def venue_verified(self) -> bool:
        """Whether the catalog identifies a usable listing venue.

        Venue-unknown rows remain in the scan catalog, but an automated entry
        cannot safely choose an entitlement or broker contract path for them.
        """

        return self.listing_venue not in {"", "UNKNOWN", "UNCLASSIFIED"}

    @property
    def entitlement_allows_entry(self) -> bool:
        """Honor explicit entitlement denials without inventing readiness.

        The seed catalog predates venue-level entitlement evidence and uses
        ``UNVERIFIED``.  That value remains visible and does not silently claim
        live readiness.  An importer or operator artifact can nevertheless
        make a hard negative authoritative with either ``entry_allowed: false``
        or a named denial status.  Runtime entitlement probes and the broker
        gate remain independent checks.
        """

        if self.entitlement.get("entry_allowed") is False:
            return False
        if self.entitlement.get("entry_allowed") is True:
            return True
        readiness = str(self.entitlement.get("readiness", "")).strip().upper()
        return readiness in {"VERIFIED", "LIVE", "READY", "ENTITLED", "SUBSCRIBED"}

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
        record = _require_mapping(record, "catalog entry")
        _validate_keys(
            record,
            allowed=_CATALOG_ENTRY_KEYS,
            required=_CATALOG_ENTRY_REQUIRED_KEYS,
            label="catalog entry",
        )
        raw_contract = record.get("broker_contract")
        if raw_contract is not None and not isinstance(raw_contract, Mapping):
            raise ValueError("catalog entry broker_contract must be an object or null")
        contract = (
            BrokerContractIdentity.from_record(raw_contract)
            if raw_contract is not None
            else None
        )
        raw_entitlement = _require_mapping(
            record["entitlement"], "catalog entry entitlement"
        )
        _validate_json_value(raw_entitlement, "catalog entry entitlement")
        return cls(
            symbol=_require_string(record["symbol"], "catalog entry symbol"),
            security_type=_require_string(
                record["security_type"], "catalog entry security_type"
            ),
            listing_venue=_require_string(
                record["listing_venue"], "catalog entry listing_venue"
            ),
            currency=_require_string(record["currency"], "catalog entry currency"),
            active=_require_bool(record["active"], "catalog entry active"),
            optionability=_require_optional_bool(
                record["optionability"], "catalog entry optionability"
            ),
            sector=_require_optional_string(record["sector"], "catalog entry sector"),
            correlation_group=_require_optional_string(
                record["correlation_group"], "catalog entry correlation_group"
            ),
            scan_eligible=_require_bool(
                record["scan_eligible"], "catalog entry scan_eligible"
            ),
            entry_eligible=_require_bool(
                record["entry_eligible"], "catalog entry entry_eligible"
            ),
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
        record = _require_mapping(record, "catalog artifact")
        _validate_keys(
            record,
            allowed=_CATALOG_KEYS,
            required=_CATALOG_REQUIRED_KEYS,
            label="catalog artifact",
        )
        schema = _require_string(record["schema"], "catalog artifact schema")
        version = _require_string(record["version"], "catalog artifact version")
        source = _require_string(record["source"], "catalog artifact source")
        entries = record["entries"]
        if type(entries) is not list:
            raise ValueError("catalog artifact entries must be a list")
        artifact_sha256 = None
        if "artifact_sha256" in record:
            artifact_sha256 = _require_string(
                record["artifact_sha256"], "catalog artifact artifact_sha256"
            )
        parsed_entries: list[CatalogEntry] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise ValueError(f"catalog entry at index {index} must be an object")
            try:
                parsed_entries.append(CatalogEntry.from_record(entry))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"catalog entry at index {index}: {exc}") from exc
        return cls(
            schema=schema,
            version=version,
            source=source,
            artifact_sha256=artifact_sha256,
            entries=tuple(parsed_entries),
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
        expected_sha256 = (
            _require_string(expected_sha256, "expected_sha256").lower().strip()
            if expected_sha256 is not None
            else None
        )
        if expected_sha256 is not None and actual != expected_sha256:
            raise ValueError(
                f"catalog hash mismatch: expected {expected_sha256}, got {actual}"
            )
        decoded = json.loads(
            raw.decode("utf-8"), parse_constant=_reject_json_constant
        )
        if not isinstance(decoded, Mapping):
            raise ValueError("catalog artifact must contain a JSON object")
        snapshot = CatalogSnapshot.from_record(decoded)
        if snapshot.artifact_sha256 is not None and snapshot.artifact_sha256 != actual:
            raise ValueError(
                "catalog artifact artifact_sha256 does not match the artifact bytes"
            )
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
