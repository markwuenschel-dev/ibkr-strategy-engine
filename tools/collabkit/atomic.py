"""Crash-safe file writes.

ARCHITECTURE.md's claim that state on disk is "crash-safe, and trivially
resumable" is only true if no reader can ever observe a half-written file. Every
write in this kit therefore goes: write a temp file *in the destination
directory* -> fsync it -> ``os.replace`` it into place.

``os.replace`` is atomic on POSIX and, since Python 3.3, on Windows too (it
maps to ``MoveFileEx`` with ``MOVEFILE_REPLACE_EXISTING``). The temp file must
share a directory with the destination or the rename crosses a filesystem
boundary and silently degrades to copy-then-delete, which is not atomic.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from pathlib import Path
from typing import Any

# fsync on every small write is measurably slower; it is also the only thing
# standing between a power cut and a truncated registry. Correctness wins, but
# leave an escape hatch for test suites doing thousands of writes.
_FSYNC = os.environ.get("COLLAB_KIT_NO_FSYNC", "") == ""


def ensure_dir(path: Path) -> Path:
    """``mkdir -p`` that returns the path, for chaining."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> Path:
    """Replace ``path`` with ``data`` atomically. Returns ``path``."""
    path = Path(path)
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if _FSYNC:
                os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        # Never leave a .tmp turd behind for the watchers to trip over.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if _FSYNC:
        _fsync_dir(path.parent)
    return path


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> Path:
    """UTF-8 text form of :func:`atomic_write_bytes`.

    Newlines are written verbatim (``newline=""`` semantics): handoff bodies are
    round-tripped between machines and must not gain CRLFs on Windows.
    """
    return atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2) -> Path:
    """Serialize ``payload`` as UTF-8 JSON, atomically, with a trailing newline.

    ``sort_keys`` keeps the on-disk registry diffable -- it is a file a human is
    expected to open and read.
    """
    text = json.dumps(payload, indent=indent, sort_keys=True, ensure_ascii=False) + "\n"
    return atomic_write_text(path, text)


def read_text(path: Path, *, default: str | None = None) -> str:
    """Read UTF-8 text, tolerating undecodable bytes.

    ``errors="replace"`` rather than a raise: a single corrupt handoff must not
    break ``handoff list`` for the whole directory.
    """
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        if default is None:
            raise
        return default


def read_json(path: Path, *, default: Any = None) -> Any:
    """Read JSON, returning ``default`` when absent or malformed.

    Malformed is treated like absent on purpose: watcher state files are
    caches. Losing one costs a duplicate notification; refusing to start costs
    the whole session.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _fsync_dir(path: Path) -> None:
    """Flush a directory entry so the rename itself survives a crash.

    Windows has no directory fd to fsync and raises; that is expected, not an
    error worth surfacing.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:  # pragma: no cover - platform dependent
        if exc.errno not in (errno.EINVAL, errno.EACCES, errno.ENOTSUP):
            raise
    finally:
        os.close(fd)
