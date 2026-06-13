"""Retention sweep for persisted journal files (runs on session close)."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path
from typing import Literal

from easycat.runtime._private_files import mkdir_private, private_tar_filter, touch_private_file

logger = logging.getLogger(__name__)


def run_retention(
    data_dir: str | Path,
    *,
    max_sessions: int = 50,
    max_bytes: int = 2 * 1024 * 1024 * 1024,  # 2 GB
    max_age_days: int = 14,
    mode: Literal["archive", "delete"] = "archive",
) -> int:
    """Enforce retention policy on journal files.  Returns number removed.

    Runs opportunistically on session close — never blocks a turn.
    Keeps the most recent *max_sessions* journals **or** *max_bytes* total,
    whichever is tighter, and additionally prunes any journal older than
    *max_age_days* (an age window that is on by default) so a long-lived
    project's ``.easycat/journals/`` directory does not accumulate stale
    recordings indefinitely.
    """
    root = Path(data_dir)
    journals_dir = root / "journals"
    if not journals_dir.is_dir():
        return 0

    # Gather journal files sorted oldest-first by mtime.
    files = sorted(journals_dir.glob("*.sqlite"), key=lambda p: p.stat().st_mtime)
    if not files:
        return 0

    sweep = _RetentionSweep(root, files, mode)
    cutoff = time.time() - max_age_days * 86400
    sweep.prune_older_than(cutoff)
    sweep.prune_to_caps(max_sessions, max_bytes)
    return sweep.removed


class _RetentionSweep:
    """Mutable retention state shared by the age-window and cap passes.

    Files are oldest-first; each pass pops from the front so a single
    ``removed`` counter stays accurate across both passes.
    """

    def __init__(self, root: Path, files: list[Path], mode: str) -> None:
        self._root = root
        self._files = files
        self._mode = mode
        self._total_bytes = sum(_session_bytes(root, f) for f in files)
        self.removed = 0

    def prune_older_than(self, cutoff: float) -> None:
        """Prune any journal older than *cutoff*, regardless of the caps."""
        while self._files:
            oldest = self._files[0]
            try:
                mtime = oldest.stat().st_mtime
            except OSError:
                self._files.pop(0)
                continue
            if mtime >= cutoff:
                break
            self._prune_oldest()

    def prune_to_caps(self, max_sessions: int, max_bytes: int) -> None:
        """Prune the oldest journal until both the count and byte caps hold."""
        while self._files and (len(self._files) > max_sessions or self._total_bytes > max_bytes):
            self._prune_oldest()

    def _prune_oldest(self) -> bool:
        """Pop and archive/remove the oldest journal; True if it was pruned."""
        oldest = self._files.pop(0)
        fsize = _session_bytes(self._root, oldest)

        # Guard file existence to avoid racing a concurrent crash-durability
        # sweep that may have already removed the file out from under us.
        if not oldest.exists():
            return False
        if self._mode == "archive" and not _archive_session(self._root, oldest):
            return False
        if not _remove_session(self._root, oldest):
            return False

        self._total_bytes -= fsize
        self.removed += 1
        return True


def _session_bytes(root: Path, db_path: Path) -> int:
    """Total bytes for a session: DB + WAL/SHM sidecars + artifacts."""
    size = db_path.stat().st_size
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            size += sidecar.stat().st_size
    art_dir = root / "artifacts" / db_path.stem
    if art_dir.is_dir():
        size += sum(f.stat().st_size for f in art_dir.rglob("*") if f.is_file())
    return size


def _archive_session(root: Path, oldest: Path) -> bool:
    """Tar the journal (plus artifacts) into ``archive/``; False on failure."""
    archive_dir = root / "archive"
    mkdir_private(archive_dir)
    archive_path = archive_dir / f"{oldest.stem}.tar.gz"
    touch_private_file(archive_path)
    try:
        # Checkpoint WAL so all data is in the main database file
        # before archiving — otherwise uncheckpointed pages are lost.
        conn = sqlite3.connect(str(oldest))
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()

        checkpoint_incomplete = row is not None and row[1] != row[2]

        session_id = oldest.stem
        artifact_dir = root / "artifacts" / session_id
        with tarfile.open(str(archive_path), "w:gz") as tar:
            tar.add(str(oldest), arcname=oldest.name, filter=private_tar_filter)
            if checkpoint_incomplete:
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(oldest) + suffix)
                    if sidecar.exists():
                        tar.add(
                            str(sidecar),
                            arcname=oldest.name + suffix,
                            filter=private_tar_filter,
                        )
            if artifact_dir.is_dir():
                tar.add(
                    str(artifact_dir),
                    arcname=f"artifacts/{session_id}",
                    filter=private_tar_filter,
                )
    except OSError:
        logger.warning("Failed to archive %s", oldest, exc_info=True)
        return False
    return True


def _remove_session(root: Path, oldest: Path) -> bool:
    """Delete the journal, its WAL/SHM sidecars, and artifacts; False on failure."""
    try:
        oldest.unlink()
        # Also remove the WAL/SHM sidecars if present.
        for suffix in (".sqlite-wal", ".sqlite-shm"):
            sidecar = oldest.with_suffix(suffix)
            if sidecar.exists():
                sidecar.unlink()
        # Remove corresponding artifacts.
        session_id = oldest.stem
        artifact_dir = root / "artifacts" / session_id
        if artifact_dir.is_dir():
            shutil.rmtree(str(artifact_dir), ignore_errors=True)
    except OSError:
        logger.warning("Failed to remove %s", oldest, exc_info=True)
        return False
    return True
