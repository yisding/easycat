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
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


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


def _open_directory_chain(path: Path) -> int:
    """Open every directory component without following ancestor symlinks."""
    absolute = path.absolute()
    current = os.open(absolute.anchor or os.curdir, _DIRECTORY_FLAGS)
    try:
        parts = absolute.parts[1:] if absolute.anchor else absolute.parts
        for part in parts:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def copy_private_file(source: Path, target: Path) -> None:
    """Copy one regular file exclusively without following any symlink component."""
    source_dir = _open_directory_chain(source.parent)
    target_dir = _open_directory_chain(target.parent)
    source_fd: int | None = None
    target_fd: int | None = None
    target_created = False
    try:
        source_fd = os.open(
            source.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=source_dir,
        )
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise _symlink_error(source)
        target_fd = os.open(
            target.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            PRIVATE_FILE_MODE,
            dir_fd=target_dir,
        )
        target_created = True
        while chunk := os.read(source_fd, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                if written <= 0:
                    raise OSError("Private file copy made no progress")
                view = view[written:]
        os.fchmod(target_fd, PRIVATE_FILE_MODE)
    except BaseException:
        if target_created:
            try:
                os.unlink(target.name, dir_fd=target_dir)
            except OSError:
                pass
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if target_fd is not None:
            os.close(target_fd)
        os.close(source_dir)
        os.close(target_dir)


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
