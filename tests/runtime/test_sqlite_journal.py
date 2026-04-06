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


class TestSqliteHotPathBehavior:
    """AC1.17: verify checkpoint-on-close and no-fsync-on-hot-path properties."""

    def test_checkpoint_on_close(self, tmp_path):
        """After close(), the WAL should be checkpointed (truncated to near-zero)."""
        j = SqliteJournal("sess-ckpt", data_dir=tmp_path)
        for i in range(100):
            j.append(
                kind=JournalRecordKind.EVENT,
                name=f"event_{i}",
                session_id="sess-ckpt",
                data={"i": i},
            )
        # Flush to ensure records are in the WAL.
        j.flush()
        wal_path = tmp_path / "journals" / "sess-ckpt.sqlite-wal"
        # WAL should be non-trivial before close.
        assert wal_path.exists()
        wal_size_before = wal_path.stat().st_size
        assert wal_size_before > 0, "WAL should contain data before close"

        j.close()

        # After close(), PRAGMA wal_checkpoint(TRUNCATE) should shrink the WAL.
        if wal_path.exists():
            wal_size_after = wal_path.stat().st_size
            assert wal_size_after == 0, (
                f"WAL should be truncated to 0 after close, got {wal_size_after}"
            )

        # All records should still be readable from the main DB file.
        conn = sqlite3.connect(str(tmp_path / "journals" / "sess-ckpt.sqlite"))
        count = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        conn.close()
        assert count == 100

    @pytest.mark.skipif(
        __import__("sys").platform != "linux" or __import__("shutil").which("strace") is None,
        reason="strace-based fsync counting requires Linux with strace installed",
    )
    def test_no_hot_path_fsync(self, tmp_path):
        """During normal appends, zero fsync/fdatasync calls should occur.

        Uses strace to count fsync/fdatasync syscalls on the journal fd.
        Only runs on Linux; skipped on other platforms with a log line.
        """
        import subprocess
        import textwrap

        script = textwrap.dedent(f"""\
            import sys
            sys.path.insert(0, "src")
            from easycat.runtime.journal import SqliteJournal
            from easycat.runtime.records import JournalRecordKind

            j = SqliteJournal("strace-sess", data_dir="{tmp_path}")
            for i in range(100):
                j.append(
                    kind=JournalRecordKind.EVENT,
                    name=f"event_{{i}}",
                    session_id="strace-sess",
                )
            j.flush()
            # Do NOT close — close triggers the checkpoint which may fsync.
            # We only care about the hot path.
            print("done")
        """)

        result = subprocess.run(
            ["strace", "-e", "trace=fsync,fdatasync", "-f", "-c", "python", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # strace -c prints a summary table to stderr.  If fsync/fdatasync
        # appear, the count will be > 0.  On a clean run they should not
        # appear at all (or appear with 0 calls).
        stderr = result.stderr
        fsync_calls = 0
        for line in stderr.splitlines():
            # strace -c output lines look like:
            #   % time     seconds  usecs/call     calls    errors syscall
            #   ------ ----------- ----------- --------- --------- -------
            #   100.00    0.000010          10         1           fsync
            parts = line.split()
            if parts and parts[-1] in ("fsync", "fdatasync"):
                try:
                    fsync_calls += int(parts[-3])
                except (ValueError, IndexError):
                    pass
        assert fsync_calls == 0, (
            f"Expected zero fsync/fdatasync on hot path, got {fsync_calls}.\n"
            f"strace output:\n{stderr}"
        )
