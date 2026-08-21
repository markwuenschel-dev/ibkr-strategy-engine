"""Archive-before-mutation primitive.

``open-questions.md`` ("Prerequisite work with no substrate on main"): "No
archive primitive exists anywhere in the engine" -- verified by grep before
this file existed (0 hits for archive/shutil.copy/shutil.move under
engine/src). This is the reusable primitive the paper-day recovery verb (and
any future caller that must archive evidence before mutating gate.json,
session.lock, scheduler.pid/claim, or execution-outbox state) is expected to
call first.

Modeled on the shape a human hand-produced once, at
``engine/.engine/paperday/recovery-archive/gate.json.pre-clear-20260819.json``
plus its sibling receipt: an archived copy of the exact bytes, plus a durable
record of where it came from, its hash, why, and when.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from pathlib import Path

import pytest

from engine.archive import ArchiveError, ArchiveManifest, archive_before_mutation

NOW = dt.datetime(2026, 8, 20, 12, 0, 0, tzinfo=dt.timezone.utc)


def test_archives_a_normal_file_and_returns_a_manifest(tmp_path: Path) -> None:
    source = tmp_path / "gate.json"
    source.write_text('{"state": "PAPER_DAY_BLOCKED"}', encoding="utf-8")
    archive_dir = tmp_path / "recovery-archive"

    manifest = archive_before_mutation(
        source,
        archive_dir=archive_dir,
        reason="pre-clear evidence",
        now=NOW,
    )

    assert isinstance(manifest, ArchiveManifest)
    assert manifest.original_path == source
    assert manifest.archive_path.exists()
    assert manifest.archive_path.parent == archive_dir
    assert manifest.reason == "pre-clear evidence"
    assert manifest.archived_at == NOW


def test_archived_copy_is_byte_identical_to_the_source(tmp_path: Path) -> None:
    source = tmp_path / "session.lock"
    content = b'{"session_id": "s1", "fencing_token": "abc"}'
    source.write_bytes(content)

    manifest = archive_before_mutation(
        source,
        archive_dir=tmp_path / "archive",
        reason="pre-mutation snapshot",
        now=NOW,
    )

    assert manifest.archive_path.read_bytes() == content


def test_manifest_sha256_matches_the_source_content(tmp_path: Path) -> None:
    source = tmp_path / "scheduler.claim"
    content = b"some claim payload, arbitrary bytes"
    source.write_bytes(content)
    expected_digest = hashlib.sha256(content).hexdigest()

    manifest = archive_before_mutation(
        source,
        archive_dir=tmp_path / "archive",
        reason="pre-clear",
        now=NOW,
    )

    assert manifest.sha256 == expected_digest
    assert manifest.sha256 == hashlib.sha256(manifest.archive_path.read_bytes()).hexdigest()


def test_original_file_is_left_untouched_never_moved(tmp_path: Path) -> None:
    source = tmp_path / "gate.json"
    content = b'{"state": "PAPER_DAY_BLOCKED"}'
    source.write_bytes(content)

    archive_before_mutation(
        source,
        archive_dir=tmp_path / "archive",
        reason="pre-clear",
        now=NOW,
    )

    # The primitive only ever copies. The caller decides, separately and
    # later, whether/how to mutate or delete the original.
    assert source.exists()
    assert source.read_bytes() == content


def test_missing_source_refuses_rather_than_silently_no_opping(tmp_path: Path) -> None:
    source = tmp_path / "does-not-exist.json"

    with pytest.raises(ArchiveError):
        archive_before_mutation(
            source,
            archive_dir=tmp_path / "archive",
            reason="pre-clear",
            now=NOW,
        )

    # No partial archive directory debris from a refused archive.
    assert not (tmp_path / "archive").exists()


def test_empty_reason_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "gate.json"
    source.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError):
        archive_before_mutation(
            source,
            archive_dir=tmp_path / "archive",
            reason="   ",
            now=NOW,
        )


def test_manifest_is_persisted_durably_alongside_the_archive(tmp_path: Path) -> None:
    source = tmp_path / "gate.json"
    source.write_text('{"state": "PAPER_DAY_BLOCKED"}', encoding="utf-8")

    manifest = archive_before_mutation(
        source,
        archive_dir=tmp_path / "archive",
        reason="pre-clear evidence",
        now=NOW,
    )

    assert manifest.manifest_path is not None
    assert manifest.manifest_path.exists()
    on_disk = json.loads(manifest.manifest_path.read_text(encoding="utf-8"))
    assert on_disk["original_path"] == str(source)
    assert on_disk["archive_path"] == str(manifest.archive_path)
    assert on_disk["sha256"] == manifest.sha256
    assert on_disk["reason"] == "pre-clear evidence"
    assert on_disk["archived_at"] == NOW.isoformat()


def test_concurrent_archive_calls_on_the_same_source_do_not_collide(tmp_path: Path) -> None:
    """Two callers racing to archive the same file before mutating it (e.g.
    two lanes each about to touch gate.json) must both succeed with distinct,
    intact archive artifacts -- never overwrite one another, even when they
    share the exact same ``now`` (same-instant concurrency is the case a
    timestamp-only filename cannot disambiguate)."""
    source = tmp_path / "gate.json"
    source.write_text('{"state": "PAPER_DAY_BLOCKED"}', encoding="utf-8")
    archive_dir = tmp_path / "archive"

    results: list[ArchiveManifest] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        try:
            results.append(
                archive_before_mutation(
                    source,
                    archive_dir=archive_dir,
                    reason="concurrent pre-clear",
                    now=NOW,
                )
            )
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 8
    archive_paths = {m.archive_path for m in results}
    assert len(archive_paths) == 8, "each concurrent call must get its own archive file"
    for manifest in results:
        assert manifest.archive_path.read_bytes() == source.read_bytes()
        assert manifest.manifest_path.exists()


def test_archive_dir_is_created_if_missing(tmp_path: Path) -> None:
    source = tmp_path / "gate.json"
    source.write_text("{}", encoding="utf-8")
    archive_dir = tmp_path / "nested" / "recovery-archive"

    manifest = archive_before_mutation(
        source, archive_dir=archive_dir, reason="pre-clear", now=NOW
    )

    assert manifest.archive_path.exists()
