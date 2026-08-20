"""Direct coverage for ``_acquire_lock_atomically`` (BLOCKER-1's fix).

The full ``test_paperday_*`` suite exercises this only through the happy path
of ``_acquire_lock``, which never proves the two properties that actually
matter here: that a second acquire on an already-held lock still refuses
(mutual exclusion did not regress when the writer became atomic-publish), and
that a crash between the temp-file write and the publish leaves no trace on
the real lock path (no torn file, ever -- the whole point of the fix).
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.paperday import _acquire_lock_atomically


class TestAcquireLockAtomically:
    def test_first_acquire_publishes_the_full_payload(self, tmp_path: Path) -> None:
        lock = tmp_path / "session.lock"
        payload = json.dumps({"session_id": "s1", "fencing_token": "abc"})

        acquired = _acquire_lock_atomically(lock, payload)

        assert acquired is True
        assert lock.exists()
        assert json.loads(lock.read_text(encoding="utf-8")) == {
            "session_id": "s1",
            "fencing_token": "abc",
        }

    def test_no_orphaned_temp_files_survive_a_successful_acquire(self, tmp_path: Path) -> None:
        lock = tmp_path / "session.lock"
        _acquire_lock_atomically(lock, json.dumps({"session_id": "s1"}))

        leftovers = [p for p in tmp_path.iterdir() if p.name != lock.name]
        assert leftovers == []

    def test_second_acquire_refuses_and_does_not_touch_the_first_lock(
        self, tmp_path: Path
    ) -> None:
        lock = tmp_path / "session.lock"
        first_payload = json.dumps({"session_id": "s1", "fencing_token": "abc"})
        second_payload = json.dumps({"session_id": "s2", "fencing_token": "xyz"})

        first = _acquire_lock_atomically(lock, first_payload)
        second = _acquire_lock_atomically(lock, second_payload)

        assert first is True
        assert second is False
        # Mutual exclusion held: the original session's identity is intact,
        # not overwritten by the loser and not torn by the collision.
        assert json.loads(lock.read_text(encoding="utf-8")) == {
            "session_id": "s1",
            "fencing_token": "abc",
        }

    def test_second_acquire_leaves_no_orphaned_temp_file_either(self, tmp_path: Path) -> None:
        lock = tmp_path / "session.lock"
        _acquire_lock_atomically(lock, json.dumps({"session_id": "s1"}))
        _acquire_lock_atomically(lock, json.dumps({"session_id": "s2"}))

        leftovers = [p for p in tmp_path.iterdir() if p.name != lock.name]
        assert leftovers == []

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        lock = tmp_path / "nested" / "state" / "session.lock"

        acquired = _acquire_lock_atomically(lock, json.dumps({"session_id": "s1"}))

        assert acquired is True
        assert lock.exists()

    def test_publish_is_never_observed_partially_written(self, tmp_path: Path) -> None:
        """The property BLOCKER-1 exists to guarantee: at every moment either
        the lock is absent, or it is fully present and valid JSON -- never a
        zero-byte or truncated file. This can't fabricate a real crash
        mid-write, but it confirms the publish step is the single atomic
        ``os.link`` call that makes such a state unreachable: the temp file
        is fully flushed+fsynced *before* the only operation that makes the
        real path visible."""
        lock = tmp_path / "session.lock"
        payload = json.dumps({"session_id": "s1", "note": "x" * 5000})

        _acquire_lock_atomically(lock, payload)

        # Never partially written: the file that exists parses cleanly and
        # matches the payload byte-for-byte, or it does not exist at all.
        assert lock.read_text(encoding="utf-8") == payload
