"""Permission helpers for sensitive runtime files."""

from __future__ import annotations

import os
import stat
import tarfile
from collections.abc import Callable
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm")


def _symlink_error(path: Path) -> OSError:
    return OSError(f"Refusing symlinked private path: {path}")


def _open_checked_path(
    path: Path,
    flags: int,
    *,
    expected_type: Callable[[int], bool],
) -> int:
    """Open *path* without following a link and verify its named inode."""
    try:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if path.is_symlink():
            raise _symlink_error(path) from exc
        raise
    try:
        named = os.lstat(path)
        opened = os.fstat(fd)
        if (
            not expected_type(named.st_mode)
            or not expected_type(opened.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise _symlink_error(path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _chmod_open_path(fd: int, mode: int) -> None:
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def mkdir_private(path: Path) -> None:
    """Create a non-symlink directory and force owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    fd = _open_checked_path(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        expected_type=stat.S_ISDIR,
    )
    _chmod_open_path(fd, PRIVATE_DIR_MODE)


def touch_private_file(path: Path) -> None:
    """Ensure a regular, non-symlink file exists with owner-only permissions."""
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            PRIVATE_FILE_MODE,
        )
    except FileExistsError:
        fd = _open_checked_path(path, os.O_RDONLY, expected_type=stat.S_ISREG)
    else:
        try:
            named = os.lstat(path)
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(named.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise _symlink_error(path)
        except BaseException:
            os.close(fd)
            raise
    _chmod_open_path(fd, PRIVATE_FILE_MODE)


def chmod_private_file(path: Path) -> None:
    """Force owner-only permissions on an existing file."""
    try:
        fd = _open_checked_path(path, os.O_RDONLY, expected_type=stat.S_ISREG)
    except FileNotFoundError:
        return
    _chmod_open_path(fd, PRIVATE_FILE_MODE)


def harden_sqlite_files(db_path: Path) -> None:
    """Force private permissions on a SQLite DB and WAL/SHM sidecars."""
    chmod_private_file(db_path)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        chmod_private_file(Path(str(db_path) + suffix))


def sqlite_readonly_uri(path: str | Path) -> str:
    """Build an escaped absolute SQLite URI for an existing read-only file."""
    return f"{Path(path).absolute().as_uri()}?mode=ro"


def private_tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Store sensitive archive members with owner-only file/dir metadata."""
    info.mode = PRIVATE_DIR_MODE if info.isdir() else PRIVATE_FILE_MODE
    return info
