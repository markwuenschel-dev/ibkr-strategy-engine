"""Portable advisory locking.

Deliberately does **not** use ``fcntl`` (absent on Windows) or ``msvcrt``
(absent everywhere else). The primitive is ``os.open(..., O_CREAT | O_EXCL)``,
which is atomic on every platform Python supports and needs no imports beyond
``os``.

Two distinct jobs:

``FileLock``       short-lived mutual exclusion around a read-modify-write of a
                   shared file (the registry). Contention is expected; waiting
                   is correct.
``SingletonLock``  "only one of me may run" for long-lived daemons (the Telegram
                   bridge). Contention means a second copy was started, and the
                   right answer is to fail fast rather than queue.

Both handle the stale-lock problem: a process that is SIGKILLed leaves its lock
file behind, and a lock nobody can ever release is worse than no lock. Recovery
is deliberately conservative -- a lock is only broken when it is both older than
its TTL *and* (when checkable) owned by a dead pid on this host.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import time
from pathlib import Path
from typing import Iterator

from .atomic import ensure_dir, read_json
from .errors import LockTimeout

DEFAULT_TIMEOUT = 10.0
DEFAULT_TTL = 300.0

# Backoff defaults suit a lock held for a while (a registry read-modify-write).
# A caller whose critical section is measured in microseconds -- the handoff
# state machine -- should pass a much smaller interval, or every waiter pays a
# 50ms floor for a 100us hold.
DEFAULT_POLL_INTERVAL = 0.05
DEFAULT_MAX_POLL_INTERVAL = 0.5
FAST_POLL_INTERVAL = 0.001
FAST_MAX_POLL_INTERVAL = 0.02

# How long release() keeps retrying the unlink before neutralizing the file.
UNLINK_RETRY_SECONDS = 2.0


def _owner_payload() -> str:
    import json

    return json.dumps(
        {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired": time.time(),
        }
    )


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` exists. Assumes alive when it cannot tell.

    "Assume alive" is the safe default: wrongly believing a process is dead
    breaks a live lock and corrupts the thing it was protecting.

    Windows needs its own path. ``os.kill(pid, 0)`` there raises a generic
    ``OSError`` (WinError 87, EINVAL) for a pid that does not exist rather than
    ``ProcessLookupError`` -- so the POSIX branch answers "alive" for every dead
    process, and stale-lock recovery silently never fires. A lock left by a
    killed agent would then wedge the collab until someone deleted the file by
    hand, which is exactly the failure the TTL exists to prevent.
    """
    if pid <= 0:
        return True
    if sys.platform.startswith("win"):
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by another user.
        return True
    except (OSError, AttributeError):  # pragma: no cover - exotic platforms
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Win32 liveness via ``OpenProcess`` + ``GetExitCodeProcess``.

    ``ctypes`` is stdlib, so this keeps the no-dependencies rule. Anything that
    is not a definitive "no such process" answers alive, per the same safe
    default as the POSIX branch.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - not Windows after all
        return True

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_INVALID_PARAMETER = 87
    STILL_ACTIVE = 259

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Declaring the signature is not optional: OpenProcess returns a HANDLE
        # (pointer-sized), and ctypes' default int return truncates it on 64-bit,
        # which would turn a valid handle into a bogus one.
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # 87 means "no process with that id". Access-denied and friends mean
            # it exists but is not ours to inspect -- alive.
            return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            # A process that genuinely exited with code 259 is indistinguishable
            # from a running one here. That is the documented Win32 ambiguity;
            # it errs toward "alive", which is the safe direction.
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):  # pragma: no cover - defensive
        return True


