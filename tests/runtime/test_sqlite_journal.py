"""Tests for the SqliteJournal backend and adapter backends."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import textwrap
from unittest import mock

import pytest

from easycat.runtime.journal import (
    JournalView,
    LitestreamSqliteJournal,
    SqliteJournal,
    create_journal,
    run_retention,
)
from easycat.runtime.records import (
    ErrorInfo,
    JournalRecordKind,
    RecoveredSessionMarker,
)
from easycat.runtime.safe_defaults import safe_env_snapshot


def _libsql_available() -> bool:
    """Check if the libsql_experimental SDK is importable."""
    try:
        import libsql_experimental  # noqa: F401

        return True
    except ImportError:
        return False


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

    async def test_follow_from_zero_does_not_emit_spurious_gap(self, journal):
        # from_sequence=0 must replay history without a synthetic follow_gap:
        # SQLite retains every record, so the first yielded record is the real
        # record at sequence 1, not a BufferOverflow gap notice.
        view = JournalView(journal)
        journal.append(kind=JournalRecordKind.EVENT, name="e1", session_id="test-session")
        journal.append(kind=JournalRecordKind.EVENT, name="e2", session_id="test-session")

        gen = view.follow(from_sequence=0, poll_interval=0.01)
        first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        await gen.aclose()

        assert first.sequence == 1
        assert first.name == "e1"
        assert "dropped_from" not in first.data


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

    def test_degraded_persists_marker_to_file(self, tmp_path):
        # Trigger degraded mode with a write the connection survives (non-JSON
        # data raises before the INSERT) so the best-effort marker can be
        # written and committed to disk.
        j = SqliteJournal("sess", data_dir=tmp_path)
        circular: dict[str, object] = {}
        circular["self"] = circular
        assert (
            j.append(
                kind=JournalRecordKind.EVENT,
                name="fail",
                session_id="sess",
                data=circular,
            )
            == -1
        )
        assert j.degraded

        # The degradation signal must be recoverable from the file itself.
        conn = sqlite3.connect(f"file:{tmp_path / 'journals' / 'sess.sqlite'}?mode=ro", uri=True)
        state = conn.execute("SELECT value FROM session_state WHERE key = 'degraded'").fetchone()
        degraded_rows = conn.execute(
            "SELECT sequence, name FROM journal WHERE kind = ?",
            (JournalRecordKind.DEGRADED.value,),
        ).fetchall()
        conn.close()
        assert state == ("1",)
        assert degraded_rows == [(-1, "journal_degraded")]
        j.close()

    def test_readonly_journal_surfaces_persisted_degraded(self, tmp_path):
        from easycat.runtime.journal import ReadonlySqliteJournal

        j = SqliteJournal("sess", data_dir=tmp_path)
        circular: dict[str, object] = {}
        circular["self"] = circular
        j.append(
            kind=JournalRecordKind.EVENT,
            name="fail",
            session_id="sess",
            data=circular,
        )
        j.close()

        # A read-only journal opened fresh from the file (no live flag) must
        # still report degradation via the persisted session_state marker.
        ro = ReadonlySqliteJournal(tmp_path / "journals" / "sess.sqlite")
        assert ro.degraded is True

    def test_reused_session_clears_persisted_degraded_marker(self, tmp_path):
        from easycat.runtime.journal import ReadonlySqliteJournal

        j1 = SqliteJournal("sess", data_dir=tmp_path)
        circular: dict[str, object] = {}
        circular["self"] = circular
        assert (
            j1.append(
                kind=JournalRecordKind.EVENT,
                name="fail",
                session_id="sess",
                data=circular,
            )
            == -1
        )
        assert j1.degraded is True
        j1.close()

        j2 = SqliteJournal("sess", data_dir=tmp_path)
        assert j2.degraded is False
        assert j2.read(start=0) == []
        j2.append(kind=JournalRecordKind.EVENT, name="fresh", session_id="sess")
        j2.close()

        ro = ReadonlySqliteJournal(tmp_path / "journals" / "sess.sqlite")
        assert ro.degraded is False
        records = ro.read(start=0)
        assert [record.name for record in records] == ["fresh"]

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
        # Simulate crash: skip close().  append() already committed each record
        # via the production path, so the records are durable without a manual
        # COMMIT — that is exactly the SIGKILL guarantee we rely on.
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

    def test_recovery_marker_roundtrips_as_typed_subclass(self, tmp_path):
        # First session: write two records, then simulate an unclean crash.
        # append() commits each record via the production path; no manual COMMIT.
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="ev1", session_id="sess")
        j1.append(kind=JournalRecordKind.EVENT, name="ev2", session_id="sess")
        j1._conn.close()
        j1._closed = True

        # Second session: the recovery marker must round-trip through SQLite as
        # a RecoveredSessionMarker with its typed fields populated, not collapse
        # to a base JournalRecord.
        j2 = SqliteJournal("sess", data_dir=tmp_path)
        records = j2.read(start=0)
        recovery = [r for r in records if r.kind == JournalRecordKind.RECOVERY]
        assert len(recovery) == 1
        marker = recovery[0]
        assert isinstance(marker, RecoveredSessionMarker)
        assert marker.recovered_record_count == 2
        assert marker.original_session_id == "sess"
        j2.close()

    def test_recovery_resets_sequence_and_drops_prior_records(self, tmp_path):
        # First session: write two records, then simulate an unclean crash.
        # append() commits each record via the production path; no manual COMMIT.
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="ev1", session_id="sess")
        j1.append(kind=JournalRecordKind.EVENT, name="ev2", session_id="sess")
        j1._conn.close()
        j1._closed = True

        # Second session: recovery must truncate the live journal so the new
        # session starts fresh at sequence=1 (DURABILITY.md contract).
        j2 = SqliteJournal("sess", data_dir=tmp_path)
        assert j2._recovered is True

        # No prior-session EVENT records leak into the live journal.
        before = j2.read(start=0)
        assert [r.name for r in before if r.kind == JournalRecordKind.EVENT] == []

        # The first real append after recovery starts at sequence=1.
        seq = j2.append(kind=JournalRecordKind.EVENT, name="fresh", session_id="sess")
        assert seq == 1

        events = [r for r in j2.read(start=0) if r.kind == JournalRecordKind.EVENT]
        assert [r.name for r in events] == ["fresh"]
        assert [r.sequence for r in events] == [1]
        j2.close()

    def test_crash_dump_promoted(self, tmp_path):
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="ev", session_id="sess")
        # append() commits via the production path; no manual COMMIT.
        j1._conn.close()
        j1._closed = True

        j2 = SqliteJournal("sess", data_dir=tmp_path)
        j2.close()

        crash_dump = tmp_path / "crash-dumps" / "sess.sqlite"
        assert crash_dump.exists()

    def test_crash_dump_copy_failure_leaves_consistent_state(self, tmp_path):
        # If the crash-dump copy raises after the connection was closed (and
        # before the DELETE/reopen), recovery must not leave the journal in a
        # half-recovered state: no recovery marker may be emitted alongside
        # un-truncated prior-session rows, the connection must be reopened so
        # the rest of __init__ runs, and the new session must still start fresh.
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="ev1", session_id="sess")
        j1.append(kind=JournalRecordKind.EVENT, name="ev2", session_id="sess")
        # append() commits via the production path; no manual COMMIT.
        j1._conn.close()
        j1._closed = True

        with mock.patch(
            "easycat.runtime.journal.shutil.copy2",
            side_effect=OSError("disk full"),
        ):
            j2 = SqliteJournal("sess", data_dir=tmp_path)

        # The copy failed, so recovery did not fully succeed: no recovery marker.
        assert j2._recovered is False
        records = j2.read(start=0)
        assert [r for r in records if r.kind == JournalRecordKind.RECOVERY] == []
        # Prior-session rows were truncated — the new session starts fresh.
        assert [r.name for r in records if r.kind == JournalRecordKind.EVENT] == []
        # The connection was reopened: appends work and start at sequence=1.
        seq = j2.append(kind=JournalRecordKind.EVENT, name="fresh", session_id="sess")
        assert seq == 1
        assert j2.degraded is False
        j2.close()

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

    def test_append_after_finalize_clears_clean_marker(self, tmp_path):
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="before_finalize", session_id="sess")
        j1.finalize()

        marker = j1._conn.execute(
            "SELECT value FROM session_state WHERE key = 'clean_close'"
        ).fetchone()
        assert marker == ("1",)

        j1.append(kind=JournalRecordKind.EVENT, name="after_finalize", session_id="sess")
        marker = j1._conn.execute(
            "SELECT value FROM session_state WHERE key = 'clean_close'"
        ).fetchone()
        assert marker is None

        # Simulate a crash after the post-finalize write was committed, but
        # before close() could write a new clean_close marker.
        j1._conn.execute("COMMIT")
        j1._conn.close()
        j1._closed = True

        j2 = SqliteJournal("sess", data_dir=tmp_path)
        assert j2._recovered is True
        records = j2.read(start=0)
        recovery = [r for r in records if r.kind == JournalRecordKind.RECOVERY]
        assert len(recovery) == 1
        # The live journal is truncated on recovery; the prior session's
        # records survive only in the crash dump, not the new session.
        assert [r.name for r in records if r.kind == JournalRecordKind.EVENT] == []
        crash_dump = tmp_path / "crash-dumps" / "sess.sqlite"
        crash_conn = sqlite3.connect(str(crash_dump))
        dumped = [
            row[0]
            for row in crash_conn.execute("SELECT name FROM journal ORDER BY sequence").fetchall()
        ]
        crash_conn.close()
        assert dumped == ["before_finalize", "after_finalize"]
        j2.close()

    def test_uncommitted_append_after_finalize_keeps_clean_marker(self, tmp_path):
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="before_finalize", session_id="sess")
        j1.finalize()

        j1.append(kind=JournalRecordKind.EVENT, name="after_finalize", session_id="sess")

        # Simulate a crash before the post-finalize transaction commits.
        # SQLite rolls back both the new record and the clean_close marker
        # deletion, so the durable database should still look clean.
        j1._conn.close()
        j1._closed = True

        conn = sqlite3.connect(str(tmp_path / "journals" / "sess.sqlite"))
        marker = conn.execute(
            "SELECT value FROM session_state WHERE key = 'clean_close'"
        ).fetchone()
        durable_events = [
            row[0] for row in conn.execute("SELECT name FROM journal ORDER BY sequence").fetchall()
        ]
        conn.close()
        assert marker == ("1",)
        assert durable_events == ["before_finalize"]

        j2 = SqliteJournal("sess", data_dir=tmp_path)
        assert j2._recovered is False
        assert [r for r in j2.read(start=0) if r.kind == JournalRecordKind.RECOVERY] == []
        assert not (tmp_path / "crash-dumps" / "sess.sqlite").exists()
        j2.close()

    def test_failed_append_after_finalize_keeps_clean_marker(self, tmp_path):
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="before_finalize", session_id="sess")
        j1.finalize()

        circular: dict[str, object] = {}
        circular["self"] = circular
        assert (
            j1.append(
                kind=JournalRecordKind.EVENT,
                name="after_finalize",
                session_id="sess",
                data=circular,
            )
            == -1
        )
        j1.flush()
        j1._conn.close()
        j1._closed = True

        conn = sqlite3.connect(str(tmp_path / "journals" / "sess.sqlite"))
        marker = conn.execute(
            "SELECT value FROM session_state WHERE key = 'clean_close'"
        ).fetchone()
        durable_events = [
            row[0] for row in conn.execute("SELECT name FROM journal ORDER BY sequence").fetchall()
        ]
        conn.close()
        assert marker == ("1",)
        assert durable_events == ["before_finalize"]

        j2 = SqliteJournal("sess", data_dir=tmp_path)
        assert j2._recovered is False
        assert not (tmp_path / "crash-dumps" / "sess.sqlite").exists()
        j2.close()

    def test_append_commits_without_manual_flush(self, tmp_path):
        """Every append() must be durable on its own — a second read-only
        connection sees the row before any flush()/finalize()/close().

        This is the unit-level guard for the DURABILITY.md SIGKILL contract:
        if the per-append commit regresses, the read below returns nothing.
        """
        j = SqliteJournal("sess", data_dir=tmp_path)
        j.append(kind=JournalRecordKind.EVENT, name="ev1", session_id="sess")
        j.append(kind=JournalRecordKind.EVENT, name="ev2", session_id="sess")

        # Read via an independent read-only connection — sees only committed data.
        ro = sqlite3.connect(f"file:{tmp_path / 'journals' / 'sess.sqlite'}?mode=ro", uri=True)
        names = [
            row[0]
            for row in ro.execute(
                "SELECT name FROM journal WHERE kind = ? ORDER BY sequence",
                (JournalRecordKind.EVENT.value,),
            ).fetchall()
        ]
        ro.close()
        assert names == ["ev1", "ev2"]
        j.close()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="SIGKILL not available on Windows",
    )
    def test_sigkill_preserves_committed_records(self, tmp_path):
        """A child process writes records, parent SIGKILLs it, reopening the
        journal recovers every committed record and emits a RECOVERY marker."""
        n_records = 50
        script = textwrap.dedent(f"""\
            import sys, time
            sys.path.insert(0, "src")
            from easycat.runtime.journal import SqliteJournal
            from easycat.runtime.records import JournalRecordKind

            j = SqliteJournal("crash-sess", data_dir="{tmp_path}")
            for i in range({n_records}):
                j.append(
                    kind=JournalRecordKind.EVENT,
                    name=f"event_{{i}}",
                    session_id="crash-sess",
                )
            # No manual flush(): the production append() path must commit each
            # record on its own so SIGKILL preserves them (DURABILITY.md).
            print("READY", flush=True)
            time.sleep(60)
        """)

        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = proc.stdout.readline().strip()
        assert line == "READY", f"Child did not signal ready: {line}"

        proc.send_signal(signal.SIGKILL)
        proc.wait()

        j2 = SqliteJournal("crash-sess", data_dir=tmp_path)
        assert j2._recovered is True

        records = j2.read(start=0)
        recovery = [r for r in records if r.kind == JournalRecordKind.RECOVERY]
        assert len(recovery) == 1
        assert recovery[0].sequence == 0
        assert recovery[0].data["recovered_record_count"] == n_records

        # The live journal is truncated on recovery; committed records are
        # preserved in the crash dump for offline post-mortem analysis.
        event_records = [r for r in records if r.kind == JournalRecordKind.EVENT]
        assert event_records == []

        j2.close()

        crash_dump = tmp_path / "crash-dumps" / "crash-sess.sqlite"
        assert crash_dump.exists()
        crash_conn = sqlite3.connect(str(crash_dump))
        dumped = crash_conn.execute(
            "SELECT COUNT(*) FROM journal WHERE kind = ?",
            (JournalRecordKind.EVENT.value,),
        ).fetchone()[0]
        crash_conn.close()
        assert dumped == n_records


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
        """Hot-path appends + flush must not add any fsync/fdatasync calls.

        The SQLite WAL bootstrap (creating the ``-wal`` file, writing the
        journal-mode header) legitimately emits a small number of fsync
        calls regardless of ``synchronous=NORMAL``; those happen once per
        session, not per turn, and are not what this test is guarding.

        To isolate the per-turn hot path we compare two runs under strace:

        - **baseline**: open journal, ``flush()``, exit
        - **full**: open journal, 100 appends, ``flush()``, exit

        Setup and shutdown fsync costs cancel out in the delta.  What
        remains is whatever the 100 appends + commit contribute — which
        under ``PRAGMA synchronous=NORMAL`` in WAL mode should be zero.
        """
        import subprocess
        import textwrap

        def _count_fsync(data_dir, appends: int):
            script = textwrap.dedent(f"""\
                import sys
                sys.path.insert(0, "src")
                from easycat.runtime.journal import SqliteJournal
                from easycat.runtime.records import JournalRecordKind

                j = SqliteJournal("strace-sess", data_dir="{data_dir}")
                for i in range({appends}):
                    j.append(
                        kind=JournalRecordKind.EVENT,
                        name=f"event_{{i}}",
                        session_id="strace-sess",
                    )
                j.flush()
                # Do NOT close — close triggers the checkpoint which fsyncs.
                print("done")
            """)
            result = subprocess.run(
                ["strace", "-e", "trace=fsync,fdatasync", "-f", "-c", "python", "-c", script],
                capture_output=True,
                text=True,
                timeout=30,
            )
            count = 0
            for line in result.stderr.splitlines():
                # strace -c summary rows:
                #   % time     seconds  usecs/call  calls  [errors]  syscall
                # The "errors" column is blank when no errors occur, so we
                # can't rely on negative indexing — "calls" is always at
                # position 3 from the start.
                parts = line.split()
                if len(parts) >= 5 and parts[-1] in ("fsync", "fdatasync"):
                    try:
                        count += int(parts[3])
                    except (ValueError, IndexError):
                        pass
            return count, result.stderr

        baseline_dir = tmp_path / "baseline"
        baseline_dir.mkdir()
        baseline, baseline_out = _count_fsync(baseline_dir, appends=0)

        hot_dir = tmp_path / "hot"
        hot_dir.mkdir()
        hot, hot_out = _count_fsync(hot_dir, appends=100)

        delta = hot - baseline
        assert delta == 0, (
            f"Expected zero hot-path fsync/fdatasync (baseline={baseline}, "
            f"full={hot}), got delta={delta}.\n"
            f"baseline strace:\n{baseline_out}\n"
            f"full strace:\n{hot_out}"
        )


# ── Litestream adapter tests ────────────────────────────────────


class TestLitestreamSqliteJournal:
    def test_fallback_when_binary_missing(self, tmp_path):
        """When litestream is not on PATH, adapter degrades to plain SqliteJournal."""
        with mock.patch("easycat.runtime.journal.shutil.which", return_value=None):
            j = LitestreamSqliteJournal(
                "test-ls-fallback",
                data_dir=tmp_path,
                replica_url="file:///tmp/replica",
            )
        # Should behave as a working journal (backed by SqliteJournal).
        seq = j.append(
            kind=JournalRecordKind.EVENT,
            name="ev1",
            session_id="test-ls-fallback",
            data={"x": 1},
        )
        assert seq == 1
        records = j.read()
        assert len(records) == 1
        assert records[0].name == "ev1"
        assert not j.degraded
        # Sidecar should not have been started.
        assert j._sidecar is None
        assert not j._litestream_available
        j.close()

    def test_no_replica_url_degrades(self, tmp_path):
        """Without a replica URL configured, adapter still functions."""
        j = LitestreamSqliteJournal(
            "test-ls-no-url",
            data_dir=tmp_path,
            replica_url="",
        )
        seq = j.append(
            kind=JournalRecordKind.EVENT,
            name="ev",
            session_id="test-ls-no-url",
        )
        assert seq == 1
        assert j._sidecar is None
        j.close()

    def test_factory_creates_litestream_adapter(self, tmp_path):
        """create_journal with backend='sqlite+litestream' returns the adapter."""
        with mock.patch("easycat.runtime.journal.shutil.which", return_value=None):
            j = create_journal(
                "test-factory-ls",
                debug="full",
                backend="sqlite+litestream",
                data_dir=str(tmp_path),
            )
        assert isinstance(j, LitestreamSqliteJournal)
        j.close()

    @pytest.mark.integration_live
    @pytest.mark.skipif(
        shutil.which("litestream") is None,
        reason="litestream binary not on PATH",
    )
    def test_litestream_sqlite_adapter_round_trip(self, tmp_path):
        """Integration: write records with litestream replicating to a file target."""
        replica_dir = tmp_path / "replica"
        replica_dir.mkdir()
        replica_url = f"file://{replica_dir}"

        j = LitestreamSqliteJournal(
            "test-ls-rt",
            data_dir=tmp_path,
            replica_url=replica_url,
        )
        assert j._litestream_available
        assert j._sidecar is not None

        for i in range(10):
            j.append(
                kind=JournalRecordKind.EVENT,
                name=f"event_{i}",
                session_id="test-ls-rt",
                data={"i": i},
            )
        j.flush()

        # Give litestream a moment to replicate, then close.
        import time

        time.sleep(2)
        j.close()

        # Restore from replica.
        import subprocess

        restore_path = tmp_path / "restored.sqlite"
        subprocess.run(
            ["litestream", "restore", "-o", str(restore_path), replica_url],
            check=True,
            timeout=10,
        )
        assert restore_path.exists()

        conn = sqlite3.connect(str(restore_path))
        count = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        conn.close()
        assert count >= 1, f"Expected records in restored DB, got {count}"


# ── libSQL adapter tests ────────────────────────────────────────


class TestLibsqlJournal:
    def test_fallback_when_sdk_missing(self, tmp_path):
        """When libsql_experimental is not installed, factory falls back to SQLite."""
        with mock.patch.dict("sys.modules", {"libsql_experimental": None}):
            j = create_journal(
                "test-libsql-fallback",
                debug="full",
                backend="libsql",
                data_dir=str(tmp_path),
            )
        # Should fall back to SqliteJournal, not LibsqlJournal.
        assert isinstance(j, SqliteJournal)
        j.close()

    @pytest.mark.integration_live
    @pytest.mark.skipif(
        not _libsql_available(),
        reason="libsql_experimental SDK not installed",
    )
    def test_libsql_adapter_round_trip(self, tmp_path):
        """Integration: round-trip through LibsqlJournal (local-only, no remote)."""
        from easycat.runtime.journal import LibsqlJournal

        j = LibsqlJournal("test-libsql-rt", data_dir=tmp_path)
        for i in range(5):
            j.append(
                kind=JournalRecordKind.EVENT,
                name=f"event_{i}",
                session_id="test-libsql-rt",
                data={"i": i},
            )
        records = j.read()
        assert len(records) == 5
        assert records[0].name == "event_0"
        assert records[4].data == {"i": 4}
        j.close()

    @pytest.mark.skipif(
        not _libsql_available(),
        reason="libsql_experimental SDK not installed",
    )
    def test_libsql_unclean_reuse_preserves_degraded_marker(self, tmp_path):
        """Unclean libSQL reuse retains prior rows, so the persisted ``degraded``
        marker must be preserved for file/bundle inspection.

        libSQL has no crash recovery: when a session id is reused without a
        ``clean_close`` marker the prior journal (including the
        ``journal_degraded`` row) is kept and appended to.  Clearing the
        ``degraded`` key there would desync the persisted state from the
        retained history, so it must survive.
        """
        from easycat.runtime.journal import LibsqlJournal, ReadonlySqliteJournal

        j1 = LibsqlJournal("sess-unclean", data_dir=tmp_path)
        circular: dict[str, object] = {}
        circular["self"] = circular
        assert (
            j1.append(
                kind=JournalRecordKind.EVENT,
                name="fail",
                session_id="sess-unclean",
                data=circular,
            )
            == -1
        )
        assert j1.degraded is True
        # close() does NOT write clean_close for libSQL — simulates unclean reuse.
        j1.close()

        # Reopen the same session id without a clean_close marker.
        j2 = LibsqlJournal("sess-unclean", data_dir=tmp_path)
        j2.close()

        ro = ReadonlySqliteJournal(tmp_path / "journals" / "sess-unclean.sqlite")
        assert ro.degraded is True
        # The persisted journal_degraded marker row is retained, not truncated.
        degraded_records = ro.slice(kind=JournalRecordKind.DEGRADED)
        assert [r.name for r in degraded_records] == ["journal_degraded"]

    @pytest.mark.skipif(
        not _libsql_available(),
        reason="libsql_experimental SDK not installed",
    )
    def test_libsql_clean_reuse_clears_degraded_marker(self, tmp_path):
        """Clean libSQL reuse truncates the prior journal, so its stale
        ``degraded`` marker must be cleared."""
        from easycat.runtime.journal import LibsqlJournal, ReadonlySqliteJournal

        j1 = LibsqlJournal("sess-clean", data_dir=tmp_path)
        circular: dict[str, object] = {}
        circular["self"] = circular
        assert (
            j1.append(
                kind=JournalRecordKind.EVENT,
                name="fail",
                session_id="sess-clean",
                data=circular,
            )
            == -1
        )
        assert j1.degraded is True
        # finalize() writes the clean_close marker — simulates a clean close.
        j1.finalize()
        j1.close()

        # Reopen the same session id after a clean close.
        j2 = LibsqlJournal("sess-clean", data_dir=tmp_path)
        assert j2.degraded is False
        assert j2.read(start=-1) == []
        j2.append(kind=JournalRecordKind.EVENT, name="fresh", session_id="sess-clean")
        j2.finalize()
        j2.close()

        ro = ReadonlySqliteJournal(tmp_path / "journals" / "sess-clean.sqlite")
        assert ro.degraded is False
        records = ro.read(start=0)
        assert [record.name for record in records] == ["fresh"]


# ── AC1.18: Credential redaction tests ──────────────────────────


class TestCredentialRedaction:
    def test_journal_adapter_credentials_redacted(self):
        """Synthetic secrets must not appear in the safe env snapshot.

        Non-secret adapter vars (EASYCAT_JOURNAL_LITESTREAM_REPLICA,
        EASYCAT_LIBSQL_URL) should appear if they are in the allowlist.
        Secret vars (AWS_SECRET_ACCESS_KEY, EASYCAT_LIBSQL_AUTH_TOKEN)
        must never appear.
        """
        env_overrides = {
            "EASYCAT_JOURNAL_LITESTREAM_REPLICA": "s3://bucket/path",
            "AWS_SECRET_ACCESS_KEY": "synthetic-aws-key",
            "EASYCAT_LIBSQL_URL": "libsql://org.turso.io",
            "EASYCAT_LIBSQL_AUTH_TOKEN": "synthetic-libsql-token",
        }
        with mock.patch.dict(os.environ, env_overrides, clear=False):
            snapshot = safe_env_snapshot()

        # Non-secret allowlisted vars should be present (sanitized to scheme://host).
        assert "EASYCAT_JOURNAL_LITESTREAM_REPLICA" in snapshot
        assert snapshot["EASYCAT_JOURNAL_LITESTREAM_REPLICA"] == "s3://bucket"
        assert "EASYCAT_LIBSQL_URL" in snapshot
        assert snapshot["EASYCAT_LIBSQL_URL"] == "libsql://org.turso.io"

        # Secret vars must NOT appear.
        assert "AWS_SECRET_ACCESS_KEY" not in snapshot
        assert "EASYCAT_LIBSQL_AUTH_TOKEN" not in snapshot

        # Ensure the synthetic secret values don't leak anywhere in the snapshot.
        all_values = " ".join(snapshot.values())
        assert "synthetic-aws-key" not in all_values
        assert "synthetic-libsql-token" not in all_values
