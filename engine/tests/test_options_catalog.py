"""Catalog manifests and fail-closed unknown-symbol behavior."""

from __future__ import annotations

import hashlib
import json

import pytest

from engine.options.catalog import CatalogEntry, CatalogSnapshot, UniverseCatalog
from engine.options.universe_data import UNIVERSE_VERSION


class TestUniverseCatalog:
    def test_seed_wraps_the_existing_eighty_without_reordering(self) -> None:
        catalog = UniverseCatalog.from_seed()
        assert len(catalog) == 80
        assert catalog.version == UNIVERSE_VERSION
        assert catalog.snapshot().symbols[0] == "SPY"
        assert all(entry.automated_entry_allowed for entry in catalog.entries)

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
