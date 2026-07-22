"""SQL-backed journal family: SQLite WAL, Litestream sidecar, and libSQL replica."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from easycat._observability import observe_gauge, record_histogram
from easycat.runtime._journal_codec import (
    _JOURNAL_INSERT_SQL,
    _SQLITE_SCHEMA,
    _build_slice_where,
    _encode_journal_row,
    _ensure_index_backfill,
    _ensure_journal_schema,
    _insert_tag_index_rows,
    _journal_record_for_append,
    _persist_degraded_marker,
    _row_to_record,
)
from easycat.runtime._private_files import (
    harden_sqlite_files,
    mkdir_private,
    touch_private_file,
)
from easycat.runtime.crash_sweep import _copy_journal_to_crash_dump, sweep_crashed_journals
from easycat.runtime.journal import _validate_read_limit
from easycat.runtime.journal_retention import run_retention
from easycat.runtime.records import (
    ErrorInfo,
    JournalRecord,
    JournalRecordKind,
    TimingInfo,
)

logger = logging.getLogger(__name__)


class _SqlJournalBase:
    """Shared implementation for the SQL-backed journals.

    ``SqliteJournal`` (local WAL) and ``LibsqlJournal`` (embedded replica)
    persist to identically-shaped ``journal`` tables and only differ in how
    they open the connection and commit/sync a write.  Everything that reads
    the table or wraps a write — the ``append()`` guard+timing wrapper,
    ``read()``/``slice()``, the ``latest_sequence``/``degraded``/``db_path``
    properties, and the ``_row_to_record`` decoder — is identical and lives
    here so the two backends cannot silently diverge.

    Subclasses set ``_conn``, ``_lock``, ``_seq``, ``_degraded``, ``_closed``,
    and ``_db_path`` in their own ``__init__`` and override ``_do_append``,
    ``_enter_degraded``, ``flush``, ``finalize``, and ``close`` with the
    connection-specific commit/sync semantics.  The in-memory ring buffer is
    deliberately NOT a subclass: its ``append`` guard omits ``_closed`` and its
    ``_do_append``/``_enter_degraded`` operate on a deque, not a connection.
    """

    _conn: Any
    _lock: threading.Lock
    _seq: int
    _degraded: bool
    _closed: bool
    _db_path: Path

    # ── ExecutionJournal interface (shared) ───────────────────────

    def append(
        self,
        kind: JournalRecordKind,
        name: str,
        session_id: str,
        turn_id: str | None = None,
        data: dict[str, Any] | None = None,
        error: ErrorInfo | None = None,
        tags: frozenset[str] = frozenset(),
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> int:
        started = time.perf_counter()
        result = "fail"
        if self._degraded or self._closed:
            record_histogram(
                "easycat.journal.append.latency",
                time.perf_counter() - started,
                {"easycat.result": result},
            )
            return -1
        try:
            sequence = self._do_append(
                kind,
                name,
                session_id,
                turn_id,
                data,
                error,
                tags,
                input_ref,
                output_ref,
            )
            result = "pass"
            return sequence
        except Exception as exc:
            self._enter_degraded(session_id, exc)
            return -1
        finally:
            record_histogram(
                "easycat.journal.append.latency",
                time.perf_counter() - started,
                {"easycat.result": result},
            )

    def read(self, start: int = 0, limit: int | None = None) -> list[JournalRecord]:
        _validate_read_limit(limit)
        with self._lock:
            sql = "SELECT * FROM journal WHERE sequence >= ? ORDER BY sequence"
            params: list[Any] = [start]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        name: str | None = None,
        tags: frozenset[str] | None = None,
    ) -> list[JournalRecord]:
        where, params = _build_slice_where(
            kind=kind,
            session_id=session_id,
            turn_id=turn_id,
            name=name,
            tags=tags,
            # A live backend always has the ``journal_tags`` junction (created +
            # backfilled on open), so tag filters use the index rather than a
            # comma-string LIKE scan.
            use_tag_index=True,
        )
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM journal{where} ORDER BY sequence", params
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def slice_by_stage(self, stage_name: str) -> list[JournalRecord]:
        """Return records whose indexed stage token equals *stage_name*.

        Backs :meth:`JournalView.filter_by_stage`. The two derived columns
        preserve the public ``stage OR observed_stage`` contract even when a
        producer supplies different values; both predicates are indexed.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM journal WHERE stage = ? OR observed_stage = ? ORDER BY sequence",
                [stage_name, stage_name],
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            return self._seq

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ── Connection-specific hooks (overridden by subclasses) ───────

    def _do_append(
        self,
        kind: JournalRecordKind,
        name: str,
        session_id: str,
        turn_id: str | None,
        data: dict[str, Any] | None,
        error: ErrorInfo | None,
        tags: frozenset[str],
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> int:
        raise NotImplementedError

    def _enter_degraded(self, session_id: str, exc: Exception) -> None:
        raise NotImplementedError

    # ── Shared row decoder ─────────────────────────────────────────

    _row_to_record = staticmethod(_row_to_record)


class SqliteJournal(_SqlJournalBase):
    """WAL-mode SQLite journal backend.

    - ``PRAGMA synchronous=NORMAL`` — writes go to the kernel page cache,
      application-crash durable without fsync on the hot path.
    - ``PRAGMA wal_autocheckpoint=0`` — no inline checkpoints; checkpoint
      happens once at clean close via ``PRAGMA wal_checkpoint(TRUNCATE)``.
    - Single-writer discipline via ``threading.Lock``.
    - Eager file-open warmup so the first turn doesn't pay cold-PRAGMA cost.
    """

    def __init__(
        self,
        session_id: str,
        *,
        data_dir: str | Path | None = None,
        retention_mode: Literal["archive", "delete"] = "archive",
    ) -> None:
        root = Path(data_dir) if data_dir else Path(os.environ.get("EASYCAT_DATA_DIR", ".easycat"))
        self._root = root
        self._retention_mode = retention_mode
        journals_dir = root / "journals"
        mkdir_private(journals_dir)
        self._db_path = journals_dir / f"{session_id}.sqlite"

        # Sweep crashed-but-unswept prior journals (different session ids whose
        # process died without a clean close) before we open our own file.  The
        # same-id recovery path below only fires when *this* session's id is
        # reused; orphaned ids never reopen, so the sweep is what promotes them
        # to crash-dumps/.  Best-effort: never block or fail journal startup.
        try:
            sweep_crashed_journals(root, skip=self._db_path)
        except (OSError, sqlite3.DatabaseError):
            logger.debug("Crash-journal sweep failed", exc_info=True)

        touch_private_file(self._db_path)
        self._session_id = session_id
        self._lock = threading.Lock()
        self._seq = 0
        self._degraded = False
        self._closed = False
        self._recovered = False
        self._original_session_id = session_id
        self._clean_close_marked = False

        # ── Check for prior unclean shutdown ─────────────────────
        existed = self._db_path.exists()

        # Eager warmup — open DB and apply PRAGMAs now.
        self._conn = self._open_connection()
        self._conn.executescript(_SQLITE_SCHEMA)
        _ensure_journal_schema(self._conn)

        prior_count = self._reconcile_prior_session(session_id) if existed else 0
        # After reconcile the live table is empty (prior rows were promoted
        # or truncated), so the pre-v2 backfill only stamps the version.
        _ensure_index_backfill(self._conn)

        # Clear prior-session state markers (we're starting a new session).
        self._conn.execute("DELETE FROM session_state WHERE key IN ('clean_close', 'degraded')")

        # Stamp our PID as a liveness marker (committed so a separate
        # crash-sweep connection can read it).  An idle WAL journal between
        # turns holds no write lock, so the orphan sweep cannot tell "live
        # but idle" from "crashed" by lock alone; the PID lets it skip a
        # journal whose owning process is still running.  Cleared on clean
        # close so a cleanly-closed (or crashed-then-PID-reused) file never
        # masquerades as live.
        self._conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
            (str(os.getpid()),),
        )
        self._conn.commit()

        # Recover sequence counter from any existing records.  Both the
        # crash-recovery and clean-reuse paths truncate the journal table
        # above, so for a reused session_id this leaves ``_seq`` at 0 and the
        # first real append starts at sequence=1.
        row = self._conn.execute("SELECT MAX(sequence) FROM journal").fetchone()
        if row and row[0] is not None:
            self._seq = row[0]

        # Start a transaction for batched writes.
        self._conn.execute("BEGIN")

        # Emit recovery marker at sequence=0 if we detected unclean shutdown.
        if self._recovered:
            self._insert_recovery_marker(session_id, prior_count)

    # ── Startup phases ────────────────────────────────────────────

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit for PRAGMAs
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        harden_sqlite_files(self._db_path)
        return conn

    def _reconcile_prior_session(self, session_id: str) -> int:
        """Handle a pre-existing DB file: crash recovery or clean reuse.

        Detects unclean shutdown (file existed but ``clean_close`` marker
        absent) and promotes the prior records to a crash dump; a cleanly
        closed prior session is simply truncated.  Returns the prior
        record count for the recovery marker.
        """
        row = self._conn.execute(
            "SELECT value FROM session_state WHERE key = 'clean_close'"
        ).fetchone()
        prior_count_row = self._conn.execute("SELECT COUNT(*) FROM journal").fetchone()
        prior_count = prior_count_row[0] if prior_count_row else 0

        if row is None and prior_count > 0:
            # Unclean shutdown from a previous session — promote to crash-dump.
            self._promote_crash_dump(session_id, prior_count)

        if row is not None and prior_count > 0:
            # Clean reuse — prior session closed normally. Truncate stale
            # records so the new session starts with an empty journal.
            self._conn.execute("DELETE FROM journal")
            self._conn.execute("DELETE FROM journal_tags")
        return prior_count

    def _promote_crash_dump(self, session_id: str, prior_count: int) -> None:
        """Copy the unclean prior journal to ``crash-dumps/`` and start fresh."""
        # Capture the prior session's id before we truncate so it can be
        # recorded on the recovery marker (see ``original_session_id``).
        prior_session_row = self._conn.execute(
            "SELECT session_id FROM journal ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        self._original_session_id = prior_session_row[0] if prior_session_row else session_id
        crash_dir = self._root / "crash-dumps"
        crash_path = crash_dir / f"{session_id}.sqlite"

        # Copy rather than move so we can keep writing to the current path.
        # Hold the lock across the close→copy→reopen sequence so no
        # concurrent append() can use the connection while it's closed.
        with self._lock:
            try:
                mkdir_private(crash_dir)
                # Close our live connection so the shared file-level promoter
                # can checkpoint+copy the on-disk database (the same core the
                # orphan sweep uses), then reopen.  Checkpointing folds any
                # WAL-only pages into the main DB before the byte copy.
                self._conn.close()
                _copy_journal_to_crash_dump(self._db_path, crash_path)
                self._conn = self._open_connection()
                # The prior session's records are now safely preserved
                # in the crash dump.  Truncate the live journal so the
                # new session starts fresh at sequence=1 (the documented
                # contract) instead of continuing the prior counter and
                # interleaving prior-session rows under the same id.
                self._conn.execute("DELETE FROM journal")
                self._conn.execute("DELETE FROM journal_tags")
                # Only now — after the crash dump was copied AND the live
                # journal truncated — is the recovery fully successful.
                # Setting the flag here (rather than before the copy)
                # guarantees the seq=0 recovery marker is emitted only on
                # a consistent "started fresh at sequence=1" state.
                self._recovered = True
                logger.info(
                    "Recovered unclean journal for session %s (%d records) → %s",
                    session_id,
                    prior_count,
                    crash_path,
                )
            except (OSError, sqlite3.Error):
                logger.warning(
                    "Failed to promote crash dump for session %s",
                    session_id,
                    exc_info=True,
                )
                self._reopen_after_failed_crash_dump(session_id)

    def _reopen_after_failed_crash_dump(self, session_id: str) -> None:
        # The copy or a PRAGMA may have failed after we closed the
        # connection (close happens before copy).  Reopen it so the
        # rest of __init__ does not run against a closed handle, and
        # truncate the prior-session rows directly: _recovered stays
        # False (no recovery marker), but the new session must still
        # start fresh rather than interleave prior-session records.
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
        self._conn = self._open_connection()
        try:
            self._conn.execute("DELETE FROM journal")
            self._conn.execute("DELETE FROM journal_tags")
        except sqlite3.Error:
            logger.warning(
                "Failed to truncate live journal after crash-dump failure for session %s",
                session_id,
                exc_info=True,
            )

    def _insert_recovery_marker(self, session_id: str, prior_count: int) -> None:
        now = TimingInfo(
            wall_ns=time.time_ns(),
            mono_ns=time.monotonic_ns(),
            cpu_ns=time.process_time_ns(),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO journal "
            "(sequence, session_id, kind, name, wall_ns, mono_ns, data, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                0,
                session_id,
                JournalRecordKind.RECOVERY.value,
                "recovered_session",
                now.wall_ns,
                now.mono_ns,
                json.dumps(
                    {
                        "recovered_record_count": prior_count,
                        "original_session_id": self._original_session_id,
                    }
                ),
                "",
            ),
        )

    # ── ExecutionJournal interface ────────────────────────────────
    # append(), read(), slice(), latest_sequence, degraded, db_path, and
    # _row_to_record are inherited from _SqlJournalBase.  Only the
    # commit/checkpoint-specific overrides live here.

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            try:
                self._conn.execute("COMMIT")
                harden_sqlite_files(self._db_path)
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                pass  # no active transaction or already closed
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO session_state (key, value) VALUES ('clean_close', '1')"
                )
                # Drop the liveness marker: the process is shutting down, so
                # the journal is no longer "live" for the crash sweep.
                self._conn.execute("DELETE FROM session_state WHERE key = 'live_pid'")
                self._clean_close_marked = True
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                pass
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                harden_sqlite_files(self._db_path)
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                logger.debug("WAL checkpoint skipped on close", exc_info=True)
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass  # already closed
        # Run retention opportunistically — never block a turn.
        try:
            run_retention(self._root, mode=self._retention_mode, skip=self._db_path)
        except Exception:
            logger.debug("Retention sweep failed", exc_info=True)

    def flush(self) -> None:
        """Commit the current transaction and start a new one."""
        if self._closed:
            return
        with self._lock:
            try:
                self._conn.execute("COMMIT")
                harden_sqlite_files(self._db_path)
                self._conn.execute("BEGIN")
            except sqlite3.OperationalError:
                pass

    def finalize(self) -> None:
        """Write clean_close marker and checkpoint the WAL without closing the connection.

        Retention is intentionally deferred to ``close()`` so it never blocks
        a turn (see the comment in ``close()``).  The connection remains open
        and a new transaction is started so that subsequent ``append()`` calls
        (e.g. post-stop debug events) are still wrapped in a transaction.
        """
        if self._closed:
            return
        with self._lock:
            try:
                self._conn.execute("COMMIT")
                harden_sqlite_files(self._db_path)
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                pass
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO session_state (key, value) VALUES ('clean_close', '1')"
                )
                self._conn.execute("DELETE FROM session_state WHERE key = 'live_pid'")
                self._conn.commit()
                self._clean_close_marked = True
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                pass
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                harden_sqlite_files(self._db_path)
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                logger.debug("WAL checkpoint skipped on finalize", exc_info=True)
            # Restart a transaction so subsequent appends are batched.
            try:
                self._conn.execute("BEGIN")
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                pass

    # ── Internals ─────────────────────────────────────────────────

    def _do_append(
        self,
        kind: JournalRecordKind,
        name: str,
        session_id: str,
        turn_id: str | None,
        data: dict[str, Any] | None,
        error: ErrorInfo | None,
        tags: frozenset[str],
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> int:
        now_wall = time.time_ns()
        now_mono = time.monotonic_ns()
        now_cpu = time.process_time_ns()

        with self._lock:
            clear_clean_close = self._clean_close_marked
            previous_seq = self._seq
            if clear_clean_close:
                self._conn.execute("SAVEPOINT post_finalize_append")
            # The savepoint only buys multi-statement atomicity (row INSERT +
            # tag-index INSERTs); a lone INSERT is already statement-atomic,
            # so skip the two extra statements per untagged hot-path append.
            append_savepoint_open = bool(tags)
            if append_savepoint_open:
                self._conn.execute("SAVEPOINT journal_append")
            try:
                if clear_clean_close:
                    self._clear_clean_close_marker_before_write()
                self._seq = previous_seq + 1
                seq = self._seq
                record = _journal_record_for_append(
                    sequence=seq,
                    session_id=session_id,
                    kind=kind,
                    name=name,
                    timing=TimingInfo(wall_ns=now_wall, mono_ns=now_mono, cpu_ns=now_cpu),
                    turn_id=turn_id,
                    data=data,
                    error=error,
                    tags=tags,
                    input_ref=input_ref,
                    output_ref=output_ref,
                )
                self._conn.execute(
                    _JOURNAL_INSERT_SQL,
                    _encode_journal_row(
                        sequence=record.sequence,
                        session_id=record.session_id,
                        kind=record.kind,
                        name=record.name,
                        wall_ns=record.timing.wall_ns,
                        mono_ns=record.timing.mono_ns,
                        cpu_ns=record.timing.cpu_ns,
                        turn_id=record.turn_id,
                        data=record.data,
                        error=record.error,
                        tags=record.tags,
                        input_ref=record.input_ref,
                        output_ref=record.output_ref,
                    ),
                )
                # Same transaction as the row above — no extra COMMIT.
                _insert_tag_index_rows(self._conn, record.sequence, record.tags)
                if append_savepoint_open:
                    self._conn.execute("RELEASE SAVEPOINT journal_append")
                    append_savepoint_open = False
            except Exception:
                self._seq = previous_seq
                try:
                    if append_savepoint_open:
                        try:
                            self._conn.execute("ROLLBACK TO SAVEPOINT journal_append")
                        finally:
                            self._conn.execute("RELEASE SAVEPOINT journal_append")
                finally:
                    if clear_clean_close:
                        try:
                            self._conn.execute("ROLLBACK TO SAVEPOINT post_finalize_append")
                        finally:
                            self._conn.execute("RELEASE SAVEPOINT post_finalize_append")
                raise
            if clear_clean_close:
                self._conn.execute("RELEASE SAVEPOINT post_finalize_append")
                self._clean_close_marked = False
            else:
                # Commit on every append so records genuinely survive process
                # death (SIGKILL/OOM/segfault), honoring the DURABILITY.md
                # contract.  Under ``synchronous=NORMAL`` this is only a
                # ``write()`` into the kernel page cache (no fsync), so the
                # per-turn latency budget still holds.  Reopen a transaction so
                # ``flush()``/``finalize()``/``close()`` always find an active
                # one to COMMIT and the post-finalize SAVEPOINT machinery keeps
                # working.  The post-finalize branch is intentionally NOT
                # committed here: it must stay rolled-back-able so a crash after
                # ``finalize()`` leaves the durable DB looking cleanly closed.
                #
                # Permissions are hardened once at open (after the WAL PRAGMAs
                # create the sidecars) and re-hardened at every checkpoint
                # boundary (flush/finalize/close); re-chmod'ing on every
                # per-token COMMIT only adds redundant stat/chmod syscalls to the
                # hot path, so it is intentionally omitted here.
                self._conn.execute("COMMIT")
                self._conn.execute("BEGIN")
        return seq

    def _clear_clean_close_marker_before_write(self) -> None:
        self._conn.execute("DELETE FROM session_state WHERE key = 'clean_close'")

    def _enter_degraded(self, session_id: str, exc: Exception) -> None:
        self._degraded = True
        observe_gauge("easycat.journal.degraded", 1)
        logger.warning("Journal entered degraded mode: %s: %s", type(exc).__name__, exc)
        # After finalize() the journal is contractually "cleanly closed": a
        # failed post-finalize append must leave no durable trace (the
        # SAVEPOINT in _do_append already rolled its write back, restoring the
        # clean_close marker).  Persisting a degraded marker here would both
        # add a spurious journal row and COMMIT, defeating that rollback and
        # making a crash-after-finalize DB look uncleanly closed.  Skip it.
        if self._clean_close_marked:
            return
        with self._lock:
            _persist_degraded_marker(self._conn, session_id, exc)


# ── Litestream adapter ──────────────────────────────────────────


def _sanitize_replica_url(url: str) -> str:
    """Return ``scheme://host`` from a replica URL, stripping path and credentials."""
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.hostname or ''}"
    except Exception:
        return "<unparseable>"


