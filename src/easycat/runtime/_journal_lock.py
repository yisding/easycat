"""Short-lived cross-process claims for one persistent journal path."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

_LOCK_BUCKET_COUNT = 256


def _lock_path(target_path: Path, *, namespace: str = "journal") -> Path:
    """Return a stable lock bucket for one durable target path.

    Lock files must persist to avoid splitting waiters across replaced lock
    inodes. Hashing target paths into a fixed namespace keeps that safety
    property without leaking one inode for every historical session id.
    """
    if not namespace.isascii() or not namespace.isalnum():
        raise ValueError("lock namespace must contain only ASCII letters and digits")
    digest = hashlib.sha256(os.fsencode(str(target_path.absolute()))).digest()
    bucket = int.from_bytes(digest[:2], "big") % _LOCK_BUCKET_COUNT
    return target_path.parent / f".easycat-{namespace}-{bucket:03d}.lock"


def _acquire_lock(fd: int, *, blocking: bool) -> None:
    """Take an advisory lock on *fd*, optionally without waiting."""
    if os.name == "nt":
        import msvcrt

        msvcrt_api = cast(Any, msvcrt)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        mode = msvcrt_api.LK_LOCK if blocking else msvcrt_api.LK_NBLCK
        msvcrt_api.locking(fd, mode, 1)
        return

    import fcntl

    mode = fcntl.LOCK_EX
    if not blocking:
        mode |= fcntl.LOCK_NB
    fcntl.flock(fd, mode)


def _release_lock(fd: int) -> None:
    """Release an advisory lock obtained by :func:`_acquire_lock`."""
    if os.name == "nt":
        import msvcrt

        msvcrt_api = cast(Any, msvcrt)
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt_api.locking(fd, msvcrt_api.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def path_file_claim(
    target_path: Path,
    *,
    blocking: bool,
    namespace: str,
) -> Iterator[bool]:
    """Yield whether this process exclusively claimed one durable path.

    Lock files deliberately persist after a claim ends: deleting a lock path
    would race another opener. The bounded bucket namespace limits their
    count while the advisory lock is released when its descriptor closes.
    """
    lock_path = _lock_path(target_path, namespace=namespace)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    claimed = False
    try:
        try:
            fd = os.open(lock_path, flags, 0o600)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(f"Lock is not a regular file: {lock_path}")
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            _acquire_lock(fd, blocking=blocking)
        except OSError:
            if blocking:
                raise
            yield False
            return

        claimed = True
        yield True
    finally:
        if claimed:
            try:
                _release_lock(fd)
            except OSError:
                pass
        if fd >= 0:
            os.close(fd)


@contextmanager
def journal_file_claim(db_path: Path, *, blocking: bool) -> Iterator[bool]:
    """Yield whether this process exclusively claimed one journal path.

    Journal startup holds the claim until it has committed its durable
    ``live_pid`` marker. Crash-dump and retention sweeps take a non-blocking
    claim before their final liveness check and destructive work. That closes
    the window where a new session could open a database after a sweep had
    classified it as crashed but before the sweep removed it.
    """
    with path_file_claim(
        db_path,
        blocking=blocking,
        namespace="journal",
    ) as claimed:
        yield claimed
