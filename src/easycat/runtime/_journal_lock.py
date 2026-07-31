"""Short-lived cross-process claims for one persistent journal path."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _lock_path(db_path: Path) -> Path:
    """Return the persistent sibling lock path for one journal database."""
    return db_path.with_name(db_path.name + ".lock")


def _acquire_lock(fd: int, *, blocking: bool) -> None:
    """Take an advisory lock on *fd*, optionally without waiting."""
    if os.name == "nt":
        import msvcrt

        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        msvcrt.locking(fd, mode, 1)
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

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def journal_file_claim(db_path: Path, *, blocking: bool) -> Iterator[bool]:
    """Yield whether this process exclusively claimed one journal path.

    Journal startup holds the claim until it has committed its durable
    ``live_pid`` marker. Crash-dump and retention sweeps take a non-blocking
    claim before their final liveness check and destructive work. That closes
    the window where a new session could open a database after a sweep had
    classified it as crashed but before the sweep removed it.

    Lock files deliberately persist after a claim ends: deleting a lock path
    would race another opener and is unnecessary because the advisory lock is
    released when its file descriptor closes.
    """
    lock_path = _lock_path(db_path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    claimed = False
    try:
        try:
            fd = os.open(lock_path, flags, 0o600)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(f"Journal lock is not a regular file: {lock_path}")
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
