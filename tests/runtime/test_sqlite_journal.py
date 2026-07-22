"""Tests for the SqliteJournal backend and adapter backends."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import time
from collections.abc import Iterable
from pathlib import Path
from unittest import mock

import pytest

from easycat.runtime import (
    JournalView,
    LitestreamSqliteJournal,
    ReadonlySqliteJournal,
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
from easycat.validation.redaction import REDACTED_PHONE, REDACTED_SECRET


def _mode(path):
    return path.stat().st_mode & 0o777


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
            data={"label": "value"},
        )
        assert seq == 1
        records = journal.read()
        assert len(records) == 1
        assert records[0].sequence == 1
        assert records[0].name == "test_event"
        assert records[0].data == {"label": "value"}

    def test_append_applies_write_filter(self, journal):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="sensitive",
            session_id="test-session",
            data={
                "text": "phone +1 415 555 1212",
                "api_key": "short",
            },
            error=ErrorInfo(
                type="RuntimeError",
                message="Authorization: Bearer sk-testsecret123456",
            ),
        )

        record = journal.read()[0]
        assert record.data == {
            "api_key": REDACTED_SECRET,
            "text": f"phone {REDACTED_PHONE}",
        }
        assert record.error is not None
        assert record.error.message == f"Authorization: {REDACTED_SECRET}"

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

    def test_read_rejects_negative_limit(self, journal):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="event",
            session_id="test-session",
        )

        with pytest.raises(ValueError, match="limit"):
            journal.read(limit=-1)
        with pytest.raises(ValueError, match="limit"):
            ReadonlySqliteJournal(journal.db_path).read(limit=-1)

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

    def test_error_info_children_roundtrip_without_polluting_data(self, journal):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="pipeline_fail",
            session_id="test-session",
            data={"stage": "pipeline"},
            error=ErrorInfo(
                type="ExceptionGroup",
                message="pipeline failed",
                children=(
                    ErrorInfo(type="ValueError", message="bad input", notes="stage=stt"),
                    ErrorInfo(type="RuntimeError", message="provider failed", notes="stage=tts"),
                ),
            ),
        )

        rec = journal.read()[0]

        assert rec.data == {"stage": "pipeline"}
        assert rec.error is not None
        assert [child.type for child in rec.error.children] == ["ValueError", "RuntimeError"]
        assert rec.error.children[0].notes == "stage=stt"
        assert rec.error.children[1].message == "provider failed"

    def test_readonly_journal_loads_old_error_info_rows_without_children_column(self, tmp_path):
        db_path = tmp_path / "old.sqlite"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE journal (
                sequence     INTEGER PRIMARY KEY,
                session_id   TEXT    NOT NULL,
                kind         TEXT    NOT NULL,
                name         TEXT    NOT NULL DEFAULT '',
                wall_ns      INTEGER NOT NULL DEFAULT 0,
                mono_ns      INTEGER NOT NULL DEFAULT 0,
                cpu_ns       INTEGER NOT NULL DEFAULT 0,
                turn_id      TEXT,
                data         TEXT    NOT NULL DEFAULT '{}',
                error_type   TEXT,
                error_msg    TEXT,
                error_tb     TEXT,
                error_notes  TEXT,
                input_ref    TEXT,
                output_ref   TEXT,
                tags         TEXT    NOT NULL DEFAULT ''
            );
            """
        )
        conn.execute(
            "INSERT INTO journal "
            "(sequence, session_id, kind, name, data, error_type, error_msg) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "old-session", "event", "fail", '{"stage": "stt"}', "ValueError", "bad"),
        )
        conn.commit()
        conn.close()

        rec = ReadonlySqliteJournal(db_path).read()[0]

        assert rec.data == {"stage": "stt"}
        assert rec.error is not None
        assert rec.error.type == "ValueError"
        assert rec.error.children == ()

    def test_tags_roundtrip(self, journal):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="tagged",
            session_id="test-session",
            tags=frozenset({"a", "b"}),
        )
        rec = journal.read()[0]
        assert rec.tags == frozenset({"a", "b"})

    def test_slice_by_tags_exact_not_substring(self, journal):
        # SQL tag filtering must match whole tags, not substrings, so it agrees
        # with the in-memory backend's ``requested <= record.tags`` contract.
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="hit",
            session_id="test-session",
            tags=frozenset({"stt"}),
        )
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="miss",
            session_id="test-session",
            tags=frozenset({"not_stt"}),
        )
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="multi",
            session_id="test-session",
            tags=frozenset({"stt", "vad"}),
        )
        # ``stt`` matches the exact-tag records, never the ``not_stt`` substring.
        assert {r.name for r in journal.slice(tags=frozenset({"stt"}))} == {"hit", "multi"}
        # Subset semantics: ``stt+vad`` only matches the record carrying both.
        assert [r.name for r in journal.slice(tags=frozenset({"stt", "vad"}))] == ["multi"]

    def test_slice_by_tags_escapes_like_wildcards(self, journal):
        # ``_`` / ``%`` in a requested tag are literal, not SQL LIKE wildcards.
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="literal",
            session_id="test-session",
            tags=frozenset({"a_b"}),
        )
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="anychar",
            session_id="test-session",
            tags=frozenset({"axb"}),
        )
        assert [r.name for r in journal.slice(tags=frozenset({"a_b"}))] == ["literal"]

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
        from easycat.runtime import ReadonlySqliteJournal

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
        from easycat.runtime import ReadonlySqliteJournal

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

    def test_open_sweeps_orphaned_foreign_crash(self, tmp_path):
        # A *different* session id whose owning process is gone is promoted
        # to crash-dumps/ the next time any SqliteJournal opens — the same-id
        # recovery path never fires for an orphaned id.
        j1 = SqliteJournal("ghost", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="ev", session_id="ghost")
        # Mark its liveness PID dead, then crash (no close()).
        j1._conn.execute("COMMIT")
        j1._conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', '1')"
        )
        # PID 1 (init) is alive but not signalable -> reads as alive; force a
        # genuinely-dead marker via os.kill probing instead by deleting it so
        # the read-only path treats the file as crashed (no live_pid).
        j1._conn.execute("DELETE FROM session_state WHERE key = 'live_pid'")
        j1._conn.commit()
        j1._conn.close()
        j1._closed = True

        j2 = SqliteJournal("fresh", data_dir=tmp_path)
        try:
            assert (tmp_path / "crash-dumps" / "ghost.sqlite").exists()
            assert not (tmp_path / "journals" / "ghost.sqlite").exists()
        finally:
            j2.close()

    def test_clean_close_clears_live_pid_marker(self, tmp_path):
        j = SqliteJournal("sess", data_dir=tmp_path)
        j.append(kind=JournalRecordKind.EVENT, name="ev", session_id="sess")
        # While open, the liveness marker is present.
        live = j._conn.execute("SELECT value FROM session_state WHERE key = 'live_pid'").fetchone()
        assert live is not None and live[0] not in (None, "")
        j.close()

        # After a clean close the marker is gone so the file never reads live.
        conn = sqlite3.connect(f"file:{tmp_path / 'journals' / 'sess.sqlite'}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM session_state WHERE key = 'live_pid'").fetchone()
        finally:
            conn.close()
        assert row is None

    def test_open_does_not_sweep_a_live_sibling(self, tmp_path):
        # A concurrently-open live journal (its PID is this test process,
        # alive) must NOT be swept when another session opens.
        live = SqliteJournal("alive", data_dir=tmp_path)
        live.append(kind=JournalRecordKind.EVENT, name="ev", session_id="alive")
        other = SqliteJournal("other", data_dir=tmp_path)
        try:
            assert (tmp_path / "journals" / "alive.sqlite").exists()
            assert not (tmp_path / "crash-dumps" / "alive.sqlite").exists()
        finally:
            other.close()
            live.close()

    def test_sqlite_journal_files_are_private_under_permissive_umask(self, tmp_path):
        old_umask = os.umask(0o022)
        j = None
        try:
            j = SqliteJournal("sess", data_dir=tmp_path)
            j.append(kind=JournalRecordKind.EVENT, name="ev", session_id="sess")

            assert _mode(tmp_path / "journals") == 0o700
            assert _mode(tmp_path / "journals" / "sess.sqlite") == 0o600
            for suffix in ("-wal", "-shm"):
                sidecar = tmp_path / "journals" / f"sess.sqlite{suffix}"
                if sidecar.exists():
                    assert _mode(sidecar) == 0o600
        finally:
            os.umask(old_umask)
            if j is not None:
                j.close()

    def test_crash_dump_files_are_private_under_permissive_umask(self, tmp_path):
        old_umask = os.umask(0o022)
        try:
            j1 = SqliteJournal("sess", data_dir=tmp_path)
            j1.append(kind=JournalRecordKind.EVENT, name="ev", session_id="sess")
            j1._conn.close()
            j1._closed = True

            j2 = SqliteJournal("sess", data_dir=tmp_path)
            j2.close()

            assert _mode(tmp_path / "crash-dumps") == 0o700
            assert _mode(tmp_path / "crash-dumps" / "sess.sqlite") == 0o600
            for suffix in ("-wal", "-shm"):
                sidecar = tmp_path / "crash-dumps" / f"sess.sqlite{suffix}"
                if sidecar.exists():
                    assert _mode(sidecar) == 0o600
        finally:
            os.umask(old_umask)

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
            "easycat.runtime.journal_sql.shutil.copy2",
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

    def test_crash_dump_checkpoint_databaseerror_degrades_not_raises(self, tmp_path):
        # A malformed WAL — the very crash being recovered — makes the crash-dump
        # checkpoint raise sqlite3.DatabaseError, a non-OperationalError the copy
        # helper does not swallow. __init__ must degrade gracefully, not let it
        # escape and crash journal startup.
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="ev1", session_id="sess")
        j1.append(kind=JournalRecordKind.EVENT, name="ev2", session_id="sess")
        j1._conn.close()
        j1._closed = True

        with mock.patch(
            "easycat.runtime.journal_sql._copy_journal_to_crash_dump",
            side_effect=sqlite3.DatabaseError("database disk image is malformed"),
        ):
            j2 = SqliteJournal("sess", data_dir=tmp_path)  # must not raise

        assert j2._recovered is False
        seq = j2.append(kind=JournalRecordKind.EVENT, name="fresh", session_id="sess")
        assert seq == 1
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
            import signal, sys
            sys.path.insert(0, "src")
            from easycat.runtime import SqliteJournal
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
            signal.pause()
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
        return tmp_path / "journals" / f"{session_id}.sqlite"

    def _make_session_with_sidecars_and_artifact(self, tmp_path, session_id, *, mtime):
        db_path = self._make_journal(tmp_path, session_id)
        wal_path = db_path.with_suffix(".sqlite-wal")
        shm_path = db_path.with_suffix(".sqlite-shm")
        wal_path.write_bytes(b"wal-sidecar" * 8)
        shm_path.write_bytes(b"shm-sidecar" * 8)

        artifact_path = tmp_path / "artifacts" / session_id / "audio" / "chunk.raw"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(f"artifact for {session_id}".encode())

        for path in (db_path, wal_path, shm_path, artifact_path):
            os.utime(path, (mtime, mtime))

        return {
            "db": db_path,
            "wal": wal_path,
            "shm": shm_path,
            "artifact": artifact_path,
            "artifact_dir": tmp_path / "artifacts" / session_id,
        }

    def _retained_size(self, paths):
        return sum(paths[name].stat().st_size for name in ("db", "wal", "shm", "artifact"))

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

    def test_retention_archive_files_are_private_under_permissive_umask(self, tmp_path):
        old_umask = os.umask(0o022)
        try:
            for i in range(3):
                self._make_journal(tmp_path, f"sess-{i}")

            run_retention(tmp_path, max_sessions=1, mode="archive")

            archive_dir = tmp_path / "archive"
            archives = list(archive_dir.glob("*.tar.gz"))
            assert _mode(archive_dir) == 0o700
            assert archives
            for archive in archives:
                assert _mode(archive) == 0o600
                with tarfile.open(archive, "r:gz") as tar:
                    for member in tar.getmembers():
                        assert (member.mode & 0o777) in {0o600, 0o700}
        finally:
            os.umask(old_umask)

    def test_retention_max_bytes_archives_and_cleans_sidecars_and_artifacts(self, tmp_path):
        # Use recent, ordered mtimes: the close-triggered internal retention
        # now runs an on-by-default 14-day age window, so seeding 1970 epoch
        # mtimes would let one journal prune the other before this call.
        now = time.time()
        old = self._make_session_with_sidecars_and_artifact(tmp_path, "old-sess", mtime=now - 2)
        new = self._make_session_with_sidecars_and_artifact(tmp_path, "new-sess", mtime=now - 1)
        max_bytes = self._retained_size(old) + self._retained_size(new) - 1

        # Disable the age window so this exercises only the byte cap.
        removed = run_retention(
            tmp_path,
            max_sessions=10,
            max_bytes=max_bytes,
            max_age_days=10**9,
            mode="archive",
        )

        assert removed == 1
        for path in (old["db"], old["wal"], old["shm"], old["artifact_dir"]):
            assert not path.exists()
        for path in (new["db"], new["wal"], new["shm"], new["artifact"]):
            assert path.exists()

        archive_path = tmp_path / "archive" / "old-sess.tar.gz"
        assert archive_path.exists()
        with tarfile.open(archive_path, "r:gz") as tar:
            names = set(tar.getnames())
        assert "old-sess.sqlite" in names
        assert "artifacts/old-sess/audio/chunk.raw" in names

    def test_retention_delete_mode(self, tmp_path):
        for i in range(3):
            self._make_journal(tmp_path, f"sess-{i}")

        run_retention(tmp_path, max_sessions=1, mode="delete")
        assert not (tmp_path / "archive").exists()
        remaining = list((tmp_path / "journals").glob("*.sqlite"))
        assert len(remaining) == 1

    def test_retention_delete_mode_cleans_sidecars_and_artifacts(self, tmp_path):
        now = time.time()
        old = self._make_session_with_sidecars_and_artifact(tmp_path, "old-sess", mtime=now - 2)
        new = self._make_session_with_sidecars_and_artifact(tmp_path, "new-sess", mtime=now - 1)

        # Disable the age window so this exercises only the count cap.
        removed = run_retention(tmp_path, max_sessions=1, max_age_days=10**9, mode="delete")

        assert removed == 1
        assert not (tmp_path / "archive").exists()
        for path in (old["db"], old["wal"], old["shm"], old["artifact_dir"]):
            assert not path.exists()
        for path in (new["db"], new["wal"], new["shm"], new["artifact"]):
            assert path.exists()

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
                from easycat.runtime import SqliteJournal
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
        with mock.patch("easycat.runtime.journal_sql.shutil.which", return_value=None):
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
        with mock.patch("easycat.runtime.journal_sql.shutil.which", return_value=None):
            j = create_journal(
                "test-factory-ls",
                debug="full",
                backend="sqlite+litestream",
                data_dir=str(tmp_path),
            )
        assert isinstance(j, LitestreamSqliteJournal)
        j.close()

    @pytest.mark.integration_external
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


