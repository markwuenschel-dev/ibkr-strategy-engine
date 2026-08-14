"""Catalog manifests and fail-closed unknown-symbol behavior."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from engine.options.catalog import CatalogEntry, CatalogSnapshot, UniverseCatalog
from engine.options.universe_data import UNIVERSE_VERSION

SEED_ARTIFACT_SHA256 = "f2035e99260fddf6d2ddf27c7cb0f05150ea25e8b27dedeb48dcf5e196693276"


def _catalog_manifest() -> dict[str, object]:
    return json.loads(
        json.dumps(
            UniverseCatalog.from_seed()
            .snapshot()
            .to_manifest(include_artifact=False)
        )
    )


def _write_catalog(tmp_path, manifest: dict[str, object]):
    artifact = tmp_path / "catalog.json"
    artifact.write_text(json.dumps(manifest), encoding="utf-8")
    return artifact


class TestUniverseCatalog:
    def test_seed_wraps_the_existing_eighty_without_reordering(self) -> None:
        catalog = UniverseCatalog.from_seed()
        assert len(catalog) == 80
        assert catalog.version == UNIVERSE_VERSION
        assert catalog.snapshot().symbols[0] == "SPY"
        assert all(entry.entry_eligible for entry in catalog.entries)
        # The compatibility seed deliberately has no venue or entitlement
        # proof. It remains scan-visible, but cannot authorize unattended
        # entry until an operator replaces it with a verified artifact.
        assert not any(entry.automated_entry_allowed for entry in catalog.entries)

    def test_unknown_symbols_are_visible_to_scan_but_not_entry(self) -> None:
        catalog = UniverseCatalog.from_seed(["zzzt"])
        entry = catalog.entry("ZZZT")
        assert entry is not None
        assert entry.active is True
        assert entry.scan_eligible is True
        assert entry.optionability is None
        assert entry.entry_eligible is False
        assert entry.automated_entry_allowed is False

    def test_entry_eligibility_cannot_be_true_for_unclassified_rows(self) -> None:
        with pytest.raises(ValueError, match="unclassified"):
            CatalogEntry(symbol="ZZZ", optionability=True, entry_eligible=True)

    @pytest.mark.parametrize(
        "entitlement",
        [
            {"entry_allowed": False},
            {"readiness": "UNAVAILABLE"},
            {"readiness": "NOT_ENTITLED"},
        ],
    )
    def test_explicit_entitlement_denial_blocks_automated_entry(
        self, entitlement: dict[str, object]
    ) -> None:
        entry = CatalogEntry(
            symbol="ZZZ",
            optionability=True,
            sector="TECHNOLOGY",
            correlation_group="SECTOR_TECH",
            entry_eligible=True,
            entitlement=entitlement,
        )

        assert not entry.automated_entry_allowed

    def test_artifact_hash_and_version_are_verified_before_use(self, tmp_path) -> None:
        snapshot = UniverseCatalog.from_seed().snapshot()
        artifact = tmp_path / "catalog.json"
        artifact.write_text(
            json.dumps(snapshot.to_manifest(include_artifact=False), sort_keys=True),
            encoding="utf-8",
        )
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        loaded = UniverseCatalog.from_artifact(
            artifact, expected_sha256=digest, expected_version=UNIVERSE_VERSION
        )
        assert loaded.catalog_hash == digest
        assert loaded.snapshot().expected_count == 80
        with pytest.raises(ValueError, match="hash mismatch"):
            UniverseCatalog.from_artifact(artifact, expected_sha256="0" * 64)

    def test_artifact_rejects_unknown_top_level_keys(self, tmp_path) -> None:
        manifest = _catalog_manifest()
        manifest["unexpected"] = True

        with pytest.raises(ValueError, match="unknown keys"):
            UniverseCatalog.from_artifact(_write_catalog(tmp_path, manifest))

    def test_artifact_rejects_unknown_entry_keys(self, tmp_path) -> None:
        manifest = _catalog_manifest()
        entry = manifest["entries"][0]  # type: ignore[index]
        entry["unexpected"] = "ignored?"  # type: ignore[index]

        with pytest.raises(ValueError, match="unknown keys"):
            UniverseCatalog.from_artifact(_write_catalog(tmp_path, manifest))

    @pytest.mark.parametrize(
        "missing",
        [
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
        ],
    )
    def test_artifact_requires_every_policy_field(self, tmp_path, missing: str) -> None:
        manifest = _catalog_manifest()
        entry = manifest["entries"][0]  # type: ignore[index]
        del entry[missing]  # type: ignore[index]

        with pytest.raises(ValueError, match=missing):
            UniverseCatalog.from_artifact(_write_catalog(tmp_path, manifest))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("active", "false"),
            ("optionability", "true"),
            ("scan_eligible", 1),
            ("entry_eligible", 0),
            ("listing_venue", False),
            ("sector", 123),
            ("entitlement", []),
            ("broker_contract", []),
        ],
    )
    def test_artifact_rejects_wrong_field_types(
        self, tmp_path, field: str, value: object
    ) -> None:
        manifest = _catalog_manifest()
        entry = manifest["entries"][0]  # type: ignore[index]
        entry[field] = value  # type: ignore[index]

        with pytest.raises(ValueError, match=field):
            UniverseCatalog.from_artifact(_write_catalog(tmp_path, manifest))

    @pytest.mark.parametrize("rows", [[None], ["SPY"], [[]]])
    def test_artifact_rejects_malformed_rows(self, tmp_path, rows: list[object]) -> None:
        manifest = _catalog_manifest()
        manifest["entries"] = rows

        with pytest.raises(ValueError, match="index 0"):
            UniverseCatalog.from_artifact(_write_catalog(tmp_path, manifest))

    def test_artifact_rejects_non_list_entries(self, tmp_path) -> None:
        manifest = _catalog_manifest()
        manifest["entries"] = {"SPY": manifest["entries"][0]}  # type: ignore[index]

        with pytest.raises(ValueError, match="entries must be a list"):
            UniverseCatalog.from_artifact(_write_catalog(tmp_path, manifest))

    def test_repository_seed_artifact_is_the_eighty_symbol_compatibility_catalog(self) -> None:
        artifact = (
            Path(__file__).resolve().parents[1]
            / ".."
            / "docs"
            / "autotrader-catalog-seed-80-v1.json"
        ).resolve()
        raw = artifact.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        assert digest == SEED_ARTIFACT_SHA256

        catalog = UniverseCatalog.from_artifact(
            artifact,
            expected_sha256=SEED_ARTIFACT_SHA256,
            expected_version=UNIVERSE_VERSION,
        )

        assert len(catalog) == 80
        assert catalog.catalog_hash == digest
        assert tuple(catalog.entries) == tuple(UniverseCatalog.from_seed().entries)

    def test_declared_artifact_hash_must_match_artifact_bytes(self, tmp_path) -> None:
        manifest = _catalog_manifest()
        manifest["artifact_sha256"] = "0" * 64

        with pytest.raises(ValueError, match="artifact_sha256"):
            UniverseCatalog.from_artifact(_write_catalog(tmp_path, manifest))

    def test_snapshot_is_immutable_and_digest_changes_with_metadata(self) -> None:
        catalog = UniverseCatalog.from_seed()
        snapshot = catalog.snapshot()
        with pytest.raises(AttributeError):
            snapshot.entries += (snapshot.entries[0],)  # type: ignore[misc]
        first = snapshot.digest
        changed = CatalogSnapshot(
            version=snapshot.version,
            entries=(
                CatalogEntry(
                    **{
                        **snapshot.entries[0].to_record(),
                        "listing_venue": "NYSEARCA",
                    }
                ),
                *snapshot.entries[1:],
            ),
        )
        assert changed.digest != first


def test_synthetic_catalog_digest_is_bounded_at_scale() -> None:
    entries = tuple(
        CatalogEntry(
            symbol=f"S{i:05d}",
            optionability=True,
            sector="SYNTHETIC",
            correlation_group=f"G{i % 20}",
            entry_eligible=True,
        )
        for i in range(10_000)
    )
    snapshot = CatalogSnapshot(version="synthetic/1", entries=entries)
    assert snapshot.expected_count == 10_000
    assert len(snapshot.catalog_hash) == 64
