"""ExecutionJournal protocol, JournalView, InMemoryRingBuffer, and SqliteJournal backends."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlparse

from easycat.observability import observe_gauge, record_histogram
from easycat.runtime.records import (
    BufferOverflow,
    ErrorInfo,
    JournalDegraded,
    JournalRecord,
    JournalRecordKind,
    TimingInfo,
)

if TYPE_CHECKING:
    from easycat.runtime.artifacts import InMemoryArtifactStore

logger = logging.getLogger(__name__)

__all__ = [
    "ExecutionJournal",
    "InMemoryRingBuffer",
    "JournalView",
    "LibsqlJournal",
    "LitestreamSqliteJournal",
    "ReadonlySqliteJournal",
    "SqliteJournal",
    "create_journal",
]


# ── Protocol ──────────────────────────────────────────────────────


@runtime_checkable
class ExecutionJournal(Protocol):
    """Append-only structured journal for session records."""

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
        """Append a record. Returns the assigned sequence number.

        *input_ref* / *output_ref* are stable artifact-store refs (SHA-256
        hex).  The caller must ensure the referenced artifact has been
        committed **before** calling ``append`` — this is the atomicity
        contract that guarantees no durable record carries a dangling ref.

        Must never raise — failures trigger degraded mode.
        """
        ...

    def read(self, start: int = 0, limit: int | None = None) -> list[JournalRecord]:
        """Return records with ``sequence >= start``, up to *limit*."""
        ...

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
    ) -> list[JournalRecord]:
        """Return records matching the given filters."""
        ...

    def close(self) -> None: ...

    def flush(self) -> None: ...

    def finalize(self) -> None:
        """Mark the session as cleanly closed without closing the backend.

        Writes the ``clean_close`` marker (for backends that support it)
        so that a subsequent session with the same id is not treated as
        crash recovery.  The backend remains readable — callers can still
        query records after this call.  ``close()`` is still required to
        release the underlying connection.
        """
        ...

    @property
    def latest_sequence(self) -> int: ...

    @property
    def degraded(self) -> bool: ...


# ── JournalView (read-only surface) ──────────────────────────────


class JournalView:
    """Read-only view exposed as ``Session.journal``."""

    def __init__(self, journal: ExecutionJournal) -> None:
        self._journal = journal

    def read(self, start: int = 0, limit: int | None = None) -> list[JournalRecord]:
        return self._journal.read(start=start, limit=limit)

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
    ) -> list[JournalRecord]:
        return self._journal.slice(kind=kind, session_id=session_id)

    def filter_by_stage(self, stage_name: str) -> list[JournalRecord]:
        """Return records whose ``data['stage']`` or ``data['observed_stage']``
        matches *stage_name*.  Mirrors :meth:`RunBundle.filter_by_stage`.
        """
        results: list[JournalRecord] = []
        for r in self._journal.read():
            stage = r.data.get("stage")
            observed = r.data.get("observed_stage")
            if stage == stage_name or observed == stage_name:
                results.append(r)
        return results

    def filter_by_turn(self, turn_id: str) -> list[JournalRecord]:
        """Return records whose ``turn_id`` matches.  Mirrors
        :meth:`RunBundle.filter_by_turn`."""
        return [r for r in self._journal.read() if r.turn_id == turn_id]

    def lookup_by_sequence(self, seq: int) -> JournalRecord | None:
        """Return the record with the given sequence number, or ``None``.
        Mirrors :meth:`RunBundle.lookup_by_sequence`."""
        for r in self._journal.read():
            if r.sequence == seq:
                return r
        return None

    async def follow(
        self,
        *,
        from_sequence: int | None = None,
        poll_interval: float = 0.05,
    ) -> collections.abc.AsyncIterator[JournalRecord]:
        """Yield new records as they are appended.

        *from_sequence* sets the starting cursor.  ``None`` (default) means
        start after the current ``latest_sequence`` — i.e. only future records.
        Pass ``0`` to replay the full history then live-tail.

        Polls ``latest_sequence`` on *poll_interval* seconds.
        """
        if from_sequence is not None:
            cursor = from_sequence
        else:
            # Read latest_sequence and compute cursor atomically — the
            # property getter holds the backend lock, so no record can
            # slip in between read and +1.
            cursor = self._journal.latest_sequence + 1
        while True:
            # Fetch records from cursor onward.  read() is lock-protected
            # in every backend, so we won't miss records that were appended
            # between the previous iteration's yield and this call.
            records = self._journal.read(start=cursor)
            for rec in records:
                yield rec
                # Advance cursor past the yielded record so we never
                # re-deliver it, even if the caller suspends mid-batch.
                cursor = rec.sequence + 1
            await asyncio.sleep(poll_interval)

    @property
    def enabled(self) -> bool:
        return True

    @property
    def degraded(self) -> bool:
        return self._journal.degraded


class ReadonlySqliteJournal:
    """Read-only wrapper over a persisted SQLite journal file.

    Used after session teardown so callers can still inspect or export
    the final journal after the live backend connection, Litestream
    sidecar, or libSQL sync thread has been closed.
    """

    def __init__(self, db_path: str | Path, *, degraded: bool = False) -> None:
        self._db_path = Path(db_path)
        self._degraded = degraded

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
        return -1

    def read(self, start: int = 0, limit: int | None = None) -> list[JournalRecord]:
        sql = "SELECT * FROM journal WHERE sequence >= ? ORDER BY sequence"
        params: list[Any] = [start]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self._query(sql, params)

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
    ) -> list[JournalRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._query(f"SELECT * FROM journal{where} ORDER BY sequence", params)

    def close(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def finalize(self) -> None:
        pass

    @property
    def latest_sequence(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(sequence) FROM journal").fetchone()
        return row[0] if row and row[0] is not None else 0

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)

    def _query(self, sql: str, params: list[Any]) -> list[JournalRecord]:
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [SqliteJournal._row_to_record(r) for r in rows]


# ── Frozen snapshot (read-only in-memory journal) ────────────────


class FrozenJournalSnapshot:
    """Immutable point-in-time copy of an in-memory journal."""

    def __init__(self, records: list[JournalRecord], *, degraded: bool = False) -> None:
        self._records = tuple(records)
        self._degraded = degraded

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
        return -1

    def read(self, start: int = 0, limit: int | None = None) -> list[JournalRecord]:
        out = [r for r in self._records if r.sequence >= start]
        if limit is not None:
            out = out[:limit]
        return out

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
    ) -> list[JournalRecord]:
        out = list(self._records)
        if kind is not None:
            out = [r for r in out if r.kind == kind]
        if session_id is not None:
            out = [r for r in out if r.session_id == session_id]
        return out

    def close(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def finalize(self) -> None:
        pass

    @property
    def latest_sequence(self) -> int:
        return self._records[-1].sequence if self._records else 0

    @property
    def degraded(self) -> bool:
        return self._degraded


# ── InMemoryRingBuffer backend ───────────────────────────────────


class InMemoryRingBuffer:
    """Bounded in-memory journal backend.

    Safe for concurrent sync writes (``threading.Lock``).  Drops the oldest
    record when capacity is exceeded and emits a ``BufferOverflow`` marker.
    """

    def __init__(
        self,
        capacity: int = 10_000,
        artifact_store: InMemoryArtifactStore | None = None,
    ) -> None:
        self._capacity = capacity
        self._buf: collections.deque[JournalRecord] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self._degraded = False
        logger.debug("In-memory journal: crash-durability waived (data lost on process exit)")
        self._overflow_pending = False
        self._artifact_store = artifact_store
        self._ref_counts: dict[str, int] = {}  # ref → number of records referencing it

    # ── ExecutionJournal interface ────────────────────────────────

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
        if self._degraded:
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
        with self._lock:
            out = [r for r in self._buf if r.sequence >= start]
        if limit is not None:
            out = out[:limit]
        return out

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
    ) -> list[JournalRecord]:
        with self._lock:
            out = list(self._buf)
        if kind is not None:
            out = [r for r in out if r.kind == kind]
        if session_id is not None:
            out = [r for r in out if r.session_id == session_id]
        return out

    def close(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def finalize(self) -> None:
        pass

    def snapshot(self) -> FrozenJournalSnapshot:
        """Return a read-only copy of the current buffer contents."""
        with self._lock:
            return FrozenJournalSnapshot(list(self._buf), degraded=self._degraded)

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            return self._seq

    @property
    def degraded(self) -> bool:
        return self._degraded

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
        now_timing = TimingInfo(
            wall_ns=time.time_ns(),
            mono_ns=time.monotonic_ns(),
            cpu_ns=time.process_time_ns(),
        )
        with self._lock:
            was_full = len(self._buf) == self._capacity

            # Collect artifact refs from the record about to be evicted.
            evicted_refs: list[str] = []
            if was_full and self._buf:
                evicted = self._buf[0]
                if evicted.input_ref:
                    evicted_refs.append(evicted.input_ref)
                if evicted.output_ref:
                    evicted_refs.append(evicted.output_ref)

            self._seq += 1
            seq = self._seq
            record = JournalRecord(
                sequence=seq,
                session_id=session_id,
                kind=kind,
                name=name,
                timing=now_timing,
                turn_id=turn_id,
                data=data or {},
                error=error,
                input_ref=input_ref,
                output_ref=output_ref,
                tags=tags,
            )
            self._buf.append(record)

            # Track ref counts for the new record.
            if input_ref:
                self._ref_counts[input_ref] = self._ref_counts.get(input_ref, 0) + 1
            if output_ref:
                self._ref_counts[output_ref] = self._ref_counts.get(output_ref, 0) + 1

            # Decrement ref counts for evicted record and clean up orphans.
            if was_full:
                self._decrement_and_evict_refs(evicted_refs)

            if was_full and not self._overflow_pending:
                self._overflow_pending = True
                # The overflow marker itself may evict another record.
                evicted_refs_marker: list[str] = []
                if len(self._buf) == self._capacity and self._buf:
                    evicted_m = self._buf[0]
                    if evicted_m.input_ref:
                        evicted_refs_marker.append(evicted_m.input_ref)
                    if evicted_m.output_ref:
                        evicted_refs_marker.append(evicted_m.output_ref)

                self._seq += 1
                marker = BufferOverflow(
                    sequence=self._seq,
                    session_id=session_id,
                    timing=now_timing,
                    data={"dropped_from": "ring_buffer"},
                )
                self._buf.append(marker)

                if evicted_refs_marker:
                    self._decrement_and_evict_refs(evicted_refs_marker)
        return seq

    def _decrement_and_evict_refs(self, refs: list[str]) -> None:
        """Decrement ref counts and delete orphaned artifacts. Caller holds lock."""
        if not self._artifact_store:
            return
        for ref in refs:
            count = self._ref_counts.get(ref, 0) - 1
            if count <= 0:
                self._ref_counts.pop(ref, None)
                self._artifact_store.delete(ref)
            else:
                self._ref_counts[ref] = count

    def _enter_degraded(self, session_id: str, exc: Exception) -> None:
        self._degraded = True
        observe_gauge("easycat.journal.degraded", 1)
        marker = JournalDegraded(
            sequence=-1,
            session_id=session_id,
            timing=TimingInfo(
                wall_ns=time.time_ns(),
                mono_ns=time.monotonic_ns(),
                cpu_ns=time.process_time_ns(),
            ),
            data={
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        print(
            f"[easycat] journal degraded: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        logger.warning("Journal entered degraded mode: %s: %s", type(exc).__name__, exc)
        # Try to write the marker — best-effort.
        try:
            with self._lock:
                self._seq += 1
                self._buf.append(
                    JournalDegraded(
                        sequence=self._seq,
                        session_id=marker.session_id,
                        timing=marker.timing,
                        data=marker.data,
                    )
                )
        except Exception:
            pass


# ── SQLite backend ──────────────────────────────────────────────


_SQLITE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS journal (
    sequence     INTEGER PRIMARY KEY,
    session_id   TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    op_id        TEXT    NOT NULL DEFAULT '',
    name         TEXT    NOT NULL DEFAULT '',
    wall_ns      INTEGER NOT NULL DEFAULT 0,
    mono_ns      INTEGER NOT NULL DEFAULT 0,
    cpu_ns       INTEGER NOT NULL DEFAULT 0,
    queue_ns     INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS session_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
"""