class _LockProbeCursor:
    """Minimal libSQL cursor: enough for LibsqlJournal.__init__ bootstrap."""

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []


class _LockProbeConn:
    """Fake libSQL connection whose ``sync()`` verifies the writer lock.

    A correctly-locked caller already holds the non-reentrant
    ``LibsqlJournal._lock``, so ``acquire(blocking=False)`` returns ``False``.
    If it acquires, the caller bypassed single-writer discipline — recorded as
    a violation.
    """

    def __init__(self):
        self.journal = None
        self.violations: list[str] = []
        self.sync_threads: set[str] = set()
        self.rollbacks = 0

    def executescript(self, sql):
        return None

    def execute(self, sql, params=None):
        return _LockProbeCursor()

    def commit(self):
        return None

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        return None

    def sync(self):
        import threading

        journal = self.journal
        if journal is None:
            # __init__/thread-start race before the test wires us up.
            return
        self.sync_threads.add(threading.current_thread().name)
        if journal._lock.acquire(blocking=False):
            self.violations.append(threading.current_thread().name)
            journal._lock.release()


class _FakeLibsqlModule:
    """Stand-in for the ``libsql_experimental`` SDK module."""

    def __init__(self, conn):
        self._conn = conn

    def connect(self, **kwargs):
        return self._conn


