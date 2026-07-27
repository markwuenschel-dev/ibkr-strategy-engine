"""Portable advisory locking, and the stale-lock recovery policy.

The interesting part is not "does the lock exclude" -- it is the recovery
policy. A lock nobody can ever release is worse than no lock, so a lock is
broken only when it is *both* past its TTL *and*, when that is checkable, owned
by a dead pid on this host. Both halves of that conjunction are tested, plus the
aliveness oracle they rest on.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from unittest import mock

from collabkit import locking
from collabkit.errors import EXIT_LOCKED, LockTimeout
from collabkit.locking import FileLock, SingletonLock, locked

from tests.support import IsolatedHomeTestCase


def _definitely_dead_pid() -> int:
    """A pid that has certainly exited: run a process to completion, reuse it.

    Better than a made-up large number, which is not portable (Linux caps pids
    far below what Windows hands out).
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    proc.wait()
    return proc.pid


class FileLockExclusionTests(IsolatedHomeTestCase):
    def setUp(self):
        super().setUp()
        self.lock_path = self.tmp / "locks" / "resource.lock"

    def test_a_second_acquire_from_another_instance_times_out(self):
        held = FileLock(self.lock_path, timeout=0.0)
        held.acquire()
        self.addCleanup(held.release)

        other = FileLock(self.lock_path, timeout=0.0)
        with self.assertRaises(LockTimeout) as caught:
            other.acquire()
        self.assertEqual(caught.exception.exit_code, EXIT_LOCKED)

    def test_the_lock_is_reentrant_on_a_single_instance(self):
        lock = FileLock(self.lock_path, timeout=0.0)
        with lock:
            self.assertTrue(lock.held)
            with lock:
                self.assertTrue(lock.held)
            # The inner exit must not drop the lock the outer block still holds.
            self.assertTrue(lock.held)
            self.assertTrue(self.lock_path.exists())
        self.assertFalse(lock.held)
        self.assertFalse(self.lock_path.exists())

    def test_the_lock_is_released_and_its_file_removed_on_a_normal_exit(self):
        with FileLock(self.lock_path, timeout=0.0):
            self.assertTrue(self.lock_path.is_file())
        self.assertFalse(self.lock_path.exists())
        # ...and the next acquirer is not blocked.
        with FileLock(self.lock_path, timeout=0.0):
            pass

    def test_the_lock_is_released_when_the_guarded_body_raises(self):
        class Boom(RuntimeError):
            pass

        with self.assertRaises(Boom):
            with FileLock(self.lock_path, timeout=0.0):
                raise Boom("body failed")

        self.assertFalse(self.lock_path.exists())
        with FileLock(self.lock_path, timeout=0.0):
            pass

    def test_the_locked_helper_is_the_function_call_form(self):
        with locked(self.lock_path, timeout=0.0) as lock:
            self.assertTrue(lock.held)
            self.assertTrue(self.lock_path.is_file())
        self.assertFalse(self.lock_path.exists())

    def test_a_waiting_acquirer_succeeds_once_the_holder_releases(self):
        first = FileLock(self.lock_path, timeout=0.0)
        first.acquire()
        second = FileLock(self.lock_path, timeout=2.0)
        first.release()
        second.acquire()
        self.addCleanup(second.release)
        self.assertTrue(second.held)

    def test_the_lock_file_records_the_owning_pid_and_host(self):
        with FileLock(self.lock_path, timeout=0.0):
            payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["pid"], os.getpid())
        self.assertEqual(payload["host"], socket.gethostname())