class SqliteJournal:
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
        journals_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = journals_dir / f"{session_id}.sqlite"
        self._session_id = session_id
        self._lock = threading.Lock()
        self._seq = 0
        self._degraded = False
        self._closed = False
        self._recovered = False
        self._clean_close_marked = False

        # ── Check for prior unclean shutdown ─────────────────────
        existed = self._db_path.exists()

        # Eager warmup — open DB and apply PRAGMAs now.
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit for PRAGMAs
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA wal_autocheckpoint=0")
        self._conn.executescript(_SQLITE_SCHEMA)

        # Detect unclean shutdown: file existed but clean_close marker absent.
        if existed:
            row = self._conn.execute(
                "SELECT value FROM session_state WHERE key = 'clean_close'"
            ).fetchone()
            prior_count_row = self._conn.execute("SELECT COUNT(*) FROM journal").fetchone()
            prior_count = prior_count_row[0] if prior_count_row else 0

            if row is None and prior_count > 0:
                # Unclean shutdown from a previous session — promote to crash-dump.
                self._recovered = True
                crash_dir = root / "crash-dumps"
                crash_dir.mkdir(parents=True, exist_ok=True)
                crash_path = crash_dir / f"{session_id}.sqlite"
                # Copy rather than move so we can keep writing to the current path.
                import shutil

                # Hold the lock across the close→copy→reopen sequence so no
                # concurrent append() can use the connection while it's closed.
                with self._lock:
                    try:
                        # Checkpoint WAL into the main database before copying.
                        # With wal_autocheckpoint=0, recent records may only
                        # exist in the WAL file; a bare file copy would lose them.
                        try:
                            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        except sqlite3.OperationalError:
                            pass  # Best-effort; copy WAL files as fallback below.
                        self._conn.close()
                        shutil.copy2(str(self._db_path), str(crash_path))
                        # Also copy WAL/SHM if they still exist (checkpoint may
                        # have been incomplete due to concurrent readers).
                        for suffix in ("-wal", "-shm"):
                            wal_src = Path(str(self._db_path) + suffix)
                            if wal_src.exists():
                                shutil.copy2(str(wal_src), str(crash_path) + suffix)
                        self._conn = sqlite3.connect(
                            str(self._db_path),
                            check_same_thread=False,
                            isolation_level=None,
                        )
                        self._conn.execute("PRAGMA journal_mode=WAL")
                        self._conn.execute("PRAGMA synchronous=NORMAL")
                        self._conn.execute("PRAGMA wal_autocheckpoint=0")
                        logger.info(
                            "Recovered unclean journal for session %s (%d records) → %s",
                            session_id,
                            prior_count,
                            crash_path,
                        )
                    except OSError:
                        logger.warning(
                            "Failed to promote crash dump for session %s",
                            session_id,
                            exc_info=True,
                        )

            if row is not None and prior_count > 0:
                # Clean reuse — prior session closed normally. Truncate stale
                # records so the new session starts with an empty journal.
                self._conn.execute("DELETE FROM journal")

        # Clear the clean_close marker (we're starting a new session).
        self._conn.execute("DELETE FROM session_state WHERE key = 'clean_close'")

        # Recover sequence counter from any existing records (crash-recovery
        # path keeps old rows; clean-reuse truncates them above).
        row = self._conn.execute("SELECT MAX(sequence) FROM journal").fetchone()
        if row and row[0] is not None:
            self._seq = row[0]

        # Start a transaction for batched writes.
        self._conn.execute("BEGIN")

        # Emit recovery marker at sequence=0 if we detected unclean shutdown.
        if self._recovered:
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
                    json.dumps({"recovered_record_count": prior_count}),
                    "",
                ),
            )

    # ── ExecutionJournal interface ────────────────────────────────

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
    ) -> list[JournalRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM journal{where} ORDER BY sequence", params
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            try:
                self._conn.execute("COMMIT")
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                pass  # no active transaction or already closed
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO session_state (key, value) VALUES ('clean_close', '1')"
                )
                self._clean_close_marked = True
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                pass
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                logger.debug("WAL checkpoint skipped on close", exc_info=True)
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass  # already closed
        # Run retention opportunistically — never block a turn.
        try:
            run_retention(self._root, mode=self._retention_mode)
        except Exception:
            logger.debug("Retention sweep failed", exc_info=True)

    def flush(self) -> None:
        """Commit the current transaction and start a new one."""
        if self._closed:
            return
        with self._lock:
            try:
                self._conn.execute("COMMIT")
                self._conn.execute("BEGIN")
            except sqlite3.OperationalError:
                pass

    def finalize(self) -> None:
        """Write clean_close marker and run retention without closing the connection.

        The connection remains open and a new transaction is started so that
        subsequent ``append()`` calls (e.g. post-stop debug events) are still
        wrapped in a transaction.
        """
        if self._closed:
            return
        with self._lock:
            try:
                self._conn.execute("COMMIT")
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                pass
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO session_state (key, value) VALUES ('clean_close', '1')"
                )
                self._conn.commit()
                self._clean_close_marked = True
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                pass
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                logger.debug("WAL checkpoint skipped on finalize", exc_info=True)
            # Restart a transaction so subsequent appends are batched.
            try:
                self._conn.execute("BEGIN")
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                pass

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
        data_json = json.dumps(data or {}, default=str)
        error_notes = error.notes if error else None
        tags_csv = ",".join(sorted(tags)) if tags else ""

        with self._lock:
            clear_clean_close = self._clean_close_marked
            if clear_clean_close:
                self._conn.execute("SAVEPOINT post_finalize_append")
            try:
                if clear_clean_close:
                    self._clear_clean_close_marker_before_write()
                self._seq += 1
                seq = self._seq
                self._conn.execute(
                    "INSERT INTO journal "
                    "(sequence, session_id, kind, name, wall_ns, mono_ns, cpu_ns, "
                    "turn_id, data, error_type, error_msg, error_tb, error_notes, "
                    "input_ref, output_ref, tags) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        seq,
                        session_id,
                        kind.value,
                        name,
                        now_wall,
                        now_mono,
                        now_cpu,
                        turn_id,
                        data_json,
                        error.type if error else None,
                        error.message if error else None,
                        error.traceback if error else None,
                        error_notes,
                        input_ref,
                        output_ref,
                        tags_csv,
                    ),
                )
            except Exception:
                if clear_clean_close:
                    try:
                        self._conn.execute("ROLLBACK TO SAVEPOINT post_finalize_append")
                    finally:
                        self._conn.execute("RELEASE SAVEPOINT post_finalize_append")
                raise
            if clear_clean_close:
                self._conn.execute("RELEASE SAVEPOINT post_finalize_append")
                self._clean_close_marked = False
        return seq

    def _clear_clean_close_marker_before_write(self) -> None:
        self._conn.execute("DELETE FROM session_state WHERE key = 'clean_close'")

    def _enter_degraded(self, session_id: str, exc: Exception) -> None:
        self._degraded = True
        observe_gauge("easycat.journal.degraded", 1)
        print(
            f"[easycat] journal degraded: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        logger.warning("Journal entered degraded mode: %s: %s", type(exc).__name__, exc)

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> JournalRecord:
        (
            sequence,
            session_id,
            kind_str,
            op_id,
            name,
            wall_ns,
            mono_ns,
            cpu_ns,
            queue_ns,
            turn_id,
            data_str,
            error_type,
            error_msg,
            error_tb,
            error_notes,
            input_ref,
            output_ref,
            tags_str,
        ) = row
        error = None
        if error_type:
            error = ErrorInfo(
                type=error_type,
                message=error_msg or "",
                traceback=error_tb,
                notes=error_notes,
            )
        tag_set = frozenset(tags_str.split(",")) if tags_str else frozenset()
        return JournalRecord(
            sequence=sequence,
            session_id=session_id,
            kind=JournalRecordKind(kind_str),
            op_id=op_id,
            name=name,
            timing=TimingInfo(wall_ns=wall_ns, mono_ns=mono_ns, cpu_ns=cpu_ns, queue_ns=queue_ns),
            turn_id=turn_id,
            data=json.loads(data_str) if data_str else {},
            error=error,
            input_ref=input_ref,
            output_ref=output_ref,
            tags=tag_set,
        )


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
    ) -> list[JournalRecord]:
        return self._inner.slice(kind=kind, session_id=session_id)

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
            self._sidecar = None


