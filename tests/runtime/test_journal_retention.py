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
import sqlite3
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


def _mark_live_pid(db_path, pid: int) -> None:
    """Stamp a closed journal with a ``live_pid`` marker for *pid*.

    A cleanly-closed journal has its ``live_pid`` cleared; re-stamping it
    simulates a sibling session that is still live (shared ``journals/``
    directory) so retention liveness can be exercised without spawning a
    second process.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
            (str(pid),),
        )
        conn.commit()
    finally:
        conn.close()


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


# --- Liveness guards (FWP4): never sweep a LIVE or caller-owned journal ---


def test_age_window_skips_journal_with_live_pid(tmp_path):
    """A journal whose ``live_pid`` is this running process is never swept."""
    live = _seed_journal(tmp_path, "live-sess")
    _backdate(live, age_days=30)  # Old enough to trip the age window.
    _mark_live_pid(live, os.getpid())  # ...but still owned by a live process.

    removed = run_retention(tmp_path, max_age_days=14, mode="archive")

    assert removed == 0
    assert live.exists()
    assert not (tmp_path / "archive").exists()


def test_caps_skip_journal_with_live_pid_prune_stale_instead(tmp_path):
    """Under the count cap the live journal is preserved; a stale one goes."""
    live = _seed_journal(tmp_path, "live-sess")
    stale = _seed_journal(tmp_path, "stale-sess")
    # Make the live (oldest) journal the cap-pass's first candidate.
    _backdate(live, age_days=2)
    _backdate(stale, age_days=1)
    _mark_live_pid(live, os.getpid())

    # max_sessions=1 with the age window disabled: the cap must reclaim space,
    # but it must skip the live journal and remove the stale one instead.
    removed = run_retention(tmp_path, max_sessions=1, max_age_days=10**9, mode="delete")

    assert removed == 1
    assert live.exists()
    assert not stale.exists()


def test_retention_skips_journal_holding_write_lock(tmp_path):
    """A journal under an active ``BEGIN IMMEDIATE`` write lock is preserved."""
    held = _seed_journal(tmp_path, "held-sess")
    _backdate(held, age_days=30)

    # Hold a write lock the way an actively-writing session would: clear the
    # clean-close marker (so it does not read as cleanly closed) and hold no
    # live_pid marker, so only the lock probe can detect liveness.
    conn = sqlite3.connect(str(held), isolation_level=None)
    try:
        conn.execute("DELETE FROM session_state WHERE key = 'clean_close'")
        conn.execute("BEGIN IMMEDIATE")
        try:
            removed = run_retention(tmp_path, max_age_days=14, mode="archive")
        finally:
            conn.execute("ROLLBACK")
    finally:
        conn.close()

    assert removed == 0
    assert held.exists()
    assert not (tmp_path / "archive").exists()


def test_retention_still_removes_genuinely_stale_clean_journal(tmp_path):
    """A back-dated, clean-closed journal with no live pid is still pruned."""
    stale = _seed_journal(tmp_path, "stale-sess")
    _backdate(stale, age_days=30)

    removed = run_retention(tmp_path, max_age_days=14, mode="delete")

    assert removed == 1
    assert not stale.exists()


def test_retention_never_sweeps_callers_own_db_even_if_oldest(tmp_path):
    """The caller's own (skipped) journal survives even as the oldest file."""
    own = _seed_journal(tmp_path, "own-sess")
    other = _seed_journal(tmp_path, "other-sess")
    # The caller's own journal is the oldest *and* old enough to trip the age
    # window, so absent the skip it would be the first thing pruned.
    _backdate(own, age_days=30)
    _backdate(other, age_days=1)

    removed = run_retention(
        tmp_path,
        max_sessions=50,
        max_age_days=14,
        mode="delete",
        skip=own,
    )

    # The age window would prune ``own``, but the skip path protects it;
    # ``other`` is fresh and well under the count cap.
    assert removed == 0
    assert own.exists()
    assert other.exists()


def test_caps_skip_callers_own_db_prune_other_instead(tmp_path):
    """Under a tight count cap the own journal is kept; another is reclaimed."""
    own = _seed_journal(tmp_path, "own-sess")
    other = _seed_journal(tmp_path, "other-sess")
    # ``own`` is the oldest, so without the skip the cap would reclaim it
    # first; the skip forces the cap onto ``other`` instead.
    _backdate(own, age_days=2)
    _backdate(other, age_days=1)

    removed = run_retention(
        tmp_path,
        max_sessions=1,
        max_age_days=10**9,
        mode="delete",
        skip=own,
    )

    assert removed == 1
    assert own.exists()
    assert not other.exists()