class StaleLockTests(IsolatedHomeTestCase):
    """When may an abandoned lock be broken?"""

    def setUp(self):
        super().setUp()
        self.lock_path = self.tmp / "locks" / "stale.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def plant(self, *, pid: int, host: str, age_seconds: float = 3600.0) -> None:
        self.lock_path.write_text(
            json.dumps({"pid": pid, "host": host, "acquired": 0}), encoding="utf-8"
        )
        old = time.time() - age_seconds
        os.utime(self.lock_path, (old, old))

    def test_a_lock_from_another_host_past_its_ttl_is_broken(self):
        # Cannot check the pid across machines, so age alone decides.
        self.plant(pid=4242, host="some-other-machine")
        lock = FileLock(self.lock_path, timeout=1.0, ttl=0.01)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(lock.held)

    def test_a_lock_whose_payload_is_unreadable_past_its_ttl_is_broken(self):
        self.lock_path.write_text("not json at all", encoding="utf-8")
        old = time.time() - 3600.0
        os.utime(self.lock_path, (old, old))

        lock = FileLock(self.lock_path, timeout=1.0, ttl=0.01)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(lock.held)

    def test_a_lock_owned_by_a_live_pid_is_not_broken_past_its_ttl(self):
        # The aliveness oracle is stubbed rather than exercised through a real
        # signal: this test is about the *policy* in _break_if_stale, and
        # os.kill(pid, 0) semantics differ per platform.
        self.plant(pid=os.getpid(), host=socket.gethostname())
        with mock.patch.object(locking, "_pid_alive", return_value=True):
            lock = FileLock(self.lock_path, timeout=0.0, ttl=0.01)
            with self.assertRaises(LockTimeout):
                lock.acquire()
        self.assertTrue(self.lock_path.is_file(), "a live owner's lock must survive")

    def test_a_lock_owned_by_a_dead_pid_on_this_host_is_broken(self):
        self.plant(pid=_definitely_dead_pid(), host=socket.gethostname())
        with mock.patch.object(locking, "_pid_alive", return_value=False):
            lock = FileLock(self.lock_path, timeout=1.0, ttl=0.01)
            lock.acquire()
            self.addCleanup(lock.release)
            self.assertTrue(lock.held)

    def test_a_fresh_lock_is_never_broken_however_dead_its_owner_is(self):
        self.plant(pid=_definitely_dead_pid(), host=socket.gethostname(), age_seconds=0.0)
        lock = FileLock(self.lock_path, timeout=0.0, ttl=300.0)
        with self.assertRaises(LockTimeout):
            lock.acquire()
        self.assertTrue(self.lock_path.is_file())

    # Regression guard: os.kill(pid, 0) on Windows raises OSError(WinError 87)
    # for a nonexistent pid instead of ProcessLookupError, which made
    # _pid_alive() answer True for every dead process and silently disabled
    # stale-lock recovery. locking._pid_alive_windows() exists for this.
    def test_pid_alive_reports_a_dead_process_as_dead(self):
        self.assertFalse(locking._pid_alive(_definitely_dead_pid()))

    def test_pid_alive_reports_this_process_as_alive(self):
        self.assertTrue(locking._pid_alive(os.getpid()))

    def test_pid_alive_assumes_alive_when_it_cannot_tell(self):
        # "Assume alive" is the safe default: wrongly believing a process is
        # dead breaks a live lock and corrupts what it was protecting.
        self.assertTrue(locking._pid_alive(0))
        self.assertTrue(locking._pid_alive(-1))

    # The end-to-end consequence of the check above: a lock left by a crashed
    # process must be reclaimable on every platform, not just POSIX.
    def test_a_real_stale_lock_from_a_crashed_process_is_taken_over(self):
        self.plant(pid=_definitely_dead_pid(), host=socket.gethostname())
        lock = FileLock(self.lock_path, timeout=1.0, ttl=0.01)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(lock.held)


class SingletonLockTests(IsolatedHomeTestCase):
    """"Only one of me may run" -- fail fast, never queue."""

    def setUp(self):
        super().setUp()
        self.lock_path = self.tmp / "locks" / "bridge.lock"

    def test_a_second_instance_is_refused_while_the_first_holds_it(self):
        first = SingletonLock(self.lock_path, name="test daemon")
        first.__enter__()
        self.addCleanup(first.__exit__)

        second = SingletonLock(self.lock_path, name="test daemon")
        with self.assertRaises(LockTimeout) as caught:
            second.__enter__()
        self.assertEqual(caught.exception.exit_code, EXIT_LOCKED)

    def test_the_lock_is_released_on_exit_so_a_restart_works(self):
        with SingletonLock(self.lock_path, name="test daemon"):
            self.assertTrue(self.lock_path.is_file())
        self.assertFalse(self.lock_path.exists())

        with SingletonLock(self.lock_path, name="test daemon"):
            self.assertTrue(self.lock_path.is_file())

    def test_it_does_not_wait_for_a_contended_lock(self):
        first = SingletonLock(self.lock_path, name="test daemon")
        first.__enter__()
        self.addCleanup(first.__exit__)

        started = time.monotonic()
        with self.assertRaises(LockTimeout):
            SingletonLock(self.lock_path, name="test daemon").__enter__()
        self.assertLess(time.monotonic() - started, 1.0, "a singleton must fail fast")

    def test_a_lock_left_by_a_dead_process_is_taken_over(self):
        # Unstubbed on purpose: this is the end-to-end path a user hits after
        # the bridge is killed, and it goes through the real liveness check.
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(
            json.dumps({"pid": _definitely_dead_pid(), "host": socket.gethostname()}),
            encoding="utf-8",
        )
        with SingletonLock(self.lock_path, name="test daemon") as lock:
            self.assertTrue(lock.path.is_file())

    def test_a_lock_from_another_host_is_assumed_live_and_refused(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(
            json.dumps({"pid": 4242, "host": "some-other-machine"}), encoding="utf-8"
        )
        with self.assertRaises(LockTimeout):
            SingletonLock(self.lock_path, name="test daemon").__enter__()