class LitestreamSqliteJournal:
    """SqliteJournal with a Litestream sidecar for WAL replication.

    Delegates all journal operations to an inner ``SqliteJournal``.  On
    construction, starts ``litestream replicate`` pointing at the SQLite
    DB file.  If the ``litestream`` binary is not on ``$PATH``, logs a
    warning and degrades to plain ``SqliteJournal`` (no crash).
    """

    def __init__(
        self,
        session_id: str,
        *,
        data_dir: str | Path | None = None,
        replica_url: str | None = None,
        retention_mode: Literal["archive", "delete"] = "archive",
    ) -> None:
        self._inner = SqliteJournal(session_id, data_dir=data_dir, retention_mode=retention_mode)
        self._replica_url = replica_url or os.environ.get("EASYCAT_JOURNAL_LITESTREAM_REPLICA", "")
        self._sidecar: subprocess.Popen[bytes] | None = None
        self._litestream_available = False
        self._stderr_thread: threading.Thread | None = None

        if not self._replica_url:
            logger.warning(
                "LitestreamSqliteJournal: no replica URL configured "
                "(set EASYCAT_JOURNAL_LITESTREAM_REPLICA); running as plain SQLite"
            )
            return

        litestream_bin = shutil.which("litestream")
        if litestream_bin is None:
            logger.warning(
                "LitestreamSqliteJournal: litestream binary not found on PATH; "
                "degrading to plain SqliteJournal"
            )
            return

        self._litestream_available = True
        safe_url = _sanitize_replica_url(self._replica_url)
        try:
            self._sidecar = subprocess.Popen(
                [
                    litestream_bin,
                    "replicate",
                    str(self._inner.db_path),
                    self._replica_url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            # Drain stderr on a daemon thread so a full OS pipe buffer can never
            # block (and silently stall) the sidecar, and so replication errors
            # surface in the logs instead of being lost.
            if self._sidecar.stderr is not None:
                self._stderr_thread = threading.Thread(
                    target=self._drain_stderr,
                    args=(self._sidecar.stderr,),
                    daemon=True,
                    name="litestream-stderr",
                )
                self._stderr_thread.start()
            logger.info(
                "Journal: backend=sqlite+litestream replica=%s pid=%d path=%s",
                safe_url,
                self._sidecar.pid,
                self._inner.db_path,
            )
        except OSError as exc:
            logger.warning(
                "LitestreamSqliteJournal: failed to start sidecar (%s); "
                "degrading to plain SqliteJournal",
                exc,
            )
            self._sidecar = None
            self._litestream_available = False

    # ── Delegated ExecutionJournal interface ──────────────────────

    def append(
        self,
        kind: JournalRecordKind,
        name: str,
        session_id: str,
        turn_id: str | None = None,
        data: dict[str, Any] | None = None,
        error: ErrorInfo | None = None,
        tags: frozenset[str] = frozenset(),
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> int:
        return self._inner.append(
            kind,
            name,
            session_id,
            turn_id,
            data,
            error,
            tags,
            input_ref,
            output_ref,
        )

    def read(self, start: int = 0, limit: int | None = None) -> list[JournalRecord]:
        return self._inner.read(start=start, limit=limit)

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        name: str | None = None,
        tags: frozenset[str] | None = None,
    ) -> list[JournalRecord]:
        return self._inner.slice(
            kind=kind,
            session_id=session_id,
            turn_id=turn_id,
            name=name,
            tags=tags,
        )

    def flush(self) -> None:
        self._inner.flush()

    def finalize(self) -> None:
        self._inner.finalize()

    def close(self) -> None:
        self._stop_sidecar()
        self._inner.close()

    @property
    def latest_sequence(self) -> int:
        return self._inner.latest_sequence

    @property
    def degraded(self) -> bool:
        return self._inner.degraded

    @property
    def db_path(self) -> Path:
        return self._inner.db_path

    # ── Internals ────────────────────────────────────────────────

    @staticmethod
    def _drain_stderr(stream: Any) -> None:
        """Forward litestream sidecar stderr to the logger until EOF."""
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    logger.warning("litestream: %s", line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _stop_sidecar(self) -> None:
        if self._sidecar is None:
            return
        try:
            self._sidecar.send_signal(signal.SIGTERM)
            self._sidecar.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._sidecar.kill()
            self._sidecar.wait(timeout=2)
        except OSError:
            pass
        finally:
            # The drain thread closes the pipe on EOF; join it so the fd is
            # released before we drop our reference to the process.
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=2)
                self._stderr_thread = None
            if self._sidecar.stderr is not None:
                try:
                    self._sidecar.stderr.close()
                except OSError:
                    pass
            self._sidecar = None


# ── libSQL adapter ──────────────────────────────────────────────


class LibsqlJournal(_SqlJournalBase):
    """Journal backend using the libSQL embedded-replica SDK.

    Reads are local; appends commit locally and sync to the remote
    primary asynchronously every ``sync_interval_s`` seconds (default 10,
    configurable via ``EASYCAT_JOURNAL_LIBSQL_SYNC_INTERVAL_S``).

    If the ``libsql_experimental`` SDK is not installed, logs a warning
    and raises ``ImportError`` — the factory catches this and falls back
    to ``SqliteJournal``.
    """

    def __init__(
        self,
        session_id: str,
        *,
        data_dir: str | Path | None = None,
        sync_url: str | None = None,
        auth_token: str | None = None,
        sync_interval_s: float | None = None,
    ) -> None:
        import libsql_experimental as libsql  # noqa: F811 — intentional conditional import

        self._libsql = libsql

        root = Path(data_dir) if data_dir else Path(os.environ.get("EASYCAT_DATA_DIR", ".easycat"))
        journals_dir = root / "journals"
        mkdir_private(journals_dir)
        self._db_path = journals_dir / f"{session_id}.sqlite"
        touch_private_file(self._db_path)

        url = sync_url or os.environ.get("EASYCAT_LIBSQL_URL", "")
        token = auth_token or os.environ.get("EASYCAT_LIBSQL_AUTH_TOKEN", "")

        connect_kwargs: dict[str, Any] = {"uri": str(self._db_path)}
        if url:
            connect_kwargs["sync_url"] = url
        if token:
            connect_kwargs["auth_token"] = token

        self._conn = libsql.connect(**connect_kwargs)
        harden_sqlite_files(self._db_path)
        self._conn.executescript(_SQLITE_SCHEMA)
        _ensure_journal_schema(self._conn)

        # Handle session-id reuse: mirror only SqliteJournal's *clean-reuse*
        # truncation.  libSQL does NOT implement crash recovery — there is no
        # crash-dump promotion, no RecoveredSessionMarker, and no _recovered
        # flag.  An unclean reuse continues appending into the prior table with
        # a continued sequence counter.  This divergence from the SqliteJournal
        # contract is documented in DURABILITY.md ("Backend support").
        row = self._conn.execute(
            "SELECT value FROM session_state WHERE key = 'clean_close'"
        ).fetchone()
        prior_count_row = self._conn.execute("SELECT COUNT(*) FROM journal").fetchone()
        prior_count = prior_count_row[0] if prior_count_row else 0

        truncated = row is not None and prior_count > 0
        if truncated:
            # Clean reuse — the prior (cleanly closed) journal is discarded, so
            # its persisted ``degraded`` marker would be stale.  Clear both the
            # ``clean_close`` and ``degraded`` keys alongside the truncation.
            self._conn.execute("DELETE FROM journal")
            self._conn.execute("DELETE FROM journal_tags")
            self._conn.execute(
                "DELETE FROM session_state WHERE key IN ('clean_close', 'degraded')"
            )
        else:
            # Unclean reuse — prior rows are retained (libSQL has no crash
            # recovery), including any ``JournalDegraded`` row.  Only clear the
            # ``clean_close`` marker; preserve ``degraded`` so file/bundle
            # inspection stays consistent with the retained history.
            self._conn.execute("DELETE FROM session_state WHERE key = 'clean_close'")

        # Unclean reuse retains prior rows, so pre-v2 files must be
        # backfilled here (post-truncation) for stage/tag queries to see them.
        _ensure_index_backfill(self._conn)

        # Recover sequence counter from any remaining records.
        row = self._conn.execute("SELECT MAX(sequence) FROM journal").fetchone()
        self._seq = row[0] if row and row[0] is not None else 0

        self._lock = threading.Lock()
        self._degraded = False
        self._closed = False

        # Periodic sync configuration.
        self._sync_interval = sync_interval_s
        if self._sync_interval is None:
            self._sync_interval = float(
                os.environ.get("EASYCAT_JOURNAL_LIBSQL_SYNC_INTERVAL_S", "10")
            )

        self._sync_stop = threading.Event()
        self._sync_thread: threading.Thread | None = None
        if url:
            self._sync_thread = threading.Thread(
                target=self._sync_loop,
                daemon=True,
                name="libsql-sync",
            )
            self._sync_thread.start()

        logger.info(
            "Journal: backend=libsql sync_interval=%.1fs path=%s",
            self._sync_interval,
            self._db_path,
        )

    # ── ExecutionJournal interface ───────────────────────────────
    # append(), read(), slice(), latest_sequence, degraded, db_path, and
    # _row_to_record are inherited from _SqlJournalBase.  Only the
    # sync-specific overrides live here.

    def flush(self) -> None:
        if self._closed:
            return
        try:
            with self._lock:
                self._conn.sync()
                # Re-harden at the flush (rotation) boundary: sidecars created
                # after open pick up private perms here rather than on every
                # append (see ``_do_append``).
                harden_sqlite_files(self._db_path)
        except Exception:
            logger.debug("libsql sync failed during flush", exc_info=True)

    def finalize(self) -> None:
        if self._closed:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO session_state (key, value) VALUES ('clean_close', '1')"
                )
                self._conn.commit()
                harden_sqlite_files(self._db_path)
        except Exception:
            logger.debug("libsql clean_close marker write failed", exc_info=True)
        try:
            with self._lock:
                self._conn.sync()
        except Exception:
            logger.debug("libsql sync failed during finalize", exc_info=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        # Stop the sync thread.
        self._sync_stop.set()
        if self._sync_thread is not None:
            self._sync_thread.join(timeout=5)

        # Final sync.
        try:
            self._conn.sync()
            harden_sqlite_files(self._db_path)
        except Exception:
            logger.debug("libsql final sync failed on close", exc_info=True)

        try:
            self._conn.close()
        except Exception:
            pass

    # ── Internals ────────────────────────────────────────────────

    def _do_append(
        self,
        kind: JournalRecordKind,
        name: str,
        session_id: str,
        turn_id: str | None,
        data: dict[str, Any] | None,
        error: ErrorInfo | None,
        tags: frozenset[str],
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> int:
        now_wall = time.time_ns()
        now_mono = time.monotonic_ns()
        now_cpu = time.process_time_ns()
        with self._lock:
            previous_seq = self._seq
            self._seq = previous_seq + 1
            seq = self._seq
            try:
                record = _journal_record_for_append(
                    sequence=seq,
                    session_id=session_id,
                    kind=kind,
                    name=name,
                    timing=TimingInfo(wall_ns=now_wall, mono_ns=now_mono, cpu_ns=now_cpu),
                    turn_id=turn_id,
                    data=data,
                    error=error,
                    tags=tags,
                    input_ref=input_ref,
                    output_ref=output_ref,
                )
                self._conn.execute(
                    _JOURNAL_INSERT_SQL,
                    _encode_journal_row(
                        sequence=record.sequence,
                        session_id=record.session_id,
                        kind=record.kind,
                        name=record.name,
                        wall_ns=record.timing.wall_ns,
                        mono_ns=record.timing.mono_ns,
                        cpu_ns=record.timing.cpu_ns,
                        turn_id=record.turn_id,
                        data=record.data,
                        error=record.error,
                        tags=record.tags,
                        input_ref=record.input_ref,
                        output_ref=record.output_ref,
                    ),
                )
                # Populated in the same commit as the row — no extra COMMIT.
                _insert_tag_index_rows(self._conn, record.sequence, record.tags)
                self._conn.commit()
            except Exception:
                self._seq = previous_seq
                try:
                    self._conn.rollback()
                except Exception:
                    logger.debug("libsql append rollback failed", exc_info=True)
                raise
            # NB: file-permission hardening is intentionally NOT done here.  It
            # is a stat+chmod over the DB and its WAL/SHM sidecars, so running
            # it on every append wastes syscalls on the hot path.  Hardening
            # happens once per open (``__init__``) and at each rotation boundary
            # (``flush``/``finalize``/``close``), mirroring ``SqliteJournal``.
        return seq

    def _sync_loop(self) -> None:
        """Background thread: periodically call ``conn.sync()``."""
        while not self._sync_stop.wait(timeout=self._sync_interval):
            try:
                with self._lock:
                    self._conn.sync()
            except Exception:
                logger.debug("libsql periodic sync failed", exc_info=True)

    def _enter_degraded(self, session_id: str, exc: Exception) -> None:
        self._degraded = True
        observe_gauge("easycat.journal.degraded", 1)
        logger.warning("Journal entered degraded mode: %s: %s", type(exc).__name__, exc)
        with self._lock:
            _persist_degraded_marker(self._conn, session_id, exc)
