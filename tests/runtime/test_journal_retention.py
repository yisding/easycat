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
import threading
import time
from contextlib import contextmanager

import pytest

from easycat.runtime import SqliteJournal, run_retention
from easycat.runtime import journal_retention as journal_retention_module
from easycat.runtime._journal_lock import _LOCK_BUCKET_COUNT, journal_file_claim
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


def test_failed_source_removal_does_not_accumulate_duplicate_archives(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _seed_journal(tmp_path, "stuck")
    monkeypatch.setattr(
        journal_retention_module,
        "_remove_session",
        lambda _root, _path: False,
    )

    for _ in range(2):
        assert (
            run_retention(
                tmp_path,
                max_sessions=0,
                max_age_days=10**9,
                mode="archive",
            )
            == 0
        )

    assert source.exists()
    assert list((tmp_path / "archive").iterdir()) == []


def test_archive_retention_preserves_prior_archive_for_reused_session_id(tmp_path):
    """A later reuse of an id must not overwrite its earlier archive."""
    _seed_journal(tmp_path, "reused-sess")
    assert (
        run_retention(
            tmp_path,
            max_sessions=0,
            max_age_days=10**9,
            mode="archive",
        )
        == 1
    )

    archive_dir = tmp_path / "archive"
    first_archive = archive_dir / "reused-sess.tar.gz"
    first_bytes = first_archive.read_bytes()

    _seed_journal(tmp_path, "reused-sess")
    assert (
        run_retention(
            tmp_path,
            max_sessions=0,
            max_age_days=10**9,
            mode="archive",
        )
        == 1
    )

    assert first_archive.read_bytes() == first_bytes
    assert (archive_dir / "reused-sess-1.tar.gz").exists()


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


def test_age_window_tolerates_missing_file_mid_sweep(tmp_path, monkeypatch):
    """A concurrent sweep may unlink a file between glob and stat."""
    stale = _seed_journal(tmp_path, "stale-sess")
    fresh = _seed_journal(tmp_path, "fresh-sess")
    _backdate(stale, age_days=30)
    _backdate(fresh, age_days=1)

    # Simulate a racing crash-durability sweep removing the stale file's
    # main DB after globbing but before retention stats it.
    original_stat = type(stale).stat
    vanished = False

    def race_stat(self, *args, **kwargs):
        nonlocal vanished
        if self == stale and not vanished:
            vanished = True
            stale.unlink(missing_ok=True)
            raise FileNotFoundError(str(self))
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(stale), "stat", race_stat)

    removed = run_retention(tmp_path, max_age_days=14, mode="archive")

    # The missing file is skipped, not counted; the fresh journal survives.
    assert removed == 0
    assert fresh.exists()


def test_age_window_missing_file_drops_cached_bytes_before_cap_pass(tmp_path, monkeypatch):
    """A vanished stale candidate must not leave phantom bytes for cap pruning."""
    stale = _seed_journal(tmp_path, "stale-sess")
    fresh = _seed_journal(tmp_path, "fresh-sess")
    _backdate(stale, age_days=30)
    _backdate(fresh, age_days=1)
    # Durable artifact-epoch metadata is part of the retained session size,
    # even though this fixture has no payload blobs.
    fresh_bytes = journal_retention_module._session_bytes(tmp_path, fresh)
    assert fresh_bytes is not None

    original_stat = type(stale).stat
    vanished = False

    def race_stat(self, *args, **kwargs):
        nonlocal vanished
        if self == stale and not vanished:
            vanished = True
            stale.unlink(missing_ok=True)
            raise FileNotFoundError(str(self))
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(stale), "stat", race_stat)

    removed = run_retention(
        tmp_path,
        max_age_days=14,
        max_bytes=fresh_bytes + 1,
        mode="delete",
    )

    assert removed == 0
    assert fresh.exists()


def test_retention_no_journals_dir_age_window(tmp_path):
    removed = run_retention(tmp_path / "nonexistent", max_age_days=14)
    assert removed == 0


