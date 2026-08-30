"""Retention sweep for persisted journal files (runs on session close)."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path
from typing import Literal

from easycat.runtime._journal_lock import journal_file_claim
from easycat.runtime._private_files import mkdir_private, private_tar_filter
from easycat.runtime.crash_sweep import is_journal_live

logger = logging.getLogger(__name__)


def run_retention(
    data_dir: str | Path,
    *,
    max_sessions: int = 50,
    max_bytes: int = 2 * 1024 * 1024 * 1024,  # 2 GB
    max_age_days: int = 14,
    mode: Literal["archive", "delete"] = "archive",
    skip: str | Path | None = None,
) -> int:
    """Enforce retention policy on journal files.  Returns number removed.

    Runs opportunistically on session close — never blocks a turn.
    Keeps the most recent *max_sessions* journals **or** *max_bytes* total,
    whichever is tighter, and additionally prunes any journal older than
    *max_age_days* (an age window that is on by default) so a long-lived
    project's ``.easycat/journals/`` directory does not accumulate stale
    recordings indefinitely.

    A **live** journal (one currently owned by a running session — common on
    a shared ``journals/`` directory such as telephony, where many sessions
    write concurrently) is never archived or removed by any pass; archiving
    or unlinking an in-flight database would lose or corrupt it.  *skip* names
    the caller's own journal path (threaded in by ``SqliteJournal.close()``)
    and is likewise never swept, even when it is the oldest file.
    """
    root = Path(data_dir)
    journals_dir = root / "journals"
    if not journals_dir.is_dir():
        return 0

    skip_resolved = None
    if skip is not None:
        skip_path = Path(skip)
        try:
            skip_resolved = skip_path.resolve()
        except OSError:
            skip_resolved = skip_path

    # Gather journal files sorted oldest-first by mtime.  A concurrent crash
    # sweep may unlink a file after globbing but before we stat it; skip that
    # vanished path instead of failing the close-time retention pass.
    files = _journal_files_oldest_first(journals_dir)
    if not files:
        return 0

    sweep = _RetentionSweep(root, files, mode, skip_resolved)
    cutoff = time.time() - max_age_days * 86400
    sweep.prune_older_than(cutoff)
    sweep.prune_to_caps(max_sessions, max_bytes)
    return sweep.removed


class _RetentionSweep:
    """Mutable retention state shared by the age-window and cap passes.

    Files are oldest-first.  ``_files`` holds only **prunable** candidates —
    live journals (owned by a running session) and the caller's own journal
    are filtered out up front and never archived or removed by either pass.
    Their bytes still count toward ``_total_bytes`` (they occupy space that
    cannot be reclaimed), but they never block the cap pass from making
    progress on the remaining removable journals.
    """

    def __init__(
        self,
        root: Path,
        files: list[Path],
        mode: str,
        skip_resolved: Path | None = None,
    ) -> None:
        self._root = root
        self._mode = mode
        self._skip_resolved = skip_resolved
        self._sizes: dict[Path, int] = {}
        self._total_bytes = 0
        self._files: list[Path] = []
        self._protected_count = 0
        self._claim_contended = False
        for file in files:
            size = _session_bytes(root, file)
            if size is None:
                continue
            # Total bytes includes protected journals — they cannot be
            # reclaimed, so the cap pass must account for the space they hold.
            self._total_bytes += size
            if self._is_protected(file):
                # Missing files can present as unreadable/protected if they
                # vanish between size accounting and liveness classification.
                # They no longer occupy a session slot, so do not count them.
                if not file.exists():
                    self._total_bytes -= size
                    continue
                self._protected_count += 1
                continue
            # Candidate list excludes protected journals so neither pass can
            # ever archive/checkpoint/unlink a live or caller-owned database.
            self._files.append(file)
            self._sizes[file] = size
        self.removed = 0

    def _is_protected(self, db_path: Path) -> bool:
        """True if *db_path* must never be swept (caller-owned or live)."""
        if self._skip_resolved is not None:
            try:
                if db_path.resolve() == self._skip_resolved:
                    return True
            except OSError:
                return True
        return is_journal_live(db_path)

    def prune_older_than(self, cutoff: float) -> None:
        """Prune any prunable journal older than *cutoff*, regardless of caps."""
        while self._files and not self._claim_contended:
            oldest = self._files[0]
            try:
                mtime = oldest.stat().st_mtime
            except OSError:
                missing = self._files.pop(0)
                self._total_bytes -= self._sizes.pop(missing, 0)
                continue
            if mtime >= cutoff:
                break
            if not self._prune_oldest():
                break

    def prune_to_caps(self, max_sessions: int, max_bytes: int) -> None:
        """Prune the oldest prunable journal until count and byte caps hold."""
        while (
            self._files
            and not self._claim_contended
            and (
                len(self._files) + self._protected_count > max_sessions
                or self._total_bytes > max_bytes
            )
        ):
            if not self._prune_oldest():
                break

    def _prune_oldest(self) -> bool:
        """Pop and archive/remove the oldest prunable journal; True if pruned."""
        oldest = self._files.pop(0)
        fsize = self._sizes.pop(oldest, 0)

        def _restore() -> None:
            self._files.insert(0, oldest)
            self._sizes[oldest] = fsize

        # Guard file existence to avoid racing a concurrent crash-durability
        # sweep that may have already removed the file out from under us.
        try:
            unavailable = oldest.is_symlink() or not oldest.exists()
        except OSError:
            unavailable = True
        if unavailable:
            self._total_bytes -= fsize
            return False
        with journal_file_claim(oldest, blocking=False) as claimed:
            if not claimed:
                # Another retention/crash sweep may be acting on this same
                # snapshot. Stop this pass instead of treating the transient
                # contention as one permanently protected session and
                # deleting a different candidate to satisfy the stale caps.
                self._claim_contended = True
                _restore()
                return False
            if self._is_protected(oldest):
                self._protected_count += 1
                return False
            try:
                unavailable = oldest.is_symlink() or not oldest.exists()
            except OSError:
                unavailable = True
            if unavailable:
                self._total_bytes -= fsize
                return False
            if not self._archive_and_remove(oldest):
                _restore()
                return False

        self._total_bytes -= fsize
        self.removed += 1
        return True

    def _archive_and_remove(self, oldest: Path) -> bool:
        """Archive if configured, then remove without retaining duplicate copies."""
        archive_path: Path | None = None
        if self._mode == "archive":
            archive_path = _archive_session(self._root, oldest)
            if archive_path is None:
                return False
        if _remove_session(self._root, oldest):
            return True

        # If the source DB remains, the archive is only a duplicate. Remove it
        # so repeated sweeps cannot reserve unbounded suffixes. Retain it when
        # only sidecar cleanup failed: it is then the sole durable DB copy.
        if archive_path is not None and oldest.exists():
            try:
                archive_path.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Failed to remove duplicate archive %s",
                    archive_path,
                    exc_info=True,
                )
        return False


def _journal_files_oldest_first(journals_dir: Path) -> list[Path]:
    """Return existing journal DBs oldest-first, tolerating concurrent unlink."""
    statted: list[tuple[float, Path]] = []
    for path in journals_dir.glob("*.sqlite"):
        try:
            if path.is_symlink():
                continue
            statted.append((path.stat().st_mtime, path))
        except OSError:
            continue
    statted.sort(key=lambda item: item[0])
    return [path for _, path in statted]


def _session_bytes(root: Path, db_path: Path) -> int | None:
    """Total bytes for a session: DB + WAL/SHM sidecars + artifacts."""
    try:
        if db_path.is_symlink():
            return None
        size = db_path.stat().st_size
    except OSError:
        return None
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        try:
            size += sidecar.stat().st_size
        except OSError:
            pass
    art_dir = root / "artifacts" / db_path.stem
    if art_dir.is_dir():
        try:
            artifact_paths = art_dir.rglob("*")
            for artifact_path in artifact_paths:
                try:
                    if artifact_path.is_file():
                        size += artifact_path.stat().st_size
                except OSError:
                    pass
        except OSError:
            pass
    return size


def _archive_session(root: Path, oldest: Path) -> Path | None:
    """Tar the journal and artifacts; return its path or ``None`` on failure."""
    archive_dir = root / "archive"
    archive_path: Path | None = None
    try:
        if oldest.is_symlink():
            return None
        mkdir_private(archive_dir)
        archive_path = _reserve_archive_path(archive_dir, oldest.stem)
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
    except (OSError, sqlite3.DatabaseError, tarfile.TarError):
        if archive_path is not None:
            try:
                archive_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Failed to remove incomplete archive %s", archive_path, exc_info=True)
        logger.warning("Failed to archive %s", oldest, exc_info=True)
        return None
    return archive_path


def _reserve_archive_path(archive_dir: Path, session_id: str) -> Path:
    """Create and return an exclusive, collision-free archive path.

    Session ids can be reused after retention has removed their prior live
    journal.  Keep the first archive's historic name and suffix later
    archives so a later session cannot replace an earlier post-mortem.
    """
    suffix = 0
    while True:
        suffix_text = "" if suffix == 0 else f"-{suffix}"
        archive_path = archive_dir / f"{session_id}{suffix_text}.tar.gz"
        try:
            fd = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            suffix += 1
            continue
        os.close(fd)
        os.chmod(archive_path, 0o600)
        return archive_path


def _remove_session(root: Path, oldest: Path) -> bool:
    """Delete the journal, its WAL/SHM sidecars, and artifacts; False on failure."""
    try:
        if oldest.is_symlink():
            return False
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