# ── libSQL adapter ──────────────────────────────────────────────


class LibsqlJournal:
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
        journals_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = journals_dir / f"{session_id}.sqlite"

        url = sync_url or os.environ.get("EASYCAT_LIBSQL_URL", "")
        token = auth_token or os.environ.get("EASYCAT_LIBSQL_AUTH_TOKEN", "")

        connect_kwargs: dict[str, Any] = {"uri": str(self._db_path)}
        if url:
            connect_kwargs["sync_url"] = url
        if token:
            connect_kwargs["auth_token"] = token

        self._conn = libsql.connect(**connect_kwargs)
        self._conn.executescript(_SQLITE_SCHEMA)

        # Handle session-id reuse: mirror SqliteJournal's truncation logic.
        row = self._conn.execute(
            "SELECT value FROM session_state WHERE key = 'clean_close'"
        ).fetchone()
        prior_count_row = self._conn.execute("SELECT COUNT(*) FROM journal").fetchone()
        prior_count = prior_count_row[0] if prior_count_row else 0

        if row is not None and prior_count > 0:
            self._conn.execute("DELETE FROM journal")

        self._conn.execute("DELETE FROM session_state WHERE key = 'clean_close'")

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
        with self._lock:
            sql = "SELECT * FROM journal WHERE sequence >= ? ORDER BY sequence"
            params: list[Any] = [start]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
        return [SqliteJournal._row_to_record(r) for r in rows]

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
    ) -> list[JournalRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM journal{where} ORDER BY sequence", params
            ).fetchall()
        return [SqliteJournal._row_to_record(r) for r in rows]

    def flush(self) -> None:
        if self._closed:
            return
        try:
            self._conn.sync()
        except Exception:
            logger.debug("libsql sync failed during flush", exc_info=True)

    def finalize(self) -> None:
        if self._closed:
            return
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO session_state (key, value) VALUES ('clean_close', '1')"
            )
            self._conn.commit()
        except Exception:
            logger.debug("libsql clean_close marker write failed", exc_info=True)
        try:
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
        except Exception:
            logger.debug("libsql final sync failed on close", exc_info=True)

        try:
            self._conn.close()
        except Exception:
            pass

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
            self._seq += 1
            seq = self._seq
            self._conn.execute(
                "INSERT INTO journal "
                "(sequence, session_id, kind, name, wall_ns, mono_ns, cpu_ns, "
                "turn_id, data, error_type, error_msg, error_tb, error_notes, "
                "input_ref, output_ref, tags) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    session_id,
                    kind.value,
                    name,
                    now_wall,
                    now_mono,
                    now_cpu,
                    turn_id,
                    json.dumps(data or {}, default=str),
                    error.type if error else None,
                    error.message if error else None,
                    error.traceback if error else None,
                    error.notes if error else None,
                    input_ref,
                    output_ref,
                    ",".join(sorted(tags)) if tags else "",
                ),
            )
            self._conn.commit()
        return seq

    def _sync_loop(self) -> None:
        """Background thread: periodically call ``conn.sync()``."""
        while not self._sync_stop.wait(timeout=self._sync_interval):
            try:
                self._conn.sync()
            except Exception:
                logger.debug("libsql periodic sync failed", exc_info=True)

    def _enter_degraded(self, session_id: str, exc: Exception) -> None:
        self._degraded = True
        observe_gauge("easycat.journal.degraded", 1)
        print(
            f"[easycat] journal degraded: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        logger.warning("Journal entered degraded mode: %s: %s", type(exc).__name__, exc)


# ── Retention ────────────────────────────────────────────────────


def run_retention(
    data_dir: str | Path,
    *,
    max_sessions: int = 50,
    max_bytes: int = 2 * 1024 * 1024 * 1024,  # 2 GB
    mode: Literal["archive", "delete"] = "archive",
) -> int:
    """Enforce retention policy on journal files.  Returns number removed.

    Runs opportunistically on session close — never blocks a turn.
    Keeps the most recent *max_sessions* journals **or** *max_bytes* total,
    whichever is tighter.
    """
    import shutil
    import tarfile

    root = Path(data_dir)
    journals_dir = root / "journals"
    if not journals_dir.is_dir():
        return 0

    # Gather journal files sorted oldest-first by mtime.
    files = sorted(journals_dir.glob("*.sqlite"), key=lambda p: p.stat().st_mtime)
    if not files:
        return 0

    artifacts_root = root / "artifacts"

    def _session_bytes(db_path: Path) -> int:
        """Total bytes for a session: DB + WAL/SHM sidecars + artifacts."""
        size = db_path.stat().st_size
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                size += sidecar.stat().st_size
        art_dir = artifacts_root / db_path.stem
        if art_dir.is_dir():
            size += sum(f.stat().st_size for f in art_dir.rglob("*") if f.is_file())
        return size

    total_bytes = sum(_session_bytes(f) for f in files)
    removed = 0

    while files and (len(files) > max_sessions or total_bytes > max_bytes):
        oldest = files.pop(0)
        fsize = _session_bytes(oldest)

        if mode == "archive":
            archive_dir = root / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / f"{oldest.stem}.tar.gz"
            try:
                # Checkpoint WAL so all data is in the main database file
                # before archiving — otherwise uncheckpointed pages are lost.
                conn = sqlite3.connect(str(oldest))
                try:
                    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                finally:
                    conn.close()

                checkpoint_incomplete = row is not None and row[1] != row[2]

                session_id = oldest.stem
                artifact_dir = root / "artifacts" / session_id
                with tarfile.open(str(archive_path), "w:gz") as tar:
                    tar.add(str(oldest), arcname=oldest.name)
                    if checkpoint_incomplete:
                        for suffix in ("-wal", "-shm"):
                            sidecar = Path(str(oldest) + suffix)
                            if sidecar.exists():
                                tar.add(str(sidecar), arcname=oldest.name + suffix)
                    if artifact_dir.is_dir():
                        tar.add(str(artifact_dir), arcname=f"artifacts/{session_id}")
            except OSError:
                logger.warning("Failed to archive %s", oldest, exc_info=True)
                continue

        try:
            oldest.unlink()
            # Also remove the WAL/SHM sidecars if present.
            for suffix in (".sqlite-wal", ".sqlite-shm"):
                sidecar = oldest.with_suffix(suffix)
                if sidecar.exists():
                    sidecar.unlink()
            # Remove corresponding artifacts.
            session_id = oldest.stem
            artifact_dir = root / "artifacts" / session_id
            if artifact_dir.is_dir():
                shutil.rmtree(str(artifact_dir), ignore_errors=True)
        except OSError:
            logger.warning("Failed to remove %s", oldest, exc_info=True)
            continue

        total_bytes -= fsize
        removed += 1

    return removed


# ── Factory ──────────────────────────────────────────────────────


def create_journal(
    session_id: str,
    *,
    debug: Literal["off", "light", "full"] = "light",
    backend: Literal["sqlite", "sqlite+litestream", "libsql"] = "sqlite",
    capacity: int = 10_000,
    data_dir: str | None = None,
    artifact_store: InMemoryArtifactStore | None = None,
    retention_mode: Literal["archive", "delete"] = "archive",
) -> InMemoryRingBuffer | SqliteJournal | LitestreamSqliteJournal | LibsqlJournal:
    """Create a journal backend based on the debug level and backend selection.

    - ``"off"``   — caller should not call this (returns in-memory as fallback)
    - ``"light"`` — in-memory ring buffer (ignores *backend*)
    - ``"full"``  — persistent backend selected by *backend*:
      - ``"sqlite"`` (default) — local SQLite WAL journal
      - ``"sqlite+litestream"`` — SQLite with Litestream WAL replication
      - ``"libsql"`` — libSQL embedded replica

    *artifact_store* is wired to the ``InMemoryRingBuffer`` so that
    artifacts referenced only by evicted records are cleaned up
    automatically.  Ignored for persistent backends (they use
    file-level retention instead).
    """
    if debug == "full":
        if backend == "sqlite+litestream":
            journal: SqliteJournal | LitestreamSqliteJournal | LibsqlJournal
            journal = LitestreamSqliteJournal(
                session_id,
                data_dir=data_dir,
                retention_mode=retention_mode,
            )
            logger.info(
                "Journal: session=%s backend=%s path=%s",
                session_id,
                backend,
                journal.db_path,
            )
            return journal

        if backend == "libsql":
            try:
                journal = LibsqlJournal(session_id, data_dir=data_dir)
                logger.info(
                    "Journal: session=%s backend=%s path=%s",
                    session_id,
                    backend,
                    journal.db_path,
                )
                return journal
            except ImportError:
                logger.warning(
                    "libsql_experimental SDK not installed; falling back to SqliteJournal"
                )

        journal = SqliteJournal(session_id, data_dir=data_dir, retention_mode=retention_mode)
        logger.info(
            "Journal: session=%s backend=%s path=%s",
            session_id,
            backend,
            journal.db_path,
        )
        return journal

    logger.info(
        "Journal: session=%s backend=in-memory capacity=%d",
        session_id,
        capacity,
    )
    return InMemoryRingBuffer(capacity=capacity, artifact_store=artifact_store)
