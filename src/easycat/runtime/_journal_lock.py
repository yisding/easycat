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


def _canonical_lock_target(target_path: Path) -> Path:
    """Return *target_path* beside its resolved physical parent.

    Resolving the parent makes spelling aliases such as an existing directory
    symlink converge on one lock-file directory.  The final component is kept
    lexical: resolving a target-file symlink here could move the lock-file
    write outside the caller's intended parent before the caller has rejected
    that unsafe target.
    """
    absolute = target_path.absolute()
    try:
        physical_parent = absolute.parent.resolve(strict=False)
    except RuntimeError as exc:
        raise OSError(f"Could not resolve lock parent: {absolute.parent}") from exc
    return physical_parent / absolute.name


def _normalized_lock_identity(canonical: Path) -> bytes:
    """Return a conservative filesystem-alias identity for *canonical*.

    Windows ignores case and trailing spaces or periods in ordinary path
    components.  ``normcase`` also normalizes Windows separators.  Applying
    the same folds on every host can serialize a few distinct paths on a
    case-sensitive filesystem, which is safe; allowing two aliases to select
    different lock files is not.
    """
    target_name = canonical.name.rstrip(" .")
    identity_path = os.path.join(os.fspath(canonical.parent), target_name)
    normalized = os.path.normcase(os.path.normpath(identity_path))
    return os.fsencode(normalized.casefold())


def _lock_identity(target_path: Path) -> bytes:
    """Resolve *target_path* once and return its normalized lock identity."""
    return _normalized_lock_identity(_canonical_lock_target(target_path))


def _validate_namespace(namespace: str) -> None:
    if not namespace.isascii() or not namespace.isalnum():
        raise ValueError("lock namespace must contain only ASCII letters and digits")


def _legacy_lock_path(target_path: Path, *, namespace: str = "journal") -> Path:
    """Return the pre-normalization lock path used by older EasyCat binaries."""
    _validate_namespace(namespace)
    digest = hashlib.sha256(os.fsencode(str(target_path.absolute()))).digest()
    bucket = int.from_bytes(digest[:2], "big") % _LOCK_BUCKET_COUNT
    return target_path.parent / f".easycat-{namespace}-{bucket:03d}.lock"


def _lock_path(target_path: Path, *, namespace: str = "journal") -> Path:
    """Return a stable lock bucket for one durable target path.

    Lock files must persist to avoid splitting waiters across replaced lock
    inodes. Hashing target paths into a fixed namespace keeps that safety
    property without leaking one inode for every historical session id.
    """
    _validate_namespace(namespace)
    canonical = _canonical_lock_target(target_path)
    digest = hashlib.sha256(_normalized_lock_identity(canonical)).digest()
    bucket = int.from_bytes(digest[:2], "big") % _LOCK_BUCKET_COUNT
    return canonical.parent / f".easycat-{namespace}-{bucket:03d}.lock"


def _claim_lock_paths(target_path: Path, *, namespace: str) -> tuple[Path, ...]:
    """Return de-duplicated legacy and canonical lock paths in global order."""
    legacy = _legacy_lock_path(target_path, namespace=namespace)
    canonical = _lock_path(target_path, namespace=namespace)
    by_physical_path: dict[bytes, Path] = {}
    for lock_path in (legacy, canonical):
        identity = _lock_identity(lock_path)
        by_physical_path.setdefault(identity, lock_path)
    return tuple(by_physical_path[identity] for identity in sorted(by_physical_path))


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


def _open_and_claim_lock(lock_path: Path, *, blocking: bool) -> int:
    """Open and claim one validated regular lock file."""
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"Lock is not a regular file: {lock_path}")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        _acquire_lock(fd, blocking=blocking)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


def _release_claims(fds: list[int]) -> None:
    """Release acquired lock descriptors in reverse global order."""
    while fds:
        fd = fds.pop()
        try:
            _release_lock(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


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

    Claims take both the legacy spelling-sensitive bucket and the canonical
    alias-stable bucket so a concurrently running older binary using the exact
    same target spelling still contends. An older binary using a different
    alias remains outside this migration bridge because it never opens the
    canonical bucket.
    """
    fds: list[int] = []
    try:
        try:
            for lock_path in _claim_lock_paths(target_path, namespace=namespace):
                fds.append(_open_and_claim_lock(lock_path, blocking=blocking))
        except OSError:
            _release_claims(fds)
            if blocking:
                raise
            yield False
            return

        yield True
    finally:
        _release_claims(fds)


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
