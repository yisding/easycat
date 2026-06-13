"""Age-window retention behavior for ``run_retention``.

The byte/count-cap behavior lives in
``tests/runtime/test_sqlite_journal.py::TestRetention``; this file focuses on
the on-by-default ``max_age_days`` window that prunes stale journals even when
the count/size caps are not exceeded.

Note: ``SqliteJournal.close()`` runs ``run_retention`` opportunistically with
the on-by-default 14-day window, so these tests seed every journal *fresh*,
then back-date the ones that should be stale *after* all journals are closed,
and finally invoke ``run_retention`` explicitly to assert on its return value.
"""

from __future__ import annotations

import os
import time

from easycat.runtime import SqliteJournal, run_retention
from easycat.runtime.records import JournalRecordKind


def _seed_journal(tmp_path, session_id: str):
    """Seed a closed SQLite journal (fresh mtime) and return its db path."""
    journal = SqliteJournal(session_id, data_dir=tmp_path)
    journal.append(kind=JournalRecordKind.EVENT, name="ev", session_id=session_id)
    journal.close()
    return tmp_path / "journals" / f"{session_id}.sqlite"


def _backdate(db_path, *, age_days: float) -> None:
    """Back-date a journal (and its sidecars) by ``age_days``."""
    mtime = time.time() - age_days * 86400
    for suffix in ("", "-wal", "-shm"):
        sidecar = type(db_path)(str(db_path) + suffix)
        if sidecar.exists():
            os.utime(sidecar, (mtime, mtime))


def test_age_window_archives_stale_journal_keeps_fresh(tmp_path):
    stale = _seed_journal(tmp_path, "stale-sess")
    fresh = _seed_journal(tmp_path, "fresh-sess")
    _backdate(stale, age_days=30)
    _backdate(fresh, age_days=1)

    # Count/size caps are well under the limits; only the age window applies.
    removed = run_retention(
        tmp_path,
        max_sessions=50,
        max_bytes=2 * 1024 * 1024 * 1024,
        max_age_days=14,
        mode="archive",
    )

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
    archives = list((tmp_path / "archive").glob("*.tar.gz"))
    assert [p.name for p in archives] == ["stale-sess.tar.gz"]


def test_age_window_delete_mode_removes_stale_without_archive(tmp_path):
    stale = _seed_journal(tmp_path, "stale-sess")
    fresh = _seed_journal(tmp_path, "fresh-sess")
    _backdate(stale, age_days=30)
    _backdate(fresh, age_days=1)

    removed = run_retention(tmp_path, max_age_days=14, mode="delete")

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
    assert not (tmp_path / "archive").exists()


def test_age_window_keeps_everything_when_all_fresh(tmp_path):
    paths = [_seed_journal(tmp_path, f"sess-{i}") for i in range(3)]
    for db in paths:
        _backdate(db, age_days=1)

    removed = run_retention(tmp_path, max_age_days=14, mode="delete")

    assert removed == 0
    assert len(list((tmp_path / "journals").glob("*.sqlite"))) == 3


def test_age_window_can_be_disabled_with_large_window(tmp_path):
    stale = _seed_journal(tmp_path, "stale-sess")
    _backdate(stale, age_days=30)

    removed = run_retention(tmp_path, max_age_days=10**9, mode="delete")

    assert removed == 0
    assert stale.exists()


def test_age_window_tolerates_missing_file_mid_sweep(tmp_path):
    """A concurrent sweep may unlink a file between glob and stat."""
    stale = _seed_journal(tmp_path, "stale-sess")
    fresh = _seed_journal(tmp_path, "fresh-sess")
    _backdate(stale, age_days=30)
    _backdate(fresh, age_days=1)

    # Simulate a racing crash-durability sweep removing the stale file's
    # main DB after globbing but before retention stats it.
    stale.unlink()

    removed = run_retention(tmp_path, max_age_days=14, mode="archive")

    # The missing file is skipped, not counted; the fresh journal survives.
    assert removed == 0
    assert fresh.exists()


def test_retention_no_journals_dir_age_window(tmp_path):
    removed = run_retention(tmp_path / "nonexistent", max_age_days=14)
    assert removed == 0
