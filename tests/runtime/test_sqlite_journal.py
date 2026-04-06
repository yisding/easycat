"""Tests for the SqliteJournal backend."""

from __future__ import annotations

import sqlite3

import pytest

from easycat.runtime.journal import SqliteJournal, run_retention
from easycat.runtime.records import (
    ErrorInfo,
    JournalRecordKind,
)


@pytest.fixture
def journal(tmp_path):
    j = SqliteJournal("test-session", data_dir=tmp_path)
    yield j
    j.close()


class TestSqliteJournalBasics:
    def test_append_and_read(self, journal):
        seq = journal.append(
            kind=JournalRecordKind.EVENT,
            name="test_event",
            session_id="test-session",
            data={"key": "value"},
        )
        assert seq == 1
        records = journal.read()
        assert len(records) == 1
        assert records[0].sequence == 1
        assert records[0].name == "test_event"
        assert records[0].data == {"key": "value"}

    def test_monotonic_sequence(self, journal):
        seqs = []
        for i in range(5):
            s = journal.append(
                kind=JournalRecordKind.EVENT,
                name=f"event_{i}",
                session_id="test-session",
            )
            seqs.append(s)
        assert seqs == [1, 2, 3, 4, 5]

    def test_read_with_start(self, journal):
        for i in range(5):
            journal.append(
                kind=JournalRecordKind.EVENT,
                name=f"event_{i}",
                session_id="test-session",
            )
        records = journal.read(start=3)
        assert len(records) == 3
        assert records[0].sequence == 3

    def test_read_with_limit(self, journal):
        for i in range(5):
            journal.append(
                kind=JournalRecordKind.EVENT,
                name=f"event_{i}",
                session_id="test-session",
            )
        records = journal.read(limit=2)
        assert len(records) == 2

    def test_slice_by_kind(self, journal):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="ev",
            session_id="test-session",
        )
        journal.append(
            kind=JournalRecordKind.METRIC,
            name="met",
            session_id="test-session",
        )
        events = journal.slice(kind=JournalRecordKind.EVENT)
        assert len(events) == 1
        assert events[0].name == "ev"

    def test_slice_by_session(self, journal):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="ev",
            session_id="test-session",
        )
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="ev2",
            session_id="other-session",
        )
        records = journal.slice(session_id="test-session")
        assert len(records) == 1

    def test_error_info_roundtrip(self, journal):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="fail",
            session_id="test-session",
            error=ErrorInfo(type="ValueError", message="bad", traceback="line 1"),
        )
        rec = journal.read()[0]
        assert rec.error is not None
        assert rec.error.type == "ValueError"
        assert rec.error.message == "bad"
        assert rec.error.traceback == "line 1"

    def test_tags_roundtrip(self, journal):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="tagged",
            session_id="test-session",
            tags=frozenset({"a", "b"}),
        )
        rec = journal.read()[0]
        assert rec.tags == frozenset({"a", "b"})

    def test_timing_populated(self, journal):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="timed",
            session_id="test-session",
        )
        rec = journal.read()[0]
        assert rec.timing.wall_ns > 0
        assert rec.timing.mono_ns > 0