def test_retention_ignores_symlinked_journal_file(tmp_path):
    outside_root = tmp_path / "outside"
    target = _seed_journal(outside_root, "target")
    journals = tmp_path / "journals"
    journals.mkdir()
    linked = journals / "linked.sqlite"
    linked.symlink_to(target)

    removed = run_retention(tmp_path, max_sessions=0, max_age_days=10**9, mode="archive")

    assert removed == 0
    assert linked.is_symlink()
    assert target.exists()
    assert not (tmp_path / "archive").exists()


def test_archive_session_symlink_returns_failure_sentinel(tmp_path):
    outside_root = tmp_path / "outside"
    target = _seed_journal(outside_root, "target")
    journals = tmp_path / "journals"
    journals.mkdir()
    linked = journals / "linked.sqlite"
    linked.symlink_to(target)

    assert journal_retention_module._archive_session(tmp_path, linked) is None
    assert linked.is_symlink()
    assert target.exists()
    assert not (tmp_path / "archive").exists()


def test_retention_refuses_symlinked_archive_directory(tmp_path):
    stale = _seed_journal(tmp_path, "stale-sess")
    target = tmp_path / "outside-archive"
    target.mkdir()
    os.chmod(target, 0o755)
    archive = tmp_path / "archive"
    archive.symlink_to(target, target_is_directory=True)

    removed = run_retention(tmp_path, max_sessions=0, max_age_days=10**9, mode="archive")

    assert removed == 0
    assert stale.exists()
    assert archive.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o755
    assert list(target.iterdir()) == []


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


def test_age_window_does_not_trust_reused_pid_marker(tmp_path):
    """An alive PID with a stale process-start token is not a live owner."""
    from easycat.runtime.crash_sweep import _boot_id, _process_start_token

    start_token = _process_start_token(os.getpid())
    boot_id = _boot_id()
    if start_token is None or boot_id is None:
        pytest.skip("process start identity requires readable Linux /proc")
    stale = _seed_journal(tmp_path, "reused-sess")
    conn = sqlite3.connect(str(stale))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
            (str(os.getpid()),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid_start', ?)",
            (f"{boot_id}:{int(start_token) + 1}",),
        )
        conn.commit()
    finally:
        conn.close()
    _backdate(stale, age_days=30)

    removed = run_retention(tmp_path, max_age_days=14, mode="delete")

    assert removed == 1
    assert not stale.exists()


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