class FileLock:
    """Exclusive advisory lock backed by an ``O_EXCL`` lock file.

    Usage::

        with FileLock(root / "logs" / "locks" / "registry.lock"):
            ...read-modify-write...

    Reentrant within a single object (nested ``with`` on the *same* instance is
    counted), but not across instances -- that would defeat the point.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        ttl: float = DEFAULT_TTL,
        purpose: str = "resource",
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_poll_interval: float = DEFAULT_MAX_POLL_INTERVAL,
    ) -> None:
        self.path = Path(path)
        self.timeout = float(timeout)
        self.ttl = float(ttl)
        self.purpose = purpose
        self.poll_interval = max(1e-4, float(poll_interval))
        self.max_poll_interval = max(self.poll_interval, float(max_poll_interval))
        self._depth = 0
        self._fd: int | None = None

    # -- context manager -------------------------------------------------

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    # -- api -------------------------------------------------------------

    @property
    def held(self) -> bool:
        return self._depth > 0

    def acquire(self) -> "FileLock":
        if self._depth:
            self._depth += 1
            return self

        ensure_dir(self.path.parent)
        deadline = time.monotonic() + self.timeout
        interval = self.poll_interval
        while True:
            if self._try_acquire():
                self._depth = 1
                return self
            # A freed or stale lock is worth retrying immediately -- but still
            # only while there is time left. Without the deadline check here, a
            # lock that repeatedly vanished and reappeared would spin forever
            # instead of timing out.
            if self._break_if_stale() and time.monotonic() < deadline:
                continue
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    f"timed out after {self.timeout:g}s waiting for the {self.purpose} lock",
                    hint=(
                        f"another process holds {self.path}; if you are sure none is "
                        f"running, delete that file"
                    ),
                )
            time.sleep(interval)
            # Back off so a long hold does not spin a core.
            interval = min(interval * 2, self.max_poll_interval)

    def release(self) -> None:
        if self._depth > 1:
            self._depth -= 1
            return
        if not self._depth:
            return
        self._depth = 0
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None
        self._unlink_with_retry()

    def _unlink_with_retry(self) -> None:
        """Remove the lock file, retrying briefly, and never fail silently.

        Windows refuses to unlink a file while *any* handle is open on it --
        including a waiter that happens to be mid-read of the owner payload.
        Swallowing that error is not cosmetic: the lock file survives its owner,
        and every waiter then blocks until the TTL expires. That is a
        self-inflicted deadlock, and it is exactly what an earlier version of
        this file did.

        If the unlink genuinely cannot be done, the file is neutralized instead
        -- emptied and back-dated -- so the stale path reclaims it on the next
        poll rather than after a full TTL.
        """
        deadline = time.monotonic() + UNLINK_RETRY_SECONDS
        delay = 0.001
        while True:
            try:
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 0.05)

        # Truncate so the owner payload no longer parses as a live claim, and
        # back-date so the age check trips immediately.
        with contextlib.suppress(OSError):
            with open(self.path, "wb"):
                pass
        with contextlib.suppress(OSError):
            os.utime(self.path, (0, 0))

    # -- internals -------------------------------------------------------

    def _try_acquire(self) -> bool:
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        except OSError:
            return False
        self._fd = fd
        with contextlib.suppress(OSError):
            os.write(fd, _owner_payload().encode("utf-8"))
        return True

    def _break_if_stale(self) -> bool:
        """Remove an abandoned lock file. Returns True if one was removed.

        The cheap check comes first and the file is **not opened** unless it is
        already past its TTL. That ordering matters on Windows: a waiter holding
        the lock file open is precisely what stops the owner from unlinking it,
        so polling by reading would make waiters sabotage the holder they are
        waiting for.
        """
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            # Vanished -- the holder released it. Retry immediately.
            return True

        if age < self.ttl:
            return False

        # Past the TTL. Only now is it worth opening the file to see whose it
        # is. A live pid on this host means a long operation, not a corpse.
        info = read_json(self.path, default=None)
        if isinstance(info, dict):
            same_host = info.get("host") == socket.gethostname()
            pid = info.get("pid")
            if same_host and isinstance(pid, int) and _pid_alive(pid):
                return False

        try:
            self.path.unlink()
        except OSError:
            return False
        return True


class SingletonLock:
    """"Only one instance of this daemon" lock.

    Unlike :class:`FileLock` this never waits: a second bridge process is a
    mistake to report, not a queue to join.
    """

    def __init__(self, path: Path | str, *, name: str = "process") -> None:
        self.path = Path(path)
        self.name = name
        self._lock = FileLock(self.path, timeout=0.0, ttl=0.0, purpose=name)

    def __enter__(self) -> "SingletonLock":
        ensure_dir(self.path.parent)
        if not self._lock._try_acquire():  # noqa: SLF001 - same module, deliberate
            if not self._is_stale():
                info = read_json(self.path, default={}) or {}
                raise LockTimeout(
                    f"another {self.name} is already running "
                    f"(pid {info.get('pid', '?')} on {info.get('host', '?')})",
                    hint=f"stop it first, or delete {self.path} if it is a stale lock",
                )
            with contextlib.suppress(OSError):
                self.path.unlink()
            if not self._lock._try_acquire():  # noqa: SLF001
                raise LockTimeout(f"could not acquire the {self.name} lock at {self.path}")
        self._lock._depth = 1  # noqa: SLF001
        return self

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()

    def _is_stale(self) -> bool:
        info = read_json(self.path, default=None)
        if not isinstance(info, dict):
            return True
        if info.get("host") != socket.gethostname():
            # Different machine (shared drive). Cannot verify; assume live.
            return False
        pid = info.get("pid")
        if not isinstance(pid, int):
            return True
        return not _pid_alive(pid)


@contextlib.contextmanager
def locked(path: Path | str, **kwargs: object) -> Iterator[FileLock]:
    """Function-call form of ``with FileLock(...)``."""
    lock = FileLock(path, **kwargs)  # type: ignore[arg-type]
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()
