"""Retention sweep for persisted journal files (runs on session close)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


def run_retention(
    data_dir: str | Path,
    *,
    max_sessions: int = 50,
    max_bytes: int = 2 * 1024 * 1024 * 1024,  # 2 GB
    mode: Literal["archive", "delete"] = "archive",
) -> int:
    """Enforce retention policy on journal files.  Returns number removed.

    Runs opportunistically on session close — never blocks a turn.
    Keeps the most recent *max_sessions* journals **or** *max_bytes* total,
    whichever is tighter.
    """
    import shutil
    import tarfile

    root = Path(data_dir)
    journals_dir = root / "journals"
    if not journals_dir.is_dir():
        return 0

    # Gather journal files sorted oldest-first by mtime.
    files = sorted(journals_dir.glob("*.sqlite"), key=lambda p: p.stat().st_mtime)
    if not files:
        return 0

    artifacts_root = root / "artifacts"

    def _session_bytes(db_path: Path) -> int:
        """Total bytes for a session: DB + WAL/SHM sidecars + artifacts."""
        size = db_path.stat().st_size
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                size += sidecar.stat().st_size
        art_dir = artifacts_root / db_path.stem
        if art_dir.is_dir():
            size += sum(f.stat().st_size for f in art_dir.rglob("*") if f.is_file())
        return size

    total_bytes = sum(_session_bytes(f) for f in files)
    removed = 0

    while files and (len(files) > max_sessions or total_bytes > max_bytes):
        oldest = files.pop(0)
        fsize = _session_bytes(oldest)

        if mode == "archive":
            archive_dir = root / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / f"{oldest.stem}.tar.gz"
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
                    tar.add(str(oldest), arcname=oldest.name)
                    if checkpoint_incomplete:
                        for suffix in ("-wal", "-shm"):
                            sidecar = Path(str(oldest) + suffix)
                            if sidecar.exists():
                                tar.add(str(sidecar), arcname=oldest.name + suffix)
                    if artifact_dir.is_dir():
                        tar.add(str(artifact_dir), arcname=f"artifacts/{session_id}")
            except OSError:
                logger.warning("Failed to archive %s", oldest, exc_info=True)
                continue

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
            continue

        total_bytes -= fsize
        removed += 1

    return removed
