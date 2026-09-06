"""Tests for the SqliteJournal backend and adapter backends."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import threading
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
from easycat.runtime import journal_sql as journal_sql_module
from easycat.runtime.artifacts import FilesystemArtifactStore
from easycat.runtime.crash_sweep import crash_dump_artifact_root, is_journal_live
from easycat.runtime.journal import append_journal_record_async
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


def _simulate_crash_after_flush(journal: SqliteJournal) -> None:
    """Leave a committed journal without running the clean-close lifecycle."""
    journal.flush()
    journal._conn.close()
    journal._closed = True


@pytest.fixture
def journal(tmp_path):
    j = SqliteJournal("test-session", data_dir=tmp_path)
    yield j
    j.close()


class TestSqliteJournalBasics:
    @pytest.mark.parametrize(
        "session_id",
        [".", "..", "../escape", r"..\escape", "/absolute", "nested/session"],
    )
    def test_rejects_session_ids_that_can_escape_journal_root(
        self,
        tmp_path,
        session_id: str,
    ) -> None:
        with pytest.raises(ValueError, match="session_id must"):
            SqliteJournal(session_id, data_dir=tmp_path)

        assert not (tmp_path.parent / "escape.sqlite").exists()

    def test_rejects_symlinked_journal_directory_without_touching_target(self, tmp_path):
        target = tmp_path / "outside-journals"
        target.mkdir()
        os.chmod(target, 0o755)
        (tmp_path / "journals").symlink_to(target, target_is_directory=True)

        journal: SqliteJournal | None = None
        try:
            with pytest.raises(OSError, match="symlink"):
                journal = SqliteJournal("linked", data_dir=tmp_path)
        finally:
            if journal is not None:
                journal.close()

        assert _mode(target) == 0o755
        assert not (target / "linked.sqlite").exists()

    def test_rejects_symlinked_journal_file_without_touching_target(self, tmp_path):
        target = tmp_path / "outside.sqlite"
        conn = sqlite3.connect(target)
        try:
            conn.execute("CREATE TABLE preserved (value TEXT)")
            conn.commit()
        finally:
            conn.close()
        os.chmod(target, 0o640)

        journals = tmp_path / "journals"
        journals.mkdir()
        (journals / "linked.sqlite").symlink_to(target)

        journal: SqliteJournal | None = None
        try:
            with pytest.raises(OSError, match="symlink"):
                journal = SqliteJournal("linked", data_dir=tmp_path)
        finally:
            if journal is not None:
                journal.close()

        assert _mode(target) == 0o640
        conn = sqlite3.connect(target)
        try:
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall() == [("preserved",)]
        finally:
            conn.close()

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

    def test_rejects_second_live_writer_for_same_session(self, tmp_path):
        first = SqliteJournal("same-session", data_dir=tmp_path)
        try:
            with pytest.raises(RuntimeError, match="already active"):
                SqliteJournal("same-session", data_dir=tmp_path)
        finally:
            first.close()

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
            "text": "phone +1 415 555 1212",
        }
        assert record.error is not None
        assert record.error.message == f"Authorization: {REDACTED_SECRET}"

    def test_append_can_apply_pii_write_filter(self, tmp_path):
        journal = SqliteJournal("pii-session", data_dir=tmp_path, redaction="pii")
        try:
            journal.append(
                kind=JournalRecordKind.EVENT,
                name="sensitive",
                session_id="pii-session",
                data={"text": "phone +1 415 555 1212"},
            )

            assert journal.read()[0].data["text"] == f"phone {REDACTED_PHONE}"
        finally:
            journal.close()

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
    @pytest.mark.parametrize("directory_name", ["runs?blue", "runs#blue", "runs%3Fblue"])
    def test_readonly_view_handles_reserved_uri_characters(
        self,
        tmp_path,
        directory_name: str,
    ) -> None:
        data_dir = tmp_path / directory_name
        journal = SqliteJournal("reserved-path", data_dir=data_dir)
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="saved",
            session_id="reserved-path",
        )
        db_path = journal.db_path
        journal.close()

        records = ReadonlySqliteJournal(db_path).read()

        assert [record.name for record in records] == ["saved"]

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

    def test_rejects_replacement_writer_until_close_releases_claim(self, tmp_path):
        first = SqliteJournal("closing-writer", data_dir=tmp_path)
        first._lock.acquire()
        close_thread = threading.Thread(target=first.close)
        close_thread.start()
        try:
            deadline = time.monotonic() + 1
            while not first._closed and time.monotonic() < deadline:
                time.sleep(0.001)
            assert first._closed

            with pytest.raises(RuntimeError, match="Journal is already active"):
                SqliteJournal("closing-writer", data_dir=tmp_path)
        finally:
            first._lock.release()
            close_thread.join(timeout=1)

        assert not close_thread.is_alive()
        reopened = SqliteJournal("closing-writer", data_dir=tmp_path)
        try:
            assert (
                reopened.append(
                    kind=JournalRecordKind.EVENT,
                    name="fresh",
                    session_id="closing-writer",
                )
                == 1
            )
        finally:
            reopened.close()

    def test_close_releases_connection_and_claim_when_hardening_fails(self, tmp_path):
        journal = SqliteJournal("close-hardening", data_dir=tmp_path)

        with mock.patch.object(
            journal_sql_module,
            "harden_sqlite_files",
            side_effect=OSError("chmod failed"),
        ):
            journal.close()

        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            journal._conn.execute("SELECT 1")

        reopened = SqliteJournal("close-hardening", data_dir=tmp_path)
        reopened.close()

    def test_append_started_before_close_is_dropped_without_degrading(self, tmp_path):
        """A writer that loses the close race is not a storage failure.

        ``append()`` performs its inexpensive closed-state check before
        entering the SQLite writer lock.  Hold it immediately after that
        check, close the journal, then let it enter the writer implementation
        to exercise the otherwise narrow interleave deterministically.
        """
        journal = SqliteJournal("close-race", data_dir=tmp_path)
        append_entered = threading.Event()
        allow_append = threading.Event()
        results: list[int] = []
        errors: list[BaseException] = []
        original_do_append = journal._do_append

        def _paused_do_append(*args, **kwargs):
            append_entered.set()
            assert allow_append.wait(timeout=1)
            return original_do_append(*args, **kwargs)

        def _append() -> None:
            try:
                results.append(
                    journal.append(
                        kind=JournalRecordKind.EVENT,
                        name="late",
                        session_id="close-race",
                    )
                )
            except BaseException as exc:  # noqa: BLE001  # pragma: no cover - assertion
                errors.append(exc)

        journal._do_append = _paused_do_append
        worker = threading.Thread(target=_append)
        worker.start()
        try:
            assert append_entered.wait(timeout=1)
            journal.close()
            allow_append.set()
            worker.join(timeout=1)

            assert not worker.is_alive()
            assert errors == []
            assert results == [-1]
            assert journal.degraded is False
        finally:
            allow_append.set()
            worker.join(timeout=1)
            journal.close()

    def test_wal_mode_enabled(self, tmp_path):
        j = SqliteJournal("sess", data_dir=tmp_path)
        # Check via a second connection.
        conn = sqlite3.connect(str(tmp_path / "journals" / "sess.sqlite"))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        j.close()
        assert mode == "wal"


class TestSqliteJournalBatching:
    @staticmethod
    def _durable_names(db_path: Path) -> list[str]:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return [
                row[0]
                for row in conn.execute("SELECT name FROM journal ORDER BY sequence").fetchall()
            ]
        finally:
            conn.close()

    def test_record_limit_commits_batch(self, tmp_path):
        journal = SqliteJournal("batch-count", data_dir=tmp_path)
        journal._batch_commit_interval_s = 1.0
        journal._batch_commit_records = 3
        try:
            journal.append(
                kind=JournalRecordKind.EVENT,
                name="one",
                session_id="batch-count",
            )
            journal.append(
                kind=JournalRecordKind.EVENT,
                name="two",
                session_id="batch-count",
            )
            assert self._durable_names(journal.db_path) == []

            journal.append(
                kind=JournalRecordKind.EVENT,
                name="three",
                session_id="batch-count",
            )
            assert self._durable_names(journal.db_path) == ["one", "two", "three"]
        finally:
            journal.close()

    def test_turn_boundary_commits_pending_batch(self, tmp_path):
        journal = SqliteJournal("batch-turn", data_dir=tmp_path)
        journal._batch_commit_interval_s = 1.0
        journal._batch_commit_records = 100
        try:
            journal.append(
                kind=JournalRecordKind.EVENT,
                name="stage_start",
                session_id="batch-turn",
            )
            assert self._durable_names(journal.db_path) == []

            journal.append(
                kind=JournalRecordKind.EVENT,
                name="turn_ended",
                session_id="batch-turn",
            )
            assert self._durable_names(journal.db_path) == ["stage_start", "turn_ended"]
        finally:
            journal.close()

    @pytest.mark.parametrize(
        ("batch_commit_records", "boundary_name"),
        [(100, "turn_ended"), (2, "stage_end")],
    )
    def test_append_boundary_commit_failure_does_not_persist_failed_row(
        self,
        tmp_path,
        batch_commit_records,
        boundary_name,
    ):
        journal = SqliteJournal("batch-failure", data_dir=tmp_path)
        journal._batch_commit_interval_s = 1.0
        journal._batch_commit_records = batch_commit_records
        try:
            assert (
                journal.append(
                    kind=JournalRecordKind.EVENT,
                    name="stage_start",
                    session_id="batch-failure",
                )
                == 1
            )
            with mock.patch.object(
                journal,
                "_execute_commit_locked",
                side_effect=sqlite3.OperationalError("injected COMMIT failure"),
            ):
                assert (
                    journal.append(
                        kind=JournalRecordKind.EVENT,
                        name=boundary_name,
                        session_id="batch-failure",
                    )
                    == -1
                )

            assert journal.degraded is True
            assert self._durable_names(journal.db_path) == ["journal_degraded"]
            assert journal.latest_sequence == 0
        finally:
            journal.close()

    def test_failed_batch_artifact_is_reclaimed_when_journal_reopens(self, tmp_path):
        journal = SqliteJournal("artifact-batch-failure", data_dir=tmp_path)
        journal._batch_commit_interval_s = 60.0
        store = FilesystemArtifactStore(
            "artifact-batch-failure",
            data_dir=tmp_path,
            max_bytes=32,
        )
        payload = b"unreferenced-byte-leak"
        ref = store.put(payload)
        try:
            assert (
                journal.append(
                    kind=JournalRecordKind.EVENT,
                    name="stage_start",
                    session_id="artifact-batch-failure",
                    input_ref=ref,
                )
                == 1
            )
            with mock.patch.object(
                journal,
                "_execute_commit_locked",
                side_effect=sqlite3.OperationalError("injected COMMIT failure"),
            ):
                assert (
                    journal.append(
                        kind=JournalRecordKind.EVENT,
                        name="turn_ended",
                        session_id="artifact-batch-failure",
                    )
                    == -1
                )

            assert journal.degraded is True
            assert store.has(ref)
        finally:
            journal.close()
            store.close()

        reopened_journal = SqliteJournal("artifact-batch-failure", data_dir=tmp_path)
        reopened_store = FilesystemArtifactStore(
            "artifact-batch-failure",
            data_dir=tmp_path,
            max_bytes=32,
        )
        try:
            assert reopened_store.has(ref) is False
            assert reopened_store._current_bytes == 0
            assert reopened_store.put(b"0123456789ABC")
        finally:
            reopened_journal.close()
            reopened_store.close()

    def test_elapsed_time_commits_batch(self, tmp_path):
        journal = SqliteJournal("batch-time", data_dir=tmp_path)
        journal._batch_commit_interval_s = 0.02
        try:
            journal.append(
                kind=JournalRecordKind.EVENT,
                name="timed",
                session_id="batch-time",
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if self._durable_names(journal.db_path) == ["timed"]:
                    break
                time.sleep(0.01)
            assert self._durable_names(journal.db_path) == ["timed"]
        finally:
            journal.close()

    def test_wal_autocheckpoint_is_bounded(self, tmp_path):
        journal = SqliteJournal("checkpoint", data_dir=tmp_path)
        try:
            pages = journal._conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
            assert pages == 1000
        finally:
            journal.close()

    async def test_async_append_runs_disk_write_off_loop(self, tmp_path):
        journal = SqliteJournal("off-loop", data_dir=tmp_path)
        caller_thread = threading.get_ident()
        append_threads: list[int] = []
        original = journal._do_append

        def _recording_append(*args, **kwargs):
            append_threads.append(threading.get_ident())
            return original(*args, **kwargs)

        journal._do_append = _recording_append
        try:
            sequence = await append_journal_record_async(
                journal,
                kind=JournalRecordKind.EVENT,
                name="stage_start",
                session_id="off-loop",
            )
            assert sequence == 1
            assert append_threads and append_threads[0] != caller_thread
        finally:
            journal.close()

    async def test_cancelled_async_append_waits_for_worker_completion(self, tmp_path):
        journal = SqliteJournal("cancelled-off-loop", data_dir=tmp_path)
        loop = asyncio.get_running_loop()
        append_started = asyncio.Event()
        release_append = threading.Event()
        original = journal._do_append

        def _blocked_append(*args, **kwargs):
            loop.call_soon_threadsafe(append_started.set)
            release_append.wait(timeout=1)
            return original(*args, **kwargs)

        journal._do_append = _blocked_append
        task = asyncio.create_task(
            append_journal_record_async(
                journal,
                kind=JournalRecordKind.EVENT,
                name="stage_start",
                session_id="cancelled-off-loop",
            )
        )
        try:
            await asyncio.wait_for(append_started.wait(), timeout=0.2)

            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()

            release_append.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.2)
            assert journal.latest_sequence == 1
        finally:
            release_append.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)
            journal.close()

    def test_stalled_scheduled_commit_does_not_block_other_journals(self, tmp_path):
        first = SqliteJournal("stalled-commit", data_dir=tmp_path)
        second = SqliteJournal("independent-commit", data_dir=tmp_path)
        first_started = threading.Event()
        release_first = threading.Event()
        second_committed = threading.Event()

        def _stalled_commit(_generation: int) -> None:
            first_started.set()
            release_first.wait(timeout=1)

        def _record_second_commit(_generation: int) -> None:
            second_committed.set()

        first._commit_scheduled_batch = _stalled_commit
        second._commit_scheduled_batch = _record_second_commit
        try:
            deadline = time.monotonic()
            journal_sql_module._SqliteBatchCommitCoordinator.schedule(first, deadline, 1)
            journal_sql_module._SqliteBatchCommitCoordinator.schedule(second, deadline, 1)

            assert first_started.wait(timeout=0.2)
            assert second_committed.wait(timeout=0.2)
        finally:
            release_first.set()
            first.close()
            second.close()


class TestCrashRecovery:
    def test_unclean_shutdown_detected(self, tmp_path):
        # First session: write records but do NOT close cleanly.
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="ev1", session_id="sess")
        j1.append(kind=JournalRecordKind.EVENT, name="ev2", session_id="sess")
        # Simulate a crash after an explicit durability boundary, without
        # writing the clean-close marker.
        _simulate_crash_after_flush(j1)

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
        # First session: write two records, then crash after a durability boundary.
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="ev1", session_id="sess")
        j1.append(kind=JournalRecordKind.EVENT, name="ev2", session_id="sess")
        _simulate_crash_after_flush(j1)

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
        # First session: write two records, then crash after a durability boundary.
        j1 = SqliteJournal("sess", data_dir=tmp_path)
        j1.append(kind=JournalRecordKind.EVENT, name="ev1", session_id="sess")
        j1.append(kind=JournalRecordKind.EVENT, name="ev2", session_id="sess")
        _simulate_crash_after_flush(j1)

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
        _simulate_crash_after_flush(j1)

        j2 = SqliteJournal("sess", data_dir=tmp_path)
        j2.close()

        crash_dump = tmp_path / "crash-dumps" / "sess.sqlite"
        assert crash_dump.exists()

    def test_reused_session_crashes_preserve_each_dump_artifact_snapshot(self, tmp_path):
        """Recovery snapshots only the crashed epoch's referenced artifacts."""
        session_id = "sess"
        artifacts = FilesystemArtifactStore(session_id, data_dir=tmp_path)
        first_ref = artifacts.put(b"first crash audio")
        first = SqliteJournal(session_id, data_dir=tmp_path)
        first.append(
            kind=JournalRecordKind.EVENT,
            name="first",
            session_id=session_id,
            input_ref=first_ref,
        )
        _simulate_crash_after_flush(first)

        second = SqliteJournal(session_id, data_dir=tmp_path)
        first_dump = tmp_path / "crash-dumps" / f"{session_id}.sqlite"
        first_snapshot = crash_dump_artifact_root(first_dump)
        first_artifact = first_snapshot / first_ref[:2] / f"{first_ref}.bin"
        assert first_artifact.read_bytes() == b"first crash audio"

        second_ref = artifacts.put(b"second crash audio")
        second.append(
            kind=JournalRecordKind.EVENT,
            name="second",
            session_id=session_id,
            input_ref=second_ref,
        )
        _simulate_crash_after_flush(second)

        third = SqliteJournal(session_id, data_dir=tmp_path)
        try:
            second_dump = tmp_path / "crash-dumps" / f"{session_id}-1.sqlite"
            second_snapshot = crash_dump_artifact_root(second_dump)
            assert first_dump.exists()
            assert second_dump.exists()
            assert (first_snapshot / first_ref[:2] / f"{first_ref}.bin").exists()
            assert not (first_snapshot / second_ref[:2] / f"{second_ref}.bin").exists()
            assert (second_snapshot / second_ref[:2] / f"{second_ref}.bin").exists()
        finally:
            third.close()

    def test_clean_session_reuse_releases_prior_epoch_artifacts(self, tmp_path):
        session_id = "reused-clean-artifacts"
        first = SqliteJournal(session_id, data_dir=tmp_path)
        artifacts = FilesystemArtifactStore(session_id, data_dir=tmp_path, max_bytes=32)
        old_ref = artifacts.put(b"prior-session-artifact")
        try:
            assert (
                first.append(
                    kind=JournalRecordKind.EVENT,
                    name="turn_ended",
                    session_id=session_id,
                    input_ref=old_ref,
                )
                == 1
            )
        finally:
            first.close()
            artifacts.close()

        prior_store = FilesystemArtifactStore(session_id, data_dir=tmp_path)
        try:
            assert prior_store.has(old_ref)
        finally:
            prior_store.close()

        second = SqliteJournal(session_id, data_dir=tmp_path)
        reopened = FilesystemArtifactStore(session_id, data_dir=tmp_path, max_bytes=32)
        try:
            assert second.read() == []
            assert reopened.has(old_ref) is False
            assert reopened._current_bytes == 0
            assert reopened.put(b"new-session-artifact")
        finally:
            second.close()
            reopened.close()

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
        # The fixture creates and "crashes" the owner in this process. Expire
        # the cache entry to model the fresh worker that would discover it.
        journal_sql_module._clear_crash_sweep_states()

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
        live_start = j._conn.execute(
            "SELECT value FROM session_state WHERE key = 'live_pid_start'"
        ).fetchone()
        assert live is not None and live[0] not in (None, "")
        assert live_start is not None and live_start[0] not in (None, "")
        j.close()

        # After a clean close the marker is gone so the file never reads live.
        conn = sqlite3.connect(f"file:{tmp_path / 'journals' / 'sess.sqlite'}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM session_state WHERE key = 'live_pid'").fetchone()
            start_row = conn.execute(
                "SELECT value FROM session_state WHERE key = 'live_pid_start'"
            ).fetchone()
        finally:
            conn.close()
        assert row is None
        assert start_row is None

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

    def test_private_file_helpers_fallback_without_fchmod(self, tmp_path, monkeypatch):
        from easycat.runtime import _private_files as private_files

        monkeypatch.setattr(private_files.os, "fchmod", None)
        monkeypatch.setattr(private_files, "_SUPPORTS_DIRECTORY_HANDLES", False)
        directory = tmp_path / "private"
        path = directory / "secret"

        private_files.mkdir_private(directory)
        private_files.touch_private_file(path)

        assert _mode(directory) == 0o700
        assert _mode(path) == 0o600

    def test_private_path_checks_fail_closed_on_metadata_error(self, tmp_path, monkeypatch):
        from easycat.runtime import _private_files as private_files

        guarded = tmp_path / "guarded"
        guarded.mkdir()
        real_lstat = type(guarded).lstat

        def denied_lstat(path):
            if path == guarded:
                raise PermissionError(str(path))
            return real_lstat(path)

        monkeypatch.setattr(type(guarded), "lstat", denied_lstat)

        assert private_files._path_is_link_or_reparse(guarded)

    def test_private_copy_falls_back_without_descriptor_relative_io(
        self,
        tmp_path,
        monkeypatch,
    ):
        from easycat.runtime import _private_files as private_files

        monkeypatch.setattr(private_files, "_SUPPORTS_DESCRIPTOR_PRIVATE_COPY", False)
        source_dir = tmp_path / "source"
        target_dir = tmp_path / "target"
        source_dir.mkdir()
        target_dir.mkdir()
        source = source_dir / "journal.sqlite"
        target = target_dir / "journal.sqlite"
        source.write_bytes(b"durable journal")

        private_files.copy_private_file(source, target)

        assert target.read_bytes() == b"durable journal"
        assert _mode(target) == 0o600

    def test_crash_dump_files_are_private_under_permissive_umask(self, tmp_path):
        old_umask = os.umask(0o022)
        try:
            j1 = SqliteJournal("sess", data_dir=tmp_path)
            j1.append(kind=JournalRecordKind.EVENT, name="ev", session_id="sess")
            _simulate_crash_after_flush(j1)

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
        _simulate_crash_after_flush(j1)

        with mock.patch(
            "easycat.runtime.crash_sweep.copy_private_file",
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
        _simulate_crash_after_flush(j1)

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

    def test_committed_append_after_finalize_restores_live_owner_marker(self, tmp_path):
        journal = SqliteJournal("sess", data_dir=tmp_path)
        try:
            journal.append(kind=JournalRecordKind.EVENT, name="before", session_id="sess")
            journal.finalize()

            journal.append(kind=JournalRecordKind.EVENT, name="after", session_id="sess")
            journal.flush()

            db_path = tmp_path / "journals" / "sess.sqlite"
            marker = journal._conn.execute(
                "SELECT value FROM session_state WHERE key = 'live_pid'"
            ).fetchone()
            assert marker == (str(os.getpid()),)
            assert is_journal_live(db_path) is True
        finally:
            journal.close()

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

    def test_elapsed_batch_commits_without_manual_flush(self, tmp_path):
        """The coordinator commits an open batch without another append."""
        j = SqliteJournal("sess", data_dir=tmp_path)
        j.append(kind=JournalRecordKind.EVENT, name="ev1", session_id="sess")
        j.append(kind=JournalRecordKind.EVENT, name="ev2", session_id="sess")

        deadline = time.monotonic() + 1.0
        try:
            while time.monotonic() < deadline:
                ro = sqlite3.connect(
                    f"file:{tmp_path / 'journals' / 'sess.sqlite'}?mode=ro",
                    uri=True,
                )
                try:
                    names = [
                        row[0]
                        for row in ro.execute(
                            "SELECT name FROM journal WHERE kind = ? ORDER BY sequence",
                            (JournalRecordKind.EVENT.value,),
                        ).fetchall()
                    ]
                finally:
                    ro.close()
                if names == ["ev1", "ev2"]:
                    break
                time.sleep(0.01)
            assert names == ["ev1", "ev2"]
        finally:
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
            import signal, sys, time
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
            # No manual flush(): wait beyond the documented 100 ms batch
            # window, then signal that the parent may test SIGKILL recovery.
            time.sleep(0.2)
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

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="SIGKILL not available on Windows",
    )
    def test_sigkill_before_batch_commit_reclaims_unreferenced_artifact(self, tmp_path):
        script = textwrap.dedent(f"""\
            import signal, sys
            sys.path.insert(0, "src")
            from easycat.runtime import SqliteJournal
            from easycat.runtime.artifacts import FilesystemArtifactStore
            from easycat.runtime.records import JournalRecordKind

            journal = SqliteJournal("artifact-crash", data_dir={str(tmp_path)!r})
            journal._batch_commit_interval_s = 60.0
            store = FilesystemArtifactStore("artifact-crash", data_dir={str(tmp_path)!r})
            ref = store.put(b"unreferenced-byte-leak")
            sequence = journal.append(
                kind=JournalRecordKind.EVENT,
                name="stage_start",
                session_id="artifact-crash",
                input_ref=ref,
            )
            print(ref, sequence, flush=True)
            signal.pause()
        """)
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        try:
            ready = proc.stdout.readline().strip().split()
            assert len(ready) == 2, ready
            ref, sequence = ready
            assert sequence == "1"

            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5)

            reopened_journal = SqliteJournal("artifact-crash", data_dir=tmp_path)
            reopened_store = FilesystemArtifactStore(
                "artifact-crash",
                data_dir=tmp_path,
                max_bytes=32,
            )
            try:
                assert reopened_journal.read(start=-1) == []
                assert reopened_store.has(ref) is False
                assert reopened_store._current_bytes == 0
                assert reopened_store.put(b"0123456789ABC")
            finally:
                reopened_journal.close()
                reopened_store.close()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)


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
    """Verify bounded checkpoints, clean-close truncation, and no hot-path fsync."""

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
                check=False,
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
    def test_replica_url_is_namespaced_per_session(self, tmp_path):
        sidecars: list[mock.Mock] = []

        def start_sidecar(*args, **kwargs):
            sidecar = mock.Mock(pid=100 + len(sidecars), stderr=None)
            sidecars.append(sidecar)
            return sidecar

        with (
            mock.patch(
                "easycat.runtime.journal_sql.shutil.which",
                return_value="/usr/bin/litestream",
            ),
            mock.patch(
                "easycat.runtime.journal_sql.subprocess.Popen",
                side_effect=start_sidecar,
            ) as popen,
        ):
            first = LitestreamSqliteJournal(
                "call-one",
                data_dir=tmp_path,
                replica_url="s3://bucket/journals?region=us-west-2",
            )
            second = LitestreamSqliteJournal(
                "call two",
                data_dir=tmp_path,
                replica_url="s3://bucket/journals?region=us-west-2",
            )

        assert popen.call_args_list[0].args[0][-1] == (
            "s3://bucket/journals/call-one.sqlite?region=us-west-2"
        )
        assert popen.call_args_list[1].args[0][-1] == (
            "s3://bucket/journals/call%20two.sqlite?region=us-west-2"
        )
        first.close()
        second.close()

    def test_s3_credentials_move_from_argv_to_the_sidecar_environment(self, tmp_path):
        """A replica URL's credentials must not reach the command line (gh 1068).

        ``ps`` / ``/proc/<pid>/cmdline`` are world-readable for the sidecar's
        whole lifetime, and the sidecar runs for the whole session.
        ``/proc/<pid>/environ`` is owner-only, and the environment is the
        hand-off litestream documents.
        """
        with (
            mock.patch(
                "easycat.runtime.journal_sql.shutil.which",
                return_value="/usr/bin/litestream",
            ),
            mock.patch(
                "easycat.runtime.journal_sql.subprocess.Popen",
                return_value=mock.Mock(pid=101, stderr=None),
            ) as popen,
            mock.patch.dict(
                os.environ,
                {"AWS_ACCESS_KEY_ID": "ambient", "AWS_SECRET_ACCESS_KEY": "ambient"},
            ),
        ):
            j = LitestreamSqliteJournal(
                "creds",
                data_dir=tmp_path,
                replica_url="s3://AKIAEXAMPLE:s3cr3t%2Fkey@bucket.example.com:9000/j",
            )
        try:
            argv = popen.call_args.args[0]
            assert argv[-1] == "s3://bucket.example.com:9000/j/creds.sqlite"
            assert not any("s3cr3t" in str(arg) for arg in argv)
            assert not any("AKIAEXAMPLE" in str(arg) for arg in argv)

            env = popen.call_args.kwargs["env"]
            # The URL's credentials outrank whatever the parent inherited:
            # litestream ranks ``AWS_*`` above ``LITESTREAM_*``.
            assert env["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE"
            assert env["AWS_SECRET_ACCESS_KEY"] == "s3cr3t/key"
            assert env["LITESTREAM_ACCESS_KEY_ID"] == "AKIAEXAMPLE"
            assert env["LITESTREAM_SECRET_ACCESS_KEY"] == "s3cr3t/key"
            # The rest of the parent environment still reaches the sidecar.
            assert env.get("PATH") == os.environ.get("PATH")
        finally:
            j.close()

    def test_azure_account_key_moves_off_argv_but_the_account_name_stays(self, tmp_path):
        """Only the secret half of ``abs://account:key@host`` is a credential."""
        with (
            mock.patch(
                "easycat.runtime.journal_sql.shutil.which",
                return_value="/usr/bin/litestream",
            ),
            mock.patch(
                "easycat.runtime.journal_sql.subprocess.Popen",
                return_value=mock.Mock(pid=102, stderr=None),
            ) as popen,
        ):
            j = LitestreamSqliteJournal(
                "abs-creds",
                data_dir=tmp_path,
                replica_url="abs://acct:AzKey%2B123@acct.blob.core.windows.net/container",
            )
        try:
            argv = popen.call_args.args[0]
            assert argv[-1] == ("abs://acct@acct.blob.core.windows.net/container/abs-creds.sqlite")
            assert not any("AzKey" in str(arg) for arg in argv)
            assert popen.call_args.kwargs["env"]["LITESTREAM_AZURE_ACCOUNT_KEY"] == "AzKey+123"
        finally:
            j.close()

    def test_credential_free_url_inherits_the_environment_unchanged(self, tmp_path):
        """Nothing to move → no synthesized environment, just inheritance."""
        with (
            mock.patch(
                "easycat.runtime.journal_sql.shutil.which",
                return_value="/usr/bin/litestream",
            ),
            mock.patch(
                "easycat.runtime.journal_sql.subprocess.Popen",
                return_value=mock.Mock(pid=103, stderr=None),
            ) as popen,
        ):
            j = LitestreamSqliteJournal(
                "plain",
                data_dir=tmp_path,
                replica_url="s3://bucket/journals",
            )
        try:
            assert popen.call_args.args[0][-1] == "s3://bucket/journals/plain.sqlite"
            assert popen.call_args.kwargs["env"] is None
        finally:
            j.close()

    def test_scheme_without_a_credential_env_contract_is_reported(self, tmp_path, caplog):
        """``sftp`` has no documented env var, so warn instead of breaking it."""
        with (
            mock.patch(
                "easycat.runtime.journal_sql.shutil.which",
                return_value="/usr/bin/litestream",
            ),
            mock.patch(
                "easycat.runtime.journal_sql.subprocess.Popen",
                return_value=mock.Mock(pid=104, stderr=None),
            ) as popen,
            caplog.at_level(logging.WARNING, logger="easycat.runtime.journal_sql"),
        ):
            j = LitestreamSqliteJournal(
                "sftp-creds",
                data_dir=tmp_path,
                replica_url="sftp://user:hunter2@example.com:22/backup",
            )
        try:
            # Replication keeps working; the exposure is surfaced, not hidden.
            assert "hunter2" in popen.call_args.args[0][-1]
            assert popen.call_args.kwargs["env"] is None
            assert any("`ps`" in record.message for record in caplog.records)
        finally:
            j.close()

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
            ["litestream", "restore", "-o", str(restore_path), j._replica_url],
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
        self.live_owner_present = False
        self.synced_live_owner_states: list[bool] = []
        self.rollbacks = 0

    def executescript(self, sql):
        return None

    def execute(self, sql, params=None):
        if "VALUES ('live_pid', ?)" in sql:
            self.live_owner_present = True
        elif "DELETE FROM session_state WHERE key IN ('live_pid', 'live_pid_start')" in sql:
            self.live_owner_present = False
        return _LockProbeCursor()

    def commit(self):
        return None

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        return None

    def sync(self):
        import threading

        self.synced_live_owner_states.append(self.live_owner_present)
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


class _BlockingOwnerDeleteConn(_LockProbeConn):
    def __init__(self) -> None:
        super().__init__()
        self.delete_started = threading.Event()
        self.release_delete = threading.Event()

    def execute(self, sql, params=None):
        if "DELETE FROM session_state WHERE key IN ('live_pid', 'live_pid_start')" in sql:
            self.delete_started.set()
            assert self.release_delete.wait(timeout=1)
        return super().execute(sql, params)


class _BlockingSyncConn(_LockProbeConn):
    def __init__(self) -> None:
        super().__init__()
        self.sync_started = threading.Event()
        self.release_sync = threading.Event()

    def sync(self):
        self.sync_started.set()
        assert self.release_sync.wait(timeout=2)
        return super().sync()


class _FailOnceCloseConn(_LockProbeConn):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("close failed before connection release")
        super().close()


class TestLibsqlJournal:
    def test_invalid_session_id_is_rejected_before_optional_sdk_import(self, tmp_path) -> None:
        from easycat.runtime import LibsqlJournal

        with mock.patch.dict("sys.modules", {"libsql_experimental": None}):  # noqa: SIM117 nested scopes clarify setup and cleanup
            with pytest.raises(ValueError, match="session_id must"):
                LibsqlJournal("../escape", data_dir=tmp_path)

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

    def test_close_serializes_final_sync_with_writer_lock(self, tmp_path: Path) -> None:
        """Close must not sync or close a libSQL connection beside a writer."""
        from easycat.runtime import LibsqlJournal

        probe = _LockProbeConn()
        fake_libsql = _FakeLibsqlModule(probe)

        with mock.patch.dict("sys.modules", {"libsql_experimental": fake_libsql}):
            journal = LibsqlJournal("close-lock", data_dir=tmp_path)
            probe.journal = journal
            journal.close()

        assert probe.violations == []

    def test_close_syncs_live_owner_marker_removal(self, tmp_path: Path) -> None:
        """The remote replica must not retain an owner after local close."""
        from easycat.runtime import LibsqlJournal

        probe = _LockProbeConn()
        fake_libsql = _FakeLibsqlModule(probe)

        with mock.patch.dict("sys.modules", {"libsql_experimental": fake_libsql}):
            journal = LibsqlJournal("close-owner", data_dir=tmp_path)
            probe.journal = journal
            assert probe.live_owner_present is True

            journal.close()

        assert probe.synced_live_owner_states[-1] is False

    def test_hung_periodic_sync_does_not_block_finalize_or_close(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stuck SDK sync stays owned without trapping teardown callers."""
        from easycat.runtime import LibsqlJournal

        probe = _BlockingSyncConn()
        fake_libsql = _FakeLibsqlModule(probe)
        monkeypatch.setattr(
            journal_sql_module,
            "JOURNAL_LIBSQL_SYNC_THREAD_JOIN_TIMEOUT_S",
            0.05,
        )

        with mock.patch.dict("sys.modules", {"libsql_experimental": fake_libsql}):
            journal = LibsqlJournal(
                "hung-sync",
                data_dir=tmp_path,
                sync_url="libsql://example.invalid",
                sync_interval_s=0.01,
            )
            probe.journal = journal
            assert probe.sync_started.wait(timeout=1)

            started = time.monotonic()
            journal.finalize()
            finalize_elapsed = time.monotonic() - started

            started = time.monotonic()
            journal.close()
            close_elapsed = time.monotonic() - started

            assert finalize_elapsed < 0.5
            assert close_elapsed < 0.5
            assert journal._close_thread is not None
            assert journal._close_thread.is_alive()

            probe.release_sync.set()
            journal._close_thread.join(timeout=1)

        assert not journal._close_thread.is_alive()
        assert probe.synced_live_owner_states[-1] is False

    def test_failed_connection_close_retains_owner_and_retries(
        self,
        tmp_path: Path,
    ) -> None:
        """A failed SDK close must not publish or cache a false clean release."""
        from easycat.runtime import LibsqlJournal

        first_connection = _FailOnceCloseConn()
        rejected_connection = _LockProbeConn()
        replacement_connection = _LockProbeConn()
        connections = iter((first_connection, rejected_connection, replacement_connection))

        class _SequencedLibsqlModule:
            @staticmethod
            def connect(**_kwargs: object) -> _LockProbeConn:
                return next(connections)

        with mock.patch.dict(
            "sys.modules",
            {"libsql_experimental": _SequencedLibsqlModule()},
        ):
            journal = LibsqlJournal("retry-close", data_dir=tmp_path)
            first_connection.journal = journal

            journal.close()

            assert first_connection.close_calls == 1
            assert journal._connection_closed is False
            assert first_connection.live_owner_present is True
            assert first_connection.synced_live_owner_states[-1] is True
            with pytest.raises(RuntimeError, match="Journal is already active"):
                LibsqlJournal("retry-close", data_dir=tmp_path)

            # Admission services the retained predecessor before rejecting the
            # attempt that began while the path was still owned.
            assert first_connection.close_calls == 2
            journal.close()

            assert first_connection.close_calls == 2
            assert journal._connection_closed is True
            assert first_connection.live_owner_present is False

            reopened = LibsqlJournal("retry-close", data_dir=tmp_path)
            reopened.close()

    def test_process_retains_failed_libsql_close_after_debug_backends_are_collected(
        self,
        tmp_path: Path,
    ) -> None:
        """A collected Session owner must not discard an unclosed SDK writer."""
        import gc
        import weakref

        from easycat.events import EventBus
        from easycat.runtime import LibsqlJournal
        from easycat.session._debug_backends import SessionDebugBackends
        from easycat.session._journal_sink import SessionJournalSink

        first_connection = _FailOnceCloseConn()
        replacement_connection = _LockProbeConn()
        connections = iter((first_connection, replacement_connection))

        class _SequencedLibsqlModule:
            @staticmethod
            def connect(**_kwargs: object) -> _LockProbeConn:
                return next(connections)

        with mock.patch.dict(
            "sys.modules",
            {"libsql_experimental": _SequencedLibsqlModule()},
        ):
            journal = LibsqlJournal("session-retry-close", data_dir=tmp_path)
            sink = SessionJournalSink(
                event_bus=EventBus(),
                journal=journal,
                artifact_store=None,
                session_id="session-retry-close",
                current_turn_id=lambda turn_id=None: turn_id,
            )
            backends = SessionDebugBackends(
                journal=journal,
                journal_view=JournalView(journal),
                artifact_store=None,
                journal_sink=sink,
            )
            journal_ref = weakref.ref(journal)
            backends_ref = weakref.ref(backends)

            backends.destroy()

            assert first_connection.close_calls == 1
            assert journal.close_complete is False
            del journal, sink, backends
            gc.collect()

            assert backends_ref() is None
            assert journal_ref() is not None
            with pytest.raises(RuntimeError, match="Journal is already active"):
                SqliteJournal("session-retry-close", data_dir=tmp_path)
            assert first_connection.close_calls == 1
            with pytest.raises(RuntimeError, match="Journal is already active"):
                LibsqlJournal("session-retry-close", data_dir=tmp_path)

            # The rejected admission retried the still-owned connection without
            # opening its replacement beside it.
            assert first_connection.close_calls == 2
            assert first_connection.live_owner_present is False

            reopened = LibsqlJournal("session-retry-close", data_dir=tmp_path)
            reopened.close()

    def test_rejects_second_live_writer_for_same_local_replica(self, tmp_path: Path) -> None:
        """A second local libSQL writer would reuse the same sequence counter."""
        from easycat.runtime import LibsqlJournal

        probe = _LockProbeConn()
        fake_libsql = _FakeLibsqlModule(probe)

        with mock.patch.dict("sys.modules", {"libsql_experimental": fake_libsql}):
            first = LibsqlJournal("single-writer", data_dir=tmp_path)
            try:
                with pytest.raises(RuntimeError, match="Journal is already active"):
                    LibsqlJournal("single-writer", data_dir=tmp_path)
            finally:
                first.close()

            # Releasing the first owner permits a later replica instance.
            reopened = LibsqlJournal("single-writer", data_dir=tmp_path)
            reopened.close()

    @pytest.mark.parametrize(
        "sync_interval_s",
        [0, -0.01, True, "0.1", float("nan"), float("inf"), float("-inf")],
    )
    def test_rejects_invalid_sync_interval(
        self,
        tmp_path: Path,
        sync_interval_s: object,
    ) -> None:
        from easycat.runtime import LibsqlJournal

        fake_libsql = _FakeLibsqlModule(_LockProbeConn())
        with mock.patch.dict("sys.modules", {"libsql_experimental": fake_libsql}):  # noqa: SIM117 nested scopes clarify setup and cleanup
            with pytest.raises(
                ValueError, match="sync_interval_s must be a finite positive number"
            ):
                LibsqlJournal(
                    "invalid-sync-interval",
                    data_dir=tmp_path,
                    sync_interval_s=sync_interval_s,  # type: ignore[arg-type]
                )

        assert not (tmp_path / "journals" / "invalid-sync-interval.sqlite").exists()

    @pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-a-number"])
    def test_rejects_invalid_sync_interval_environment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        from easycat.runtime import LibsqlJournal

        monkeypatch.setenv("EASYCAT_JOURNAL_LIBSQL_SYNC_INTERVAL_S", value)
        fake_libsql = _FakeLibsqlModule(_LockProbeConn())
        with mock.patch.dict("sys.modules", {"libsql_experimental": fake_libsql}):  # noqa: SIM117 nested scopes clarify setup and cleanup
            with pytest.raises(
                ValueError, match="sync_interval_s must be a finite positive number"
            ):
                LibsqlJournal("invalid-sync-environment", data_dir=tmp_path)

        assert not (tmp_path / "journals" / "invalid-sync-environment.sqlite").exists()

    def test_rejects_replacement_writer_until_close_releases_claim(self, tmp_path: Path) -> None:
        """A closing writer owns the path until marker cleanup completes."""
        from easycat.runtime import LibsqlJournal

        probe = _BlockingOwnerDeleteConn()
        fake_libsql = _FakeLibsqlModule(probe)

        with mock.patch.dict("sys.modules", {"libsql_experimental": fake_libsql}):
            first = LibsqlJournal("closing-writer", data_dir=tmp_path)
            close_thread = threading.Thread(target=first.close)
            close_thread.start()
            try:
                assert probe.delete_started.wait(timeout=1)
                with pytest.raises(RuntimeError, match="Journal is already active"):
                    LibsqlJournal("closing-writer", data_dir=tmp_path)
            finally:
                probe.release_delete.set()
                close_thread.join(timeout=1)

        assert not close_thread.is_alive()

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