class TestLibsqlJournal:
    def test_tag_index_failure_rolls_back_and_restores_sequence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from easycat.runtime import LibsqlJournal
        from easycat.runtime import journal_sql as journal_sql_module

        probe = _LockProbeConn()
        fake_libsql = _FakeLibsqlModule(probe)

        def fail_tag_index(*_args: object) -> None:
            raise RuntimeError("tag index failed")

        with mock.patch.dict("sys.modules", {"libsql_experimental": fake_libsql}):
            journal = LibsqlJournal("atomic-libsql", data_dir=tmp_path)
            probe.journal = journal
            monkeypatch.setattr(
                journal_sql_module,
                "_insert_tag_index_rows",
                fail_tag_index,
            )

            assert (
                journal.append(
                    kind=JournalRecordKind.EVENT,
                    name="partial",
                    session_id="atomic-libsql",
                    tags=frozenset({"tagged"}),
                )
                == -1
            )
            assert journal.latest_sequence == 0
            assert probe.rollbacks == 1
            probe.journal = None
            journal.close()

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

    @pytest.mark.integration_external
    @pytest.mark.skipif(
        not _libsql_available(),
        reason="libsql_experimental SDK not installed",
    )
    def test_libsql_adapter_round_trip(self, tmp_path):
        """Integration: round-trip through LibsqlJournal (local-only, no remote)."""
        from easycat.runtime import LibsqlJournal

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

    @pytest.mark.integration_external
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
        from easycat.runtime import LibsqlJournal, ReadonlySqliteJournal

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

    @pytest.mark.integration_external
    @pytest.mark.skipif(
        not _libsql_available(),
        reason="libsql_experimental SDK not installed",
    )
    def test_libsql_clean_reuse_clears_degraded_marker(self, tmp_path):
        """Clean libSQL reuse truncates the prior journal, so its stale
        ``degraded`` marker must be cleared."""
        from easycat.runtime import LibsqlJournal, ReadonlySqliteJournal

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

    def test_libsql_sync_paths_hold_writer_lock(self, tmp_path):
        """Regression (bug #5): the periodic ``sync()`` daemon, ``flush()``,
        and ``finalize()`` must touch the libSQL connection under the
        single-writer ``threading.Lock``.

        Without the lock the daemon sync races the append thread on the same
        connection; ``append`` treats any error as fatal (``_enter_degraded``
        → returns ``-1`` forever), silently dropping every later record.

        Runs without the real ``libsql_experimental`` SDK by injecting a fake
        connection whose ``sync()`` probes the lock: a correctly-locked caller
        already holds the non-reentrant lock, so ``acquire(blocking=False)``
        returns ``False``.  If it acquires, the caller bypassed the lock.
        """
        from easycat.runtime import LibsqlJournal

        probe = _LockProbeConn()
        fake_libsql = _FakeLibsqlModule(probe)

        with mock.patch.dict("sys.modules", {"libsql_experimental": fake_libsql}):
            j = LibsqlJournal(
                "sess-lock",
                data_dir=tmp_path,
                sync_url="libsql://example.invalid",
                sync_interval_s=0.01,
            )
            probe.journal = j
            try:
                start_seq = j.latest_sequence
                # Same-thread probes: flush()/finalize() must hold the lock.
                for _ in range(3):
                    j.flush()
                    j.finalize()
                # Exercise the append path while the daemon loop ticks.
                for i in range(20):
                    assert (
                        j.append(
                            kind=JournalRecordKind.EVENT,
                            name=f"e{i}",
                            session_id="sess-lock",
                            data={"i": i},
                        )
                        != -1
                    )
                # Wait until the background daemon sync loop has ticked.
                deadline = time.monotonic() + 2.0
                while "libsql-sync" not in probe.sync_threads and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                # ``close()`` is out of the fixed scope (it stops the daemon
                # before its own final sync); disarm the probe so its teardown
                # sync does not register a spurious violation.
                probe.journal = None
                j.close()

        assert "libsql-sync" in probe.sync_threads, "background sync loop never ran"
        assert probe.violations == [], f"sync ran without the writer lock: {probe.violations}"
        assert j.degraded is False
        assert j.latest_sequence == start_seq + 20


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