class TestSqliteJournalLifecycle:
    def test_close_sets_clean_marker(self, tmp_path):
        j = SqliteJournal("sess", data_dir=tmp_path)
        j.append(kind=JournalRecordKind.EVENT, name="ev", session_id="sess")
        j.close()

        # Verify clean_close marker was written.
        conn = sqlite3.connect(str(tmp_path / "journals" / "sess.sqlite"))
        row = conn.execute("SELECT value FROM session_state WHERE key = 'clean_close'").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "1"

    def test_flush_commits_and_continues(self, journal):
        journal.append(kind=JournalRecordKind.EVENT, name="ev1", session_id="test-session")
        journal.flush()
        journal.append(kind=JournalRecordKind.EVENT, name="ev2", session_id="test-session")
        records = journal.read()
        assert len(records) == 2

    def test_degraded_mode_on_error(self, journal):
        assert not journal.degraded
        # Force an error by closing the connection behind the journal's back.
        journal._conn.close()
        journal._closed = False  # hack to allow append attempt
        seq = journal.append(
            kind=JournalRecordKind.EVENT,
            name="fail",
            session_id="test-session",
        )
        assert seq == -1
        assert journal.degraded

    def test_double_close_is_safe(self, tmp_path):
        j = SqliteJournal("sess", data_dir=tmp_path)
        j.close()
        j.close()  # should not raise

    def test_wal_mode_enabled(self, tmp_path):
        j = SqliteJournal("sess", data_dir=tmp_path)
        # Check via a second connection.
        conn = sqlite3.connect(str(tmp_path / "journals" / "sess.sqlite"))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        j.close()
        assert mode == "wal"


class TestCrashRecovery:
    def test_unclean_shutdown_detected(self, tmp_path):
        # First session: write records but do NOT close cleanly.
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="ev1", session_id="sess")
        j1.append(kind=JournalRecordKind.EVENT, name="ev2", session_id="sess")
        # Simulate crash: commit the transaction but skip close().
        j1._conn.execute("COMMIT")
        j1._conn.close()
        j1._closed = True

        # Second session: reopen same session_id — should detect unclean shutdown.
        j2 = SqliteJournal("sess", data_dir=tmp_path)
        assert j2._recovered is True

        # Recovery marker should be at sequence=0.
        records = j2.read(start=0)
        recovery = [r for r in records if r.kind == JournalRecordKind.RECOVERY]
        assert len(recovery) == 1
        assert recovery[0].sequence == 0
        assert recovery[0].name == "recovered_session"
        j2.close()

    def test_crash_dump_promoted(self, tmp_path):
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="ev", session_id="sess")
        j1._conn.execute("COMMIT")
        j1._conn.close()
        j1._closed = True

        j2 = SqliteJournal("sess", data_dir=tmp_path)
        j2.close()

        crash_dump = tmp_path / "crash-dumps" / "sess.sqlite"
        assert crash_dump.exists()

    def test_clean_close_no_recovery(self, tmp_path):
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="ev", session_id="sess")
        j1.close()

        j2 = SqliteJournal("sess", data_dir=tmp_path)
        assert j2._recovered is False
        records = j2.read(start=0)
        recovery = [r for r in records if r.kind == JournalRecordKind.RECOVERY]
        assert len(recovery) == 0
        j2.close()


class TestRetention:
    def _make_journal(self, tmp_path, session_id):
        j = SqliteJournal(session_id, data_dir=tmp_path)
        j.append(kind=JournalRecordKind.EVENT, name="ev", session_id=session_id)
        j.close()

    def test_retention_by_count(self, tmp_path):
        for i in range(5):
            self._make_journal(tmp_path, f"sess-{i}")

        removed = run_retention(tmp_path, max_sessions=3, max_bytes=10 * 1024 * 1024 * 1024)
        assert removed == 2
        remaining = list((tmp_path / "journals").glob("*.sqlite"))
        assert len(remaining) == 3

    def test_retention_archives(self, tmp_path):
        for i in range(3):
            self._make_journal(tmp_path, f"sess-{i}")

        run_retention(tmp_path, max_sessions=1, mode="archive")
        archives = list((tmp_path / "archive").glob("*.tar.gz"))
        assert len(archives) == 2

    def test_retention_delete_mode(self, tmp_path):
        for i in range(3):
            self._make_journal(tmp_path, f"sess-{i}")

        run_retention(tmp_path, max_sessions=1, mode="delete")
        assert not (tmp_path / "archive").exists()
        remaining = list((tmp_path / "journals").glob("*.sqlite"))
        assert len(remaining) == 1

    def test_retention_no_journals_dir(self, tmp_path):
        # Should not crash if the directory doesn't exist.
        removed = run_retention(tmp_path / "nonexistent")
        assert removed == 0
