"""Permission helpers for sensitive runtime files."""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm")


def mkdir_private(path: Path) -> None:
    """Create a directory and force owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    os.chmod(path, PRIVATE_DIR_MODE)


def touch_private_file(path: Path) -> None:
    """Ensure a file exists with owner-only permissions before writing."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    os.chmod(path, PRIVATE_FILE_MODE)


def chmod_private_file(path: Path) -> None:
    """Force owner-only permissions on an existing file."""
    if path.exists():
        os.chmod(path, PRIVATE_FILE_MODE)


def harden_sqlite_files(db_path: Path) -> None:
    """Force private permissions on a SQLite DB and WAL/SHM sidecars."""
    chmod_private_file(db_path)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        chmod_private_file(Path(str(db_path) + suffix))


def private_tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Store sensitive archive members with owner-only file/dir metadata."""
    info.mode = PRIVATE_DIR_MODE if info.isdir() else PRIVATE_FILE_MODE
    return info
