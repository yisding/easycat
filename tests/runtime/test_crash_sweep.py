"""Tests for the orphaned-journal crash-durability sweep."""

from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest

from easycat.runtime import SqliteJournal, sweep_crashed_journals
from easycat.runtime.crash_sweep import (
    _copy_journal_to_crash_dump,
    _crashed_state,
)
from easycat.runtime.records import JournalRecordKind


def _dead_pid() -> int:
    """Return a PID that has definitely exited (so it reads as not-alive)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _crash_one(session_id: str, tmp_path) -> None:
    """Write a journal with rows and abandon it as a dead foreign process.

    Stamps the ``live_pid`` marker with an exited PID so the sweep's
    liveness check treats it like a genuinely-crashed process (the test
    runs in a live process whose own PID would otherwise read as alive).
    """
    j = SqliteJournal(session_id, data_dir=tmp_path)
    j.append(kind=JournalRecordKind.EVENT, name="ev", session_id=session_id)
    # append() already committed each record; rewrite the liveness marker to
    # a dead PID, then drop the connection to simulate a crash (no close()).
    j._conn.execute("COMMIT")
    j._conn.execute(
        "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
        (str(_dead_pid()),),
    )
    j._conn.commit()
    j._conn.close()
    j._closed = True


def test_sweep_promotes_crashed_orphan(tmp_path) -> None:
    _crash_one("orphan", tmp_path)
    journal = tmp_path / "journals" / "orphan.sqlite"
    assert journal.exists()

    promoted = sweep_crashed_journals(tmp_path)

    assert promoted == 1
    crash = tmp_path / "crash-dumps" / "orphan.sqlite"
    assert crash.exists()
    # The source is removed so it stops accumulating in journals/.
    assert not journal.exists()


def test_sweep_leaves_clean_closed_journal(tmp_path) -> None:
    j = SqliteJournal("clean", data_dir=tmp_path)
    j.append(kind=JournalRecordKind.EVENT, name="ev", session_id="clean")
    j.close()  # writes the clean_close marker

    promoted = sweep_crashed_journals(tmp_path)

    assert promoted == 0
    assert (tmp_path / "journals" / "clean.sqlite").exists()
    assert not (tmp_path / "crash-dumps" / "clean.sqlite").exists()


def test_sweep_skips_empty_journal(tmp_path) -> None:
    # A journal file with schema but no rows (and no clean_close) is not a
    # crash — nothing to recover, so leave it alone even after its owning
    # process is gone.
    j = SqliteJournal("empty", data_dir=tmp_path)
    j._conn.execute(
        "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
        (str(_dead_pid()),),
    )
    j._conn.commit()
    j._conn.close()
    j._closed = True

    assert _crashed_state(tmp_path / "journals" / "empty.sqlite") == "empty"

    promoted = sweep_crashed_journals(tmp_path)
    assert promoted == 0
    assert not (tmp_path / "crash-dumps" / "empty.sqlite").exists()


def test_sweep_skips_locked_live_journal_without_raising(tmp_path) -> None:
    # An open live session holds a write transaction on its journal; the
    # sweep must classify it as live (skip), not promote it, and not raise.
    # A separate session id forces the orphan-sweep path (not same-id
    # recovery) so we exercise the liveness probe directly.
    live = SqliteJournal("live", data_dir=tmp_path)
    live.append(kind=JournalRecordKind.EVENT, name="ev", session_id="live")
    try:
        promoted = sweep_crashed_journals(tmp_path)
    finally:
        live.close()

    assert promoted == 0
    # The live journal stays put; it was never promoted.
    assert (tmp_path / "journals" / "live.sqlite").exists()
    assert not (tmp_path / "crash-dumps" / "live.sqlite").exists()


def test_sweep_skips_malformed_sqlite_without_raising(tmp_path) -> None:
    journals = tmp_path / "journals"
    journals.mkdir()
    bad = journals / "bad.sqlite"
    bad.write_text("not a sqlite database")

    assert _crashed_state(bad) == "skip"
    assert sweep_crashed_journals(tmp_path) == 0
    assert bad.exists()


def test_sqlite_journal_open_ignores_malformed_sibling_journal(tmp_path) -> None:
    journals = tmp_path / "journals"
    journals.mkdir()
    bad = journals / "bad.sqlite"
    bad.write_text("not a sqlite database")

    journal = SqliteJournal("fresh", data_dir=tmp_path)
    try:
        assert (journals / "fresh.sqlite").exists()
        assert bad.exists()
    finally:
        journal.close()


def test_sweep_skips_the_caller_owned_path(tmp_path) -> None:
    _crash_one("mine", tmp_path)
    own = tmp_path / "journals" / "mine.sqlite"

    promoted = sweep_crashed_journals(tmp_path, skip=own)

    assert promoted == 0
    assert own.exists()
    assert not (tmp_path / "crash-dumps" / "mine.sqlite").exists()


def test_sweep_runs_on_next_sqlite_open(tmp_path) -> None:
    # A *different* session id whose process crashed is promoted the next
    # time any SqliteJournal opens — the same-id recovery path never fires
    # for an orphaned id.
    _crash_one("ghost", tmp_path)
    assert (tmp_path / "journals" / "ghost.sqlite").exists()

    j2 = SqliteJournal("fresh", data_dir=tmp_path)
    try:
        crash = tmp_path / "crash-dumps" / "ghost.sqlite"
        assert crash.exists()
        assert not (tmp_path / "journals" / "ghost.sqlite").exists()
    finally:
        j2.close()


def test_sweep_does_not_promote_own_journal_on_open(tmp_path) -> None:
    # Re-opening the same crashed id must go through the same-id recovery
    # path (which writes a recovery marker), not the orphan sweep.
    _crash_one("sess", tmp_path)

    j2 = SqliteJournal("sess", data_dir=tmp_path)
    try:
        # Same-id recovery promotes via _reconcile_prior_session, marking
        # _recovered True; the sweep must have left this path alone.
        assert j2._recovered is True
    finally:
        j2.close()


def test_sweep_missing_journals_dir_is_noop(tmp_path) -> None:
    assert sweep_crashed_journals(tmp_path) == 0


def test_promoted_crash_dump_preserves_records(tmp_path) -> None:
    _crash_one("payload", tmp_path)
    sweep_crashed_journals(tmp_path)

    crash = tmp_path / "crash-dumps" / "payload.sqlite"
    conn = sqlite3.connect(f"file:{crash}?mode=ro", uri=True)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM journal ORDER BY sequence")]
    finally:
        conn.close()
    assert "ev" in names


def test_crashed_state_classification(tmp_path) -> None:
    # crashed: rows, no clean_close
    _crash_one("c", tmp_path)
    assert _crashed_state(tmp_path / "journals" / "c.sqlite") == "crashed"

    # clean: clean_close marker present
    j = SqliteJournal("ok", data_dir=tmp_path)
    j.append(kind=JournalRecordKind.EVENT, name="ev", session_id="ok")
    j.close()
    assert _crashed_state(tmp_path / "journals" / "ok.sqlite") == "clean"

    # skip: file without the journal schema
    bare = tmp_path / "journals" / "bare.sqlite"
    conn = sqlite3.connect(bare)
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    assert _crashed_state(bare) == "skip"


def test_copy_journal_to_crash_dump_copies_sidecars(tmp_path) -> None:
    _crash_one("side", tmp_path)
    src = tmp_path / "journals" / "side.sqlite"
    dst = tmp_path / "crash-dumps" / "side.sqlite"
    dst.parent.mkdir(parents=True, exist_ok=True)

    _copy_journal_to_crash_dump(src, dst)

    assert dst.exists()
    conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        count = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    finally:
        conn.close()
    assert count >= 1


@pytest.mark.parametrize("bad", ["", "0", None])
def test_crashed_state_treats_falsey_clean_close_as_crashed(tmp_path, bad) -> None:
    # A clean_close value that is empty/"0"/NULL is not a clean close.
    _crash_one("falsey", tmp_path)
    db = tmp_path / "journals" / "falsey.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('clean_close', ?)",
            (bad,),
        )
        conn.commit()
    finally:
        conn.close()
    assert _crashed_state(db) == "crashed"