def test_retention_rechecks_liveness_after_acquiring_journal_claim(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A newly opened journal must not be archived or removed mid-startup."""
    db_path = _seed_journal(tmp_path, "victim")
    _backdate(db_path, age_days=30)
    entered_claim = threading.Event()
    release_claim = threading.Event()
    original_claim = journal_retention_module.journal_file_claim

    @contextmanager
    def delayed_claim(path, *, blocking):
        if path == db_path and not blocking:
            entered_claim.set()
            assert release_claim.wait(2)
        with original_claim(path, blocking=blocking) as claimed:
            yield claimed

    monkeypatch.setattr(journal_retention_module, "journal_file_claim", delayed_claim)
    removed: list[int] = []
    retention_thread = threading.Thread(
        target=lambda: removed.append(run_retention(tmp_path, max_age_days=14, mode="archive")),
    )
    retention_thread.start()
    assert entered_claim.wait(2)

    live = SqliteJournal("victim", data_dir=tmp_path)
    try:
        release_claim.set()
        retention_thread.join(2)
        assert not retention_thread.is_alive()
        assert removed == [0]
        assert db_path.exists()
        live.append(kind=JournalRecordKind.EVENT, name="live", session_id="victim")
    finally:
        release_claim.set()
        retention_thread.join(2)
        live.close()


def test_retention_stops_when_another_sweep_holds_oldest_claim(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oldest = _seed_journal(tmp_path, "oldest")
    newest = _seed_journal(tmp_path, "newest")
    _backdate(oldest, age_days=2)
    _backdate(newest, age_days=1)
    original_claim = journal_retention_module.journal_file_claim

    @contextmanager
    def contend_oldest(path, *, blocking):
        if path == oldest and not blocking:
            yield False
            return
        with original_claim(path, blocking=blocking) as claimed:
            yield claimed

    monkeypatch.setattr(journal_retention_module, "journal_file_claim", contend_oldest)

    removed = run_retention(
        tmp_path,
        max_sessions=1,
        max_age_days=10**9,
        mode="delete",
    )

    assert removed == 0
    assert oldest.exists()
    assert newest.exists()


def _two_candidate_sweep(tmp_path):
    """Seed two stale journals and return (sweep, oldest, newest)."""
    oldest = _seed_journal(tmp_path, "oldest")
    newest = _seed_journal(tmp_path, "newest")
    _backdate(oldest, age_days=2)
    _backdate(newest, age_days=1)
    sweep = journal_retention_module._RetentionSweep(tmp_path, [oldest, newest], "delete")
    return sweep, oldest, newest


def test_cap_pass_continues_past_candidate_that_vanished_before_claim(tmp_path) -> None:
    sweep, oldest, newest = _two_candidate_sweep(tmp_path)
    total = sweep._total_bytes
    oldest_bytes = sweep._sizes[oldest]
    newest_bytes = sweep._sizes[newest]
    oldest.unlink()

    sweep.prune_to_caps(max_sessions=0, max_bytes=10**9)

    assert sweep.removed == 1
    assert not newest.exists()
    assert sweep._total_bytes == total - oldest_bytes - newest_bytes
    assert sweep._protected_count == 0


def test_cap_pass_continues_past_candidate_that_vanished_after_claim(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sweep, oldest, newest = _two_candidate_sweep(tmp_path)
    total = sweep._total_bytes
    oldest_bytes = sweep._sizes[oldest]
    newest_bytes = sweep._sizes[newest]
    original_claim = journal_retention_module.journal_file_claim

    @contextmanager
    def unlink_under_claim(path, *, blocking):
        with original_claim(path, blocking=blocking) as claimed:
            if path == oldest and claimed:
                oldest.unlink()
            yield claimed

    monkeypatch.setattr(journal_retention_module, "journal_file_claim", unlink_under_claim)

    sweep.prune_to_caps(max_sessions=0, max_bytes=10**9)

    assert sweep.removed == 1
    assert not newest.exists()
    assert sweep._total_bytes == total - oldest_bytes - newest_bytes
    assert sweep._protected_count == 0


def test_cap_pass_keeps_bytes_for_candidate_that_became_protected_after_claim(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sweep, oldest, newest = _two_candidate_sweep(tmp_path)
    total = sweep._total_bytes
    newest_bytes = sweep._sizes[newest]
    original_claim = journal_retention_module.journal_file_claim

    @contextmanager
    def go_live_under_claim(path, *, blocking):
        with original_claim(path, blocking=blocking) as claimed:
            if path == oldest and claimed:
                _mark_live_pid(oldest, os.getpid())
            yield claimed

    monkeypatch.setattr(journal_retention_module, "journal_file_claim", go_live_under_claim)

    sweep.prune_to_caps(max_sessions=0, max_bytes=10**9)

    # The newly live journal is skipped but still occupies space; pruning
    # continues to the next candidate instead of stalling.
    assert sweep.removed == 1
    assert oldest.exists()
    assert not newest.exists()
    assert sweep._protected_count == 1
    assert sweep._total_bytes == total - newest_bytes


def test_age_pass_continues_past_candidate_that_vanished_after_claim(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sweep, oldest, newest = _two_candidate_sweep(tmp_path)
    original_claim = journal_retention_module.journal_file_claim

    @contextmanager
    def unlink_under_claim(path, *, blocking):
        with original_claim(path, blocking=blocking) as claimed:
            if path == oldest and claimed:
                oldest.unlink()
            yield claimed

    monkeypatch.setattr(journal_retention_module, "journal_file_claim", unlink_under_claim)

    sweep.prune_older_than(time.time())

    assert sweep.removed == 1
    assert not newest.exists()
    assert sweep._total_bytes == 0


def test_journal_claim_lock_namespace_is_bounded(tmp_path) -> None:
    journals = tmp_path / "journals"
    journals.mkdir()

    for index in range(_LOCK_BUCKET_COUNT * 2):
        with journal_file_claim(journals / f"session-{index}.sqlite", blocking=True) as claimed:
            assert claimed is True

    lock_files = list(journals.glob(".easycat-journal-*.lock"))
    assert 1 <= len(lock_files) <= _LOCK_BUCKET_COUNT


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