# ── Indexed stage/turn/tag queries + old-schema migration ────────


# The journal schema exactly as it shipped *before* the indexed ``stage``
# column and the ``journal_tags`` junction were added: 17 columns (through
# ``error_children``), no stage column, no indexes, no junction table.  Files
# written by that version must still open, read, migrate, and — when unclean —
# promote to a crash dump with a recovered-session marker.
_PRE_STAGE_SCHEMA = """
CREATE TABLE journal (
    sequence     INTEGER PRIMARY KEY,
    session_id   TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    name         TEXT    NOT NULL DEFAULT '',
    wall_ns      INTEGER NOT NULL DEFAULT 0,
    mono_ns      INTEGER NOT NULL DEFAULT 0,
    cpu_ns       INTEGER NOT NULL DEFAULT 0,
    turn_id      TEXT,
    data         TEXT    NOT NULL DEFAULT '{}',
    error_type   TEXT,
    error_msg    TEXT,
    error_tb     TEXT,
    error_notes  TEXT,
    input_ref    TEXT,
    output_ref   TEXT,
    tags         TEXT    NOT NULL DEFAULT '',
    error_children TEXT
);
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE session_state (key TEXT PRIMARY KEY, value TEXT);
"""


def _write_pre_stage_journal(
    db_path: Path,
    rows: list[tuple[int, str, str | None, dict[str, object], Iterable[str]]],
    *,
    clean_close: bool = False,
) -> None:
    """Create an old-schema (pre-stage) journal file with *rows*.

    Each row is ``(sequence, name, turn_id, data_dict, tags_iterable)``.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_PRE_STAGE_SCHEMA)
    for sequence, name, turn_id, data, tags in rows:
        conn.execute(
            "INSERT INTO journal "
            "(sequence, session_id, kind, name, turn_id, data, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sequence,
                "old-session",
                JournalRecordKind.EVENT.value,
                name,
                turn_id,
                json.dumps(data),
                ",".join(sorted(tags)),
            ),
        )
    if clean_close:
        conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('clean_close', '1')"
        )
    conn.commit()
    conn.close()


class TestIndexedStageTurnTagQueries:
    def test_filter_by_stage_returns_stage_and_observed_stage_records(
        self,
        journal: SqliteJournal,
    ) -> None:
        # stage records stamp ``data['stage']``; control-signal records stamp
        # ``observed_stage`` (equal to ``stage``).  Both must match.
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="stage_start",
            session_id="test-session",
            data={"stage": "stt"},
        )
        journal.append(
            kind=JournalRecordKind.CONTROL,
            name="control_signal",
            session_id="test-session",
            data={"stage": "stt", "observed_stage": "stt"},
        )
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="other",
            session_id="test-session",
            data={"stage": "tts"},
        )
        journal.append(
            kind=JournalRecordKind.CONTROL,
            name="mixed",
            session_id="test-session",
            data={"stage": "stt", "observed_stage": "agent"},
        )
        view = JournalView(journal)
        names = {r.name for r in view.filter_by_stage("stt")}
        assert names == {"stage_start", "control_signal", "mixed"}
        assert [r.name for r in view.filter_by_stage("tts")] == ["other"]
        assert [r.name for r in view.filter_by_stage("agent")] == ["mixed"]

    def test_filter_by_stage_uses_indexed_column_not_full_scan(
        self,
        journal: SqliteJournal,
    ) -> None:
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="s",
            session_id="test-session",
            data={"stage": "vad"},
        )
        # The dedicated index exists and the query path is an index lookup.
        assert hasattr(journal, "slice_by_stage")
        names = {row[1] for row in journal._conn.execute("PRAGMA index_list(journal)").fetchall()}
        assert {"idx_journal_stage", "idx_journal_observed_stage"} <= names
        plan = journal._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM journal WHERE stage = ? OR observed_stage = ?",
            ["vad", "vad"],
        ).fetchall()
        plan_text = " ".join(str(column) for step in plan for column in step)
        assert "idx_journal_stage" in plan_text
        assert "idx_journal_observed_stage" in plan_text

    def test_filter_by_turn_uses_indexed_turn_column(self, journal: SqliteJournal) -> None:
        journal.append(
            kind=JournalRecordKind.EVENT, name="a", session_id="test-session", turn_id="t1"
        )
        journal.append(
            kind=JournalRecordKind.EVENT, name="b", session_id="test-session", turn_id="t2"
        )
        journal.append(
            kind=JournalRecordKind.EVENT, name="c", session_id="test-session", turn_id="t1"
        )
        view = JournalView(journal)
        assert [r.name for r in view.filter_by_turn("t1")] == ["a", "c"]
        assert [r.name for r in view.filter_by_turn("t2")] == ["b"]

        names = {row[1] for row in journal._conn.execute("PRAGMA index_list(journal)").fetchall()}
        assert "idx_journal_turn_id" in names

    def test_slice_by_tags_uses_junction_table(self, journal: SqliteJournal) -> None:
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="hit",
            session_id="test-session",
            tags=frozenset({"stt"}),
        )
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="multi",
            session_id="test-session",
            tags=frozenset({"stt", "vad"}),
        )
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="miss",
            session_id="test-session",
            tags=frozenset({"not_stt"}),
        )
        # Correct subset semantics, served by the junction.
        assert {r.name for r in journal.slice(tags=frozenset({"stt"}))} == {"hit", "multi"}
        assert [r.name for r in journal.slice(tags=frozenset({"stt", "vad"}))] == ["multi"]

        # The junction is populated and the query references it (not a LIKE scan
        # over the comma string).
        junction = {
            (row[0], row[1])
            for row in journal._conn.execute("SELECT tag, sequence FROM journal_tags").fetchall()
        }
        assert ("stt", 1) in junction and ("vad", 2) in junction and ("not_stt", 3) in junction
        plan_text = " ".join(
            str(c)
            for step in journal._conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM journal WHERE "
                "sequence IN (SELECT sequence FROM journal_tags WHERE tag = ?)",
                ["stt"],
            ).fetchall()
            for c in step
        )
        assert "journal_tags" in plan_text

    def test_append_rolls_back_row_and_sequence_when_tag_index_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from easycat.runtime import journal_sql as journal_sql_module

        journal = SqliteJournal("atomic", data_dir=tmp_path)

        def fail_after_first_tag(
            conn: sqlite3.Connection,
            sequence: int,
            tags: frozenset[str],
        ) -> None:
            conn.execute(
                "INSERT INTO journal_tags (tag, sequence) VALUES (?, ?)",
                (next(iter(tags)), sequence),
            )
            raise RuntimeError("tag index failed")

        monkeypatch.setattr(journal_sql_module, "_insert_tag_index_rows", fail_after_first_tag)

        assert (
            journal.append(
                kind=JournalRecordKind.EVENT,
                name="partial",
                session_id="atomic",
                tags=frozenset({"tagged"}),
            )
            == -1
        )
        assert journal.latest_sequence == 0
        assert (
            journal._conn.execute("SELECT COUNT(*) FROM journal WHERE sequence > 0").fetchone()[0]
            == 0
        )
        assert (
            journal._conn.execute(
                "SELECT COUNT(*) FROM journal_tags WHERE sequence > 0"
            ).fetchone()[0]
            == 0
        )
        journal.close()


class TestOldSchemaJournalMigration:
    def test_index_migration_uses_bounded_keyset_batches(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from easycat.runtime import _journal_codec as journal_codec

        db_path = tmp_path / "batched.sqlite"
        _write_pre_stage_journal(
            db_path,
            rows=[
                (sequence, f"row-{sequence}", None, {"stage": "stt"}, {"tagged"})
                for sequence in range(1, 6)
            ],
        )
        conn = sqlite3.connect(db_path)
        journal_codec._ensure_journal_schema(conn)
        monkeypatch.setattr(journal_codec, "_INDEX_BACKFILL_BATCH_SIZE", 2)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)

        journal_codec._ensure_index_backfill(conn)

        batch_reads = [
            statement
            for statement in statements
            if "SELECT sequence, data, tags FROM journal" in statement
        ]
        assert len(batch_reads) == 4  # three populated batches plus the terminating read
        assert all("LIMIT 2" in statement for statement in batch_reads)
        assert all("WHERE sequence >" in statement for statement in batch_reads[1:])
        assert conn.execute("SELECT COUNT(*) FROM journal WHERE stage = 'stt'").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM journal_tags").fetchone()[0] == 5
        conn.close()

    def test_ensure_schema_migrates_pre_stage_file_additively(self, tmp_path: Path) -> None:
        from easycat.runtime._journal_codec import _ensure_index_backfill, _ensure_journal_schema

        db_path = tmp_path / "old.sqlite"
        _write_pre_stage_journal(
            db_path,
            rows=[
                (1, "stage_start", "t1", {"stage": "stt"}, {"stt", "slow"}),
                (2, "control", "t1", {"stage": "agent", "observed_stage": "agent"}, set()),
                (3, "plain", "t2", {"note": "x"}, {"vad"}),
            ],
        )

        conn = sqlite3.connect(db_path)
        _ensure_journal_schema(conn)
        _ensure_index_backfill(conn)

        # Column, indexes, and junction table were added additively.
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(journal)").fetchall()}
        assert {"stage", "observed_stage"} <= cols
        idx = {r[1] for r in conn.execute("PRAGMA index_list(journal)").fetchall()}
        assert {
            "idx_journal_stage",
            "idx_journal_observed_stage",
            "idx_journal_turn_id",
        } <= idx
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "journal_tags" in tables

        # Stage column was backfilled from data['stage']/observed_stage.
        stages = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT sequence, stage, observed_stage FROM journal"
            ).fetchall()
        }
        assert stages == {1: ("stt", None), 2: ("agent", "agent"), 3: (None, None)}

        # Tags were backfilled into the junction from the comma string.
        junction = {
            (r[0], r[1]) for r in conn.execute("SELECT tag, sequence FROM journal_tags").fetchall()
        }
        assert junction == {("slow", 1), ("stt", 1), ("vad", 3)}
        conn.commit()
        conn.close()

        # Running migration again is a no-op (completion marker already present).
        conn2 = sqlite3.connect(db_path)
        _ensure_journal_schema(conn2)
        _ensure_index_backfill(conn2)
        again = {
            (r[0], r[1])
            for r in conn2.execute("SELECT tag, sequence FROM journal_tags").fetchall()
        }
        assert again == junction
        assert conn2.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
        conn2.close()

    def test_interrupted_index_migration_resumes(self, tmp_path: Path) -> None:
        from easycat.runtime._journal_codec import _ensure_index_backfill, _ensure_journal_schema

        db_path = tmp_path / "interrupted.sqlite"
        _write_pre_stage_journal(
            db_path,
            rows=[
                (1, "first", "t1", {"stage": "stt"}, {"stt", "slow"}),
                (2, "second", "t2", {"observed_stage": "agent"}, {"agent"}),
            ],
        )
        interrupted = sqlite3.connect(db_path)
        interrupted.execute("ALTER TABLE journal ADD COLUMN stage TEXT")
        interrupted.execute("ALTER TABLE journal ADD COLUMN observed_stage TEXT")
        interrupted.execute(
            "CREATE TABLE journal_tags ("
            "tag TEXT NOT NULL, sequence INTEGER NOT NULL, PRIMARY KEY (tag, sequence))"
        )
        interrupted.execute("UPDATE journal SET stage = 'stt' WHERE sequence = 1")
        interrupted.execute("INSERT INTO journal_tags (tag, sequence) VALUES ('stt', 1)")
        interrupted.commit()
        interrupted.close()

        resumed = sqlite3.connect(db_path)
        _ensure_journal_schema(resumed)
        _ensure_index_backfill(resumed)

        assert resumed.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
        assert resumed.execute(
            "SELECT sequence, stage, observed_stage FROM journal ORDER BY sequence"
        ).fetchall() == [(1, "stt", None), (2, None, "agent")]
        assert set(resumed.execute("SELECT tag, sequence FROM journal_tags")) == {
            ("stt", 1),
            ("slow", 1),
            ("agent", 2),
        }
        resumed.close()

    def test_readonly_reads_pre_stage_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "old.sqlite"
        _write_pre_stage_journal(
            db_path,
            rows=[
                (1, "stage_start", "t1", {"stage": "stt"}, {"stt"}),
                (2, "other", "t2", {"observed_stage": "agent"}, set()),
            ],
        )
        ro = ReadonlySqliteJournal(db_path)
        # Reads work despite the missing stage column / junction table.
        assert [r.name for r in ro.read()] == ["stage_start", "other"]
        # filter_by_turn goes through slice(turn_id=) — works on the old column.
        view = JournalView(ro)
        assert [r.name for r in view.filter_by_turn("t1")] == ["stage_start"]
        # filter_by_stage falls back to the scan (read-only view has no indexed
        # helper) and still honors both stage and observed_stage.
        assert [r.name for r in view.filter_by_stage("stt")] == ["stage_start"]
        assert [r.name for r in view.filter_by_stage("agent")] == ["other"]
        # Tag slicing on the read-only path uses the comma-string LIKE, which
        # works on a file that predates the junction table.
        assert [r.name for r in ro.slice(tags=frozenset({"stt"}))] == ["stage_start"]

    def test_unclean_pre_stage_file_promotes_crash_dump_with_recovery_marker(
        self,
        tmp_path: Path,
    ) -> None:
        # An old-schema file left unclean (no clean_close marker) must still be
        # promoted to a crash dump and generate a recovered-session marker when
        # its session id is reopened by the current backend.
        db_path = tmp_path / "journals" / "sess.sqlite"
        _write_pre_stage_journal(
            db_path,
            rows=[
                (1, "ev1", "t1", {"stage": "stt"}, {"stt"}),
                (2, "ev2", "t1", {"stage": "tts"}, set()),
            ],
            clean_close=False,
        )

        j2 = SqliteJournal("sess", data_dir=tmp_path)
        assert j2._recovered is True

        records = j2.read(start=0)
        recovery = [r for r in records if r.kind == JournalRecordKind.RECOVERY]
        assert len(recovery) == 1
        assert isinstance(recovery[0], RecoveredSessionMarker)
        assert recovery[0].recovered_record_count == 2

        # New session starts fresh at sequence=1 and its stage/tag indexes work.
        seq = j2.append(
            kind=JournalRecordKind.EVENT,
            name="fresh",
            session_id="sess",
            turn_id="t9",
            data={"stage": "vad"},
            tags=frozenset({"live"}),
        )
        assert seq == 1
        view = JournalView(j2)
        assert [r.name for r in view.filter_by_stage("vad")] == ["fresh"]
        assert [r.name for r in view.filter_by_turn("t9")] == ["fresh"]
        assert [r.name for r in j2.slice(tags=frozenset({"live"}))] == ["fresh"]
        j2.close()

        # The crash dump preserves the prior (old-schema) records and is
        # readable through the read-only view.
        crash_dump = tmp_path / "crash-dumps" / "sess.sqlite"
        assert crash_dump.exists()
        dumped = {r.name for r in ReadonlySqliteJournal(crash_dump).read()}
        assert {"ev1", "ev2"} <= dumped

    def test_clean_pre_stage_file_reuse_starts_fresh(self, tmp_path: Path) -> None:
        db_path = tmp_path / "journals" / "sess.sqlite"
        _write_pre_stage_journal(
            db_path,
            rows=[(1, "old", "t1", {"stage": "stt"}, {"stt"})],
            clean_close=True,
        )
        j2 = SqliteJournal("sess", data_dir=tmp_path)
        assert j2._recovered is False
        # Prior rows are truncated on clean reuse; junction is cleared too.
        assert [r.name for r in j2.read() if r.kind == JournalRecordKind.EVENT] == []
        assert j2._conn.execute("SELECT COUNT(*) FROM journal_tags").fetchone()[0] == 0
        j2.close()
