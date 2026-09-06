"""SQL-backed journal family: SQLite WAL, Litestream sidecar, and libSQL replica."""

from __future__ import annotations

import concurrent.futures
import heapq
import itertools
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final, Literal
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit

from easycat._numeric import is_finite_number
from easycat._observability import observe_gauge, record_histogram
from easycat._session_id import validate_persistent_session_id
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
from easycat.runtime._journal_lock import journal_file_claim
from easycat.runtime._private_files import (
    harden_sqlite_files,
    mkdir_private,
    touch_private_file,
)
from easycat.runtime.artifacts import FilesystemArtifactStore
from easycat.runtime.crash_sweep import (
    _copy_journal_to_crash_dump,
    _has_live_pid,
    discard_crash_dump,
    reserve_crash_dump_paths,
    self_birth_identity,
    snapshot_crash_dump_artifacts,
    sweep_crashed_journals,
)
from easycat.runtime.journal import _validate_read_limit
from easycat.runtime.journal_retention import run_retention
from easycat.runtime.records import (
    ErrorInfo,
    JournalRecord,
    JournalRecordKind,
    TimingInfo,
)
from easycat.teardown_budgets import (
    JOURNAL_LIBSQL_SYNC_THREAD_JOIN_TIMEOUT_S,
    JOURNAL_LITESTREAM_KILL_TIMEOUT_S,
    JOURNAL_LITESTREAM_STDERR_JOIN_TIMEOUT_S,
    JOURNAL_LITESTREAM_TERMINATE_TIMEOUT_S,
)
from easycat.validation.redaction import RedactionPolicy, validate_redaction_policy

logger = logging.getLogger(__name__)

# SqliteJournal and LibsqlJournal both write a local SQLite-format replica.
# Keep one process-local claim registry so backend choice cannot bypass the
# sequence-writer exclusivity invariant for a shared path.
_LIVE_JOURNALS_LOCK = threading.Lock()
_LIVE_JOURNALS: weakref.WeakValueDictionary[tuple[int, Path], Any] = weakref.WeakValueDictionary()
# A libSQL SDK close can fail or outlive the bounded public close call. Keep
# those connections strongly owned at process scope until the SDK confirms
# physical closure; the ordinary live-writer registry intentionally remains
# weak so abandoned synchronous SQLite journals retain their recovery behavior.
_PENDING_LIBSQL_CLOSES: dict[tuple[int, Path], Any] = {}
_CRASH_SWEEP_INTERVAL_SECONDS = 60.0
_CRASH_SWEEP_RETRY_SECONDS = 1.0
_CRASH_SWEEP_MAX_ROOTS = 128
_CRASH_SWEEP_STATE_LOCK = threading.Lock()


def _begin_filesystem_artifact_epoch(root: Path, session_id: str, conn: Any) -> None:
    """Rotate managed artifact ownership after journal recovery completes."""
    try:
        rows = conn.execute(
            "SELECT input_ref AS ref FROM journal WHERE input_ref IS NOT NULL "
            "UNION SELECT output_ref AS ref FROM journal WHERE output_ref IS NOT NULL"
        ).fetchall()
        referenced_refs = {row[0] for row in rows if isinstance(row[0], str)}
        store = FilesystemArtifactStore(session_id, data_dir=root)
        try:
            store.begin_journal_epoch(referenced_refs)
        finally:
            store.close()
    except Exception:
        # Artifact capture is best-effort and must not prevent the journal from
        # starting. Managed markers remain conservative on failure, so a later
        # epoch can retry without blanket deletion.
        logger.warning(
            "Artifact journal-epoch startup failed for session %s",
            session_id,
            exc_info=True,
        )


@dataclass
class _CrashSweepState:
    lock: threading.Lock
    last_success: float | None = None
    timer: threading.Timer | None = None


_CRASH_SWEEP_STATES: OrderedDict[Path, _CrashSweepState] = OrderedDict()


def _crash_sweep_key(root: Path) -> Path:
    return root.absolute()


def _reset_crash_sweep_state_after_fork() -> None:
    global _CRASH_SWEEP_STATE_LOCK, _CRASH_SWEEP_STATES
    _CRASH_SWEEP_STATE_LOCK = threading.Lock()
    _CRASH_SWEEP_STATES = OrderedDict()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_crash_sweep_state_after_fork)


def _clear_crash_sweep_states() -> None:
    """Cancel scheduled scans and clear process-local coordination state."""
    with _CRASH_SWEEP_STATE_LOCK:
        states = list(_CRASH_SWEEP_STATES.values())
        _CRASH_SWEEP_STATES.clear()
    for state in states:
        if state.timer is not None:
            state.timer.cancel()


def _crash_sweep_state(root_key: Path) -> _CrashSweepState:
    with _CRASH_SWEEP_STATE_LOCK:
        state = _CRASH_SWEEP_STATES.get(root_key)
        if state is None:
            state = _CrashSweepState(lock=threading.Lock())
            _CRASH_SWEEP_STATES[root_key] = state
        else:
            _CRASH_SWEEP_STATES.move_to_end(root_key)

        while len(_CRASH_SWEEP_STATES) > _CRASH_SWEEP_MAX_ROOTS:
            old_key, old_state = next(iter(_CRASH_SWEEP_STATES.items()))
            if old_state.timer is not None:
                old_state.timer.cancel()
            del _CRASH_SWEEP_STATES[old_key]
        return state


def _schedule_crash_sweep(root_key: Path, state: _CrashSweepState, delay: float) -> None:
    if state.timer is not None:
        return
    timer = threading.Timer(
        max(0.0, delay),
        _run_scheduled_crash_sweep,
        args=(root_key, state),
    )
    timer.daemon = True
    state.timer = timer
    timer.start()


def _run_scheduled_crash_sweep(root_key: Path, state: _CrashSweepState) -> None:
    with _CRASH_SWEEP_STATE_LOCK:
        if _CRASH_SWEEP_STATES.get(root_key) is not state:
            return
    with state.lock:
        state.timer = None
        _run_crash_sweep(root_key, state, skip=None)


def _run_crash_sweep(root: Path, state: _CrashSweepState, *, skip: Path | None) -> None:
    try:
        sweep_crashed_journals(root, skip=skip)
    except (OSError, sqlite3.DatabaseError):
        logger.debug("Crash-journal sweep failed", exc_info=True)
        _schedule_crash_sweep(root, state, _CRASH_SWEEP_RETRY_SECONDS)
        return

    state.last_success = time.monotonic()
    _schedule_crash_sweep(root, state, _CRASH_SWEEP_INTERVAL_SECONDS)


def _sweep_crashed_journals_if_due(root: Path, *, skip: Path) -> None:
    """Periodically scan *root* without putting an O(n) scan on every open."""
    root_key = _crash_sweep_key(root)
    state = _crash_sweep_state(root_key)
    with state.lock:
        now = time.monotonic()
        elapsed = None if state.last_success is None else now - state.last_success
        if elapsed is not None and 0 <= elapsed < _CRASH_SWEEP_INTERVAL_SECONDS:
            _schedule_crash_sweep(
                root_key,
                state,
                _CRASH_SWEEP_INTERVAL_SECONDS - elapsed,
            )
            return
        if state.timer is not None:
            state.timer.cancel()
            state.timer = None
        _run_crash_sweep(root_key, state, skip=skip)


class _SqliteBatchCommitCoordinator:
    """Schedule elapsed-time SQLite batch commits with bounded isolation.

    One coordinator maintains the deadline heap while a small worker pool runs
    due commits. A stalled filesystem operation can occupy one worker without
    delaying every other journal. Heap entries carry a journal generation, so
    stale deadlines become cheap no-ops after a count/turn/lifecycle boundary
    commits the transaction first.
    """

    _condition = threading.Condition()
    _deadlines: ClassVar[list[tuple[float, int, weakref.ReferenceType[Any], int]]] = []
    _counter = itertools.count()
    _thread: threading.Thread | None = None
    _executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=4,
        thread_name_prefix="easycat-sqlite-journal-commit",
    )

    @classmethod
    def schedule(cls, journal: SqliteJournal, deadline: float, generation: int) -> None:
        with cls._condition:
            heapq.heappush(
                cls._deadlines,
                (deadline, next(cls._counter), weakref.ref(journal), generation),
            )
            if cls._thread is None:
                cls._thread = threading.Thread(
                    target=cls._run,
                    daemon=True,
                    name="easycat-sqlite-journal-commit",
                )
                cls._thread.start()
            cls._condition.notify()

    @classmethod
    def _run(cls) -> None:
        while True:
            with cls._condition:
                while True:
                    if not cls._deadlines:
                        cls._thread = None
                        return
                    deadline, _order, journal_ref, generation = cls._deadlines[0]
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        cls._condition.wait(timeout=remaining)
                        continue
                    heapq.heappop(cls._deadlines)
                    break
            journal = journal_ref()
            if journal is not None:
                cls._executor.submit(journal._commit_scheduled_batch, generation)


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
            # ``close()`` can begin after the inexpensive check above but
            # before a concurrent writer acquires its connection lock.  The
            # connection-specific writer rechecks its state under that lock
            # and returns this normal drop sentinel rather than attempting a
            # write against a closed connection (which must not degrade a
            # cleanly shut-down journal).
            if sequence < 0:
                return -1
            result = "pass"
            return sequence
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
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
    - Appends share a transaction for at most 100 ms or 100 records; turn
      boundaries and lifecycle flushes commit immediately.
    - ``PRAGMA wal_autocheckpoint=1000`` — committed WAL pages are folded
      back into the database throughout long-running sessions.
    - Single-writer discipline via ``threading.Lock``.
    - Eager file-open warmup so the first turn doesn't pay cold-PRAGMA cost.
    """

    writes_block = True
    _batch_commit_interval_s = 0.1
    _batch_commit_records = 100
    _wal_autocheckpoint_pages = 1000
    _commit_boundary_names = frozenset({"turn_started", "turn_ended"})

    def __init__(
        self,
        session_id: str,
        *,
        data_dir: str | Path | None = None,
        retention_mode: Literal["archive", "delete"] = "archive",
        redaction: RedactionPolicy = "secrets",
    ) -> None:
        validate_persistent_session_id(session_id)
        self._redaction = validate_redaction_policy(redaction)
        root = Path(data_dir) if data_dir else Path(os.environ.get("EASYCAT_DATA_DIR", ".easycat"))
        self._root = root
        self._retention_mode = retention_mode
        journals_dir = root / "journals"
        mkdir_private(journals_dir)
        self._db_path = journals_dir / f"{session_id}.sqlite"
        self._session_id = session_id
        self._lock = threading.Lock()
        self._seq = 0
        self._degraded = False
        self._closed = False
        self._closing = False
        self._recovered = False
        self._original_session_id = session_id
        self._clean_close_marked = False
        self._degraded_marker_safe = True
        self._pending_records = 0
        self._batch_generation = 0
        self._batch_deadline: float | None = None

        # The claim is deliberately acquired before a session opens its
        # SQLite connection and held until ``live_pid`` is committed below.
        # A crash/retention sweep uses the same claim for final revalidation
        # and removal, preventing it from unlinking a just-opened database.
        with journal_file_claim(self._db_path, blocking=True) as claimed:
            assert claimed
            # Sweep crashed-but-unswept prior journals (different session ids
            # whose process died without a clean close) before we open our own
            # file. The same-id recovery path below only fires when *this*
            # session's id is reused; orphaned ids never reopen, so the sweep
            # is what promotes them to crash-dumps/. Best-effort: never block
            # or fail journal startup.
            _sweep_crashed_journals_if_due(root, skip=self._db_path)

            touch_private_file(self._db_path)

            # ── Check for prior unclean shutdown ─────────────────
            existed = self._db_path.exists()

            # Eager warmup — open DB and apply PRAGMAs now.
            self._conn = self._open_connection()
            self._conn.executescript(_SQLITE_SCHEMA)
            _ensure_journal_schema(self._conn)

            try:
                self._claim_live_journal()
                self._initialize_live_journal(session_id, existed=existed)
                _begin_filesystem_artifact_epoch(root, session_id, self._conn)
            except BaseException:
                self._release_live_journal()
                self._conn.close()
                raise

    # ── Startup phases ────────────────────────────────────────────

    def _claim_live_journal(self) -> None:
        """Reject a second live writer for this process/path identity."""
        key = (os.getpid(), self._db_path.absolute())
        with _LIVE_JOURNALS_LOCK:
            pending_libsql = _PENDING_LIBSQL_CLOSES.get(key)
            current = _LIVE_JOURNALS.get(key)
            # ``close()`` closes admission before it acquires the connection
            # lock. Keep the path claim exclusive until teardown releases the
            # registry entry; otherwise a replacement can open beside the
            # still-live connection and inherit a stale transaction snapshot.
            # ``_closed`` without ``_closing`` represents an abandoned/crashed
            # connection and remains eligible for the existing recovery path.
            if pending_libsql is not None or (
                current is not None
                and (not current._closed or bool(getattr(current, "_closing", False)))
            ):
                raise RuntimeError(f"Journal is already active: {self._db_path}")

            row = self._conn.execute(
                "SELECT value FROM session_state WHERE key = 'live_pid'"
            ).fetchone()
            if row is not None and row[0] not in (None, ""):
                try:
                    live_pid = int(row[0])
                except (TypeError, ValueError):
                    live_pid = 0
                if live_pid != os.getpid() and _has_live_pid(self._conn):
                    raise RuntimeError(f"Journal is active in process {live_pid}: {self._db_path}")
            _LIVE_JOURNALS[key] = self

    def _release_live_journal(self) -> None:
        key = (os.getpid(), self._db_path.absolute())
        with _LIVE_JOURNALS_LOCK:
            if _LIVE_JOURNALS.get(key) is self:
                _LIVE_JOURNALS.pop(key, None)

    def _initialize_live_journal(self, session_id: str, *, existed: bool) -> None:
        prior_count = self._reconcile_prior_session(session_id) if existed else 0
        # After reconcile the live table is empty (prior rows were promoted
        # or truncated), so the pre-v2 backfill only stamps the version.
        _ensure_index_backfill(self._conn)

        # Clear prior-session state markers (we're starting a new session).
        self._conn.execute("DELETE FROM session_state WHERE key IN ('clean_close', 'degraded')")

        # Stamp our PID and process-birth identity as liveness markers
        # (committed so a separate crash-sweep connection can read them).
        # An idle WAL journal between turns holds no write lock, so the
        # orphan sweep cannot tell "live but idle" from "crashed" by lock
        # alone. The birth identity distinguishes this process from a later
        # process that reuses its PID, even after a reboot.
        self._restore_live_owner_marker()
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
            self._pending_records = 1
            self._schedule_batch_commit_locked()

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit for PRAGMAs
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA wal_autocheckpoint={self._wal_autocheckpoint_pages}")
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

        # Copy rather than move so we can keep writing to the current path.
        # Hold the lock across the close→copy→reopen sequence so no
        # concurrent append() can use the connection while it's closed.
        with self._lock:
            try:
                crash_path, artifact_root = reserve_crash_dump_paths(self._root, session_id)
                # Close our live connection so the shared file-level promoter
                # can checkpoint+copy the on-disk database (the same core the
                # orphan sweep uses), then reopen.  Checkpointing folds any
                # WAL-only pages into the main DB before the byte copy.
                self._conn.close()
                try:
                    _copy_journal_to_crash_dump(self._db_path, crash_path)
                    if not snapshot_crash_dump_artifacts(
                        self._root,
                        self._db_path,
                        artifact_root,
                    ):
                        discard_crash_dump(crash_path, artifact_root)
                        self._conn = self._open_connection()
                        raise RuntimeError(
                            "Crash artifact snapshot was incomplete; "
                            "refusing to truncate the source journal"
                        )
                except (OSError, sqlite3.Error):
                    discard_crash_dump(crash_path, artifact_root)
                    raise
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
        self._closing = True
        self._closed = True
        try:
            with self._lock:
                try:
                    try:
                        self._commit_transaction_locked(reopen=False)
                    except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                        pass  # no active transaction or already closed
                    self._harden_files_on_close()
                    try:
                        self._conn.execute(
                            "INSERT OR REPLACE INTO session_state "
                            "(key, value) VALUES ('clean_close', '1')"
                        )
                        # Drop the liveness marker: the process is shutting down, so
                        # the journal is no longer "live" for the crash sweep.
                        self._conn.execute(
                            "DELETE FROM session_state WHERE key IN ('live_pid', 'live_pid_start')"
                        )
                        self._clean_close_marked = True
                    except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                        pass
                    try:
                        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                        logger.debug("WAL checkpoint skipped on close", exc_info=True)
                    self._harden_files_on_close()
                finally:
                    try:
                        self._conn.close()
                    except sqlite3.Error:
                        logger.debug("SQLite connection close failed", exc_info=True)
        finally:
            # A permission-hardening failure is diagnostic, not permission to
            # leave an open connection and an unretryable process-local claim.
            self._release_live_journal()
        # Run retention opportunistically — never block a turn.
        try:
            run_retention(self._root, mode=self._retention_mode, skip=self._db_path)
        except Exception:
            logger.debug("Retention sweep failed", exc_info=True)

    def _harden_files_on_close(self) -> None:
        try:
            harden_sqlite_files(self._db_path)
        except OSError:
            logger.warning("Failed to harden SQLite journal files during close", exc_info=True)

    def flush(self) -> None:
        """Commit the current transaction and start a new one."""
        if self._closed:
            return
        with self._lock:
            try:
                self._commit_transaction_locked()
                harden_sqlite_files(self._db_path)
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
                self._commit_transaction_locked(reopen=False)
                harden_sqlite_files(self._db_path)
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                pass
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO session_state (key, value) VALUES ('clean_close', '1')"
                )
                self._conn.execute(
                    "DELETE FROM session_state WHERE key IN ('live_pid', 'live_pid_start')"
                )
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
                self._reset_batch_state_locked()
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
            if self._closed or self._degraded:
                return -1
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
                    redaction=self._redaction,
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
            self._finish_append_locked(name, post_finalize=clear_clean_close)
        return seq

    def _finish_append_locked(self, name: str, *, post_finalize: bool) -> None:
        if post_finalize:
            self._conn.execute("RELEASE SAVEPOINT post_finalize_append")
            self._clean_close_marked = False
        self._pending_records += 1
        if post_finalize:
            # Schedule deadline so the single post-finalize record becomes visible (gh 1035).
            self._schedule_batch_commit_locked()
            return
        if (
            name in self._commit_boundary_names
            or self._pending_records >= self._batch_commit_records
        ):
            self._commit_transaction_locked(recover_failed_batch=True)
            return
        self._schedule_batch_commit_locked()

    def _schedule_batch_commit_locked(self) -> None:
        if self._batch_deadline is not None or self._closed:
            return
        self._batch_generation += 1
        generation = self._batch_generation
        deadline = time.monotonic() + self._batch_commit_interval_s
        self._batch_deadline = deadline
        _SqliteBatchCommitCoordinator.schedule(self, deadline, generation)

    def _commit_scheduled_batch(self, generation: int) -> None:
        """Commit a still-current elapsed-time batch on the coordinator thread."""
        exc: Exception | None = None
        with self._lock:
            if (
                self._closed
                or self._degraded
                or generation != self._batch_generation
                or self._batch_deadline is None
                or self._pending_records == 0
            ):
                return
            try:
                self._commit_transaction_locked(recover_failed_batch=True)
            except Exception as commit_exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
                exc = commit_exc
        if exc is not None:
            self._enter_degraded(self._session_id, exc)

    def _commit_transaction_locked(
        self,
        *,
        reopen: bool = True,
        recover_failed_batch: bool = False,
    ) -> None:
        """Commit the open transaction and invalidate any scheduled deadline."""
        try:
            self._execute_commit_locked()
        except Exception:
            if recover_failed_batch:
                self._recover_failed_batch_commit_locked(reopen=reopen)
            raise
        self._reset_batch_state_locked()
        if reopen:
            self._conn.execute("BEGIN")

    def _execute_commit_locked(self) -> None:
        """Execute COMMIT through a narrow seam used by failure-path tests."""
        self._conn.execute("COMMIT")

    def _recover_failed_batch_commit_locked(self, *, reopen: bool) -> None:
        """Rollback a failed batch before degraded-marker persistence."""
        rollback_succeeded = False
        try:
            self._conn.execute("ROLLBACK")
            rollback_succeeded = True
        except sqlite3.Error:
            logger.debug("Failed to roll back journal batch after COMMIT error", exc_info=True)
        self._reset_batch_state_locked()
        self._degraded_marker_safe = rollback_succeeded
        if not rollback_succeeded:
            return
        try:
            row = self._conn.execute(
                "SELECT MAX(sequence) FROM journal WHERE sequence >= 0"
            ).fetchone()
            self._seq = int(row[0]) if row and row[0] is not None else 0
            if reopen:
                self._conn.execute("BEGIN")
        except sqlite3.Error:
            self._degraded_marker_safe = False
            logger.debug("Failed to restore journal after batch rollback", exc_info=True)

    def _reset_batch_state_locked(self) -> None:
        self._pending_records = 0
        self._batch_deadline = None
        self._batch_generation += 1

    def _clear_clean_close_marker_before_write(self) -> None:
        self._conn.execute("DELETE FROM session_state WHERE key = 'clean_close'")
        # finalize() removes the durable owner marker. A successful
        # post-finalize append reopens the journal, so restore the marker in
        # the same savepoint as the marker deletion and record insert. If the
        # append fails, rolling the savepoint back retains clean-close state.
        self._restore_live_owner_marker()

    def _restore_live_owner_marker(self) -> None:
        """Mark this connection's process as the durable journal owner."""
        self._conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
            (str(os.getpid()),),
        )
        process_birth = self_birth_identity()
        if process_birth is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid_start', ?)",
                (process_birth,),
            )
        else:
            self._conn.execute("DELETE FROM session_state WHERE key = 'live_pid_start'")

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
        if self._clean_close_marked or not self._degraded_marker_safe:
            return
        with self._lock:
            _persist_degraded_marker(self._conn, session_id, exc)


# ── Litestream adapter ──────────────────────────────────────────


def _sanitize_replica_url(url: str) -> str:
    """Return ``scheme://host`` from a replica URL, stripping path and credentials."""
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.hostname or ''}"
    except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
        return "<unparseable>"


# Litestream reads a replica credential from the environment when the URL and
# config file do not carry one.  Both S3 families are set together: litestream
# ranks ``AWS_*`` above ``LITESTREAM_*``, so an ambient ``AWS_ACCESS_KEY_ID``
# inherited from the parent would otherwise outrank the credentials the URL
# carried.
_S3_CREDENTIAL_ENV_VARS: Final = (
    ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
    ("LITESTREAM_ACCESS_KEY_ID", "LITESTREAM_SECRET_ACCESS_KEY"),
)


def _split_replica_credentials(url: str) -> tuple[str, dict[str, str]]:
    """Split a replica URL into an argv-safe URL and sidecar credential env vars.

    A process's command line is world-readable for its whole lifetime — ``ps``,
    ``/proc/<pid>/cmdline`` — so litestream's documented
    ``scheme://KEY:SECRET@host/path`` form must not reach argv.
    ``/proc/<pid>/environ`` is readable only by the process owner, so the
    environment is the safe hand-off, and it is the mechanism litestream
    documents (gh 1068).

    Only schemes with a documented credential environment variable are
    rewritten.  ``sftp`` has none, so its URL is returned unchanged and the
    caller warns rather than silently breaking replication.  For ``abs`` only
    the account *key* moves: the account name is not a secret and litestream
    still needs it in the URL.
    """
    parsed = urlsplit(url)
    if not parsed.password:
        return url, {}
    userinfo, _, host = parsed.netloc.rpartition("@")
    raw_user, _, raw_password = userinfo.partition(":")
    password = unquote(raw_password)
    scheme = parsed.scheme.lower()
    if scheme == "s3":
        access_key = unquote(raw_user)
        env = {
            var: value
            for key_var, secret_var in _S3_CREDENTIAL_ENV_VARS
            for var, value in ((key_var, access_key), (secret_var, password))
        }
        netloc = host
    elif scheme == "abs":
        env = {"LITESTREAM_AZURE_ACCOUNT_KEY": password}
        netloc = f"{raw_user}@{host}"
    else:
        return url, {}
    # ``host`` is sliced from the original netloc, so its case and port survive
    # verbatim (``urlsplit().hostname`` would lowercase them).
    return urlunsplit(parsed._replace(netloc=netloc)), env


def _session_replica_url(base_url: str, session_id: str) -> str:
    """Namespace a replica root by session without disturbing URL options."""
    parsed = urlsplit(base_url)
    session_path = f"{quote(session_id, safe='')}.sqlite"
    path = f"{parsed.path.rstrip('/')}/{session_path}"
    return urlunsplit(parsed._replace(path=path))


class LitestreamSqliteJournal:
    """SqliteJournal with a Litestream sidecar for WAL replication.

    Delegates all journal operations to an inner ``SqliteJournal``.  On
    construction, starts ``litestream replicate`` pointing at the SQLite
    DB file.  If the ``litestream`` binary is not on ``$PATH``, logs a
    warning and degrades to plain ``SqliteJournal`` (no crash).
    """

    writes_block = True

    def __init__(
        self,
        session_id: str,
        *,
        data_dir: str | Path | None = None,
        replica_url: str | None = None,
        retention_mode: Literal["archive", "delete"] = "archive",
        redaction: RedactionPolicy = "secrets",
    ) -> None:
        self._inner = SqliteJournal(
            session_id,
            data_dir=data_dir,
            retention_mode=retention_mode,
            redaction=redaction,
        )
        replica_root = replica_url or os.environ.get("EASYCAT_JOURNAL_LITESTREAM_REPLICA", "")
        self._replica_url = _session_replica_url(replica_root, session_id) if replica_root else ""
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
        argv_url, credential_env = _split_replica_credentials(self._replica_url)
        sidecar_env: dict[str, str] | None = None
        if credential_env:
            if any("\x00" in value for value in credential_env.values()):
                # A ``%00`` in the URL decodes to a NUL, which no environment
                # value may contain: ``Popen`` would raise ``ValueError`` and
                # take journal construction down with it. Such a credential
                # cannot authenticate anyway, so name it and degrade the same
                # way a missing binary does.
                logger.warning(
                    "LitestreamSqliteJournal: replica credential contains a NUL byte "
                    "(a percent-encoded %%00); it cannot be passed to the sidecar. "
                    "Degrading to plain SqliteJournal"
                )
                self._litestream_available = False
                return
            sidecar_env = {**os.environ, **credential_env}
        elif urlsplit(self._replica_url).password:
            logger.warning(
                "LitestreamSqliteJournal: the %s replica URL embeds a password and "
                "litestream documents no credential environment variable for that "
                "scheme, so it stays on the sidecar's command line where any local "
                "user can read it via `ps`. Prefer a credential-free URL plus "
                "litestream's own environment contract, or an external litestream "
                "sidecar with a config file.",
                urlsplit(self._replica_url).scheme or "replica",
            )
        try:
            self._sidecar = subprocess.Popen(
                [
                    litestream_bin,
                    "replicate",
                    str(self._inner.db_path),
                    argv_url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=sidecar_env,
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
        except (OSError, ValueError) as exc:
            # ``ValueError`` covers ``Popen`` rejecting an argument or an
            # environment value outright; a journal must degrade rather than
            # fail construction for a replica-target problem.
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

    def slice_by_stage(self, stage_name: str) -> list[JournalRecord]:
        # Delegate so ``JournalView.filter_by_stage`` finds the indexed lookup
        # instead of falling back to deserializing every record (gh 1026).
        return self._inner.slice_by_stage(stage_name)

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
            self._sidecar.wait(timeout=JOURNAL_LITESTREAM_TERMINATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._sidecar.kill()
            self._sidecar.wait(timeout=JOURNAL_LITESTREAM_KILL_TIMEOUT_S)
        except OSError:
            pass
        finally:
            # The drain thread closes the pipe on EOF; join it so the fd is
            # released before we drop our reference to the process.
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=JOURNAL_LITESTREAM_STDERR_JOIN_TIMEOUT_S)
                self._stderr_thread = None
            if self._sidecar.stderr is not None:
                try:
                    self._sidecar.stderr.close()
                except OSError:
                    pass
            self._sidecar = None


# ── libSQL adapter ──────────────────────────────────────────────


def _resolve_libsql_sync_interval(sync_interval_s: object | None) -> float:
    """Return a finite positive sync interval from config or the environment."""
    value = sync_interval_s
    if value is None:
        raw = os.environ.get("EASYCAT_JOURNAL_LIBSQL_SYNC_INTERVAL_S", "10")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("sync_interval_s must be a finite positive number") from exc
    if not is_finite_number(value) or value <= 0:
        raise ValueError("sync_interval_s must be a finite positive number")
    return float(value)


class LibsqlJournal(_SqlJournalBase):
    """Journal backend using the libSQL embedded-replica SDK.

    Reads are local; appends commit locally and sync to the remote
    primary asynchronously every ``sync_interval_s`` seconds (default 10,
    configurable via ``EASYCAT_JOURNAL_LIBSQL_SYNC_INTERVAL_S``).

    If the ``libsql_experimental`` SDK is not installed, logs a warning
    and raises ``ImportError`` — the factory catches this and falls back
    to ``SqliteJournal``.
    """

    writes_block = True

    def __init__(
        self,
        session_id: str,
        *,
        data_dir: str | Path | None = None,
        sync_url: str | None = None,
        auth_token: str | None = None,
        sync_interval_s: float | None = None,
        redaction: RedactionPolicy = "secrets",
    ) -> None:
        validate_persistent_session_id(session_id)
        self._redaction = validate_redaction_policy(redaction)
        import libsql_experimental as libsql

        self._libsql = libsql
        self._sync_interval = _resolve_libsql_sync_interval(sync_interval_s)

        root = Path(data_dir) if data_dir else Path(os.environ.get("EASYCAT_DATA_DIR", ".easycat"))
        journals_dir = root / "journals"
        mkdir_private(journals_dir)
        self._db_path = journals_dir / f"{session_id}.sqlite"
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._degraded = False
        self._closed = False
        self._connection_closed = False
        self._finalize_requested = False
        self._finalize_thread: threading.Thread | None = None
        self._close_thread: threading.Thread | None = None

        url = sync_url or os.environ.get("EASYCAT_LIBSQL_URL", "")
        token = auth_token or os.environ.get("EASYCAT_LIBSQL_AUTH_TOKEN", "")

        connect_kwargs: dict[str, Any] = {"uri": str(self._db_path)}
        if url:
            connect_kwargs["sync_url"] = url
        if token:
            connect_kwargs["auth_token"] = token

        # A prior Session may already have been collected after swapping its
        # live journal for a read-only postmortem view. Service that retained
        # writer's bounded close retry before opening another SDK connection,
        # but reject this admission attempt because the path was still owned
        # when it began. A later attempt can proceed once physical close has
        # removed the process-global pending claim.
        if self._retry_pending_close_for_path():
            raise RuntimeError(f"Journal is already active: {self._db_path}")

        # Serialize startup until the durable live-owner marker is committed.
        # The local replica has the same sequence primary key as SqliteJournal,
        # so two live writers would otherwise begin from the same ``MAX`` and
        # permanently degrade the loser on its first duplicate insert.
        with journal_file_claim(self._db_path, blocking=True) as claimed:
            assert claimed
            touch_private_file(self._db_path)
            self._conn = libsql.connect(**connect_kwargs)
            harden_sqlite_files(self._db_path)
            try:
                self._initialize_live_replica(session_id)
                _begin_filesystem_artifact_epoch(root, session_id, self._conn)
            except BaseException:
                self._release_live_journal()
                self._conn.close()
                raise

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

    def _initialize_live_replica(self, session_id: str) -> None:
        self._conn.executescript(_SQLITE_SCHEMA)
        _ensure_journal_schema(self._conn)
        self._claim_live_journal()

        # Handle session-id reuse: mirror only SqliteJournal's *clean-reuse*
        # truncation. libSQL does NOT implement crash recovery — there is no
        # crash-dump promotion, no RecoveredSessionMarker, and no _recovered
        # flag. An unclean reuse continues appending into the prior table with
        # a continued sequence counter. This divergence from the SqliteJournal
        # contract is documented in DURABILITY.md ("Backend support").
        row = self._conn.execute(
            "SELECT value FROM session_state WHERE key = 'clean_close'"
        ).fetchone()
        prior_count_row = self._conn.execute("SELECT COUNT(*) FROM journal").fetchone()
        prior_count = prior_count_row[0] if prior_count_row else 0

        truncated = row is not None and prior_count > 0
        if truncated:
            # Clean reuse — the prior (cleanly closed) journal is discarded,
            # so its persisted ``degraded`` marker would be stale. Clear both
            # the ``clean_close`` and ``degraded`` keys alongside truncation.
            self._conn.execute("DELETE FROM journal")
            self._conn.execute("DELETE FROM journal_tags")
            self._conn.execute(
                "DELETE FROM session_state WHERE key IN ('clean_close', 'degraded')"
            )
        else:
            # Unclean reuse — prior rows are retained (libSQL has no crash
            # recovery), including any ``JournalDegraded`` row. Only clear the
            # ``clean_close`` marker; preserve ``degraded`` so file/bundle
            # inspection stays consistent with the retained history.
            self._conn.execute("DELETE FROM session_state WHERE key = 'clean_close'")

        # Unclean reuse retains prior rows, so pre-v2 files must be backfilled
        # here (post-truncation) for stage/tag queries to see them.
        _ensure_index_backfill(self._conn)

        # Recover sequence counter from any remaining records.
        row = self._conn.execute("SELECT MAX(sequence) FROM journal").fetchone()
        self._seq = row[0] if row and row[0] is not None else 0
        self._restore_live_owner_marker()
        self._conn.commit()

    def _claim_live_journal(self) -> None:
        """Reject a second live writer for this local replica path."""
        key = (os.getpid(), self._db_path.absolute())
        with _LIVE_JOURNALS_LOCK:
            current = _PENDING_LIBSQL_CLOSES.get(key) or _LIVE_JOURNALS.get(key)
            # ``close()`` marks the instance closed before it removes the
            # persisted owner marker. Keep the in-process claim exclusive
            # until teardown reaches ``_release_live_journal()``.
            if current is not None:
                raise RuntimeError(f"Journal is already active: {self._db_path}")

            row = self._conn.execute(
                "SELECT value FROM session_state WHERE key = 'live_pid'"
            ).fetchone()
            if row is not None and row[0] not in (None, ""):
                try:
                    live_pid = int(row[0])
                except (TypeError, ValueError):
                    live_pid = 0
                if live_pid != os.getpid() and _has_live_pid(self._conn):
                    raise RuntimeError(f"Journal is active in process {live_pid}: {self._db_path}")
            _LIVE_JOURNALS[key] = self

    def _retry_pending_close_for_path(self) -> bool:
        """Retry a retained predecessor without opening a replacement connection."""
        key = (os.getpid(), self._db_path.absolute())
        with _LIVE_JOURNALS_LOCK:
            pending = _PENDING_LIBSQL_CLOSES.get(key)
        if pending is None:
            return False
        try:
            pending.close()
        except Exception:
            logger.warning("Pending libSQL close retry failed", exc_info=True)
        return True

    def _retain_pending_close(self) -> None:
        """Keep this journal alive until its SDK connection physically closes."""
        key = (os.getpid(), self._db_path.absolute())
        with _LIVE_JOURNALS_LOCK:
            pending = _PENDING_LIBSQL_CLOSES.get(key)
            if pending is not None and pending is not self:
                raise RuntimeError(f"Journal is already active: {self._db_path}")
            _PENDING_LIBSQL_CLOSES[key] = self

    def _release_live_journal(self) -> None:
        key = (os.getpid(), self._db_path.absolute())
        with _LIVE_JOURNALS_LOCK:
            if _LIVE_JOURNALS.get(key) is self:
                _LIVE_JOURNALS.pop(key, None)
            if _PENDING_LIBSQL_CLOSES.get(key) is self:
                _PENDING_LIBSQL_CLOSES.pop(key, None)

    def _restore_live_owner_marker(self) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
            (str(os.getpid()),),
        )
        process_birth = self_birth_identity()
        if process_birth is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid_start', ?)",
                (process_birth,),
            )
        else:
            self._conn.execute("DELETE FROM session_state WHERE key = 'live_pid_start'")

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
        with self._lifecycle_lock:
            if self._closed:
                return
            self._finalize_requested = True
            worker = self._finalize_thread
            if worker is None or not worker.is_alive():
                worker = threading.Thread(
                    target=self._finalize_in_background,
                    daemon=True,
                    name="libsql-finalize",
                )
                self._finalize_thread = worker
                worker.start()
        worker.join(timeout=JOURNAL_LIBSQL_SYNC_THREAD_JOIN_TIMEOUT_S)
        if worker.is_alive():
            logger.warning(
                "libSQL finalize exceeded %.1fs; cleanup remains runtime-owned",
                JOURNAL_LIBSQL_SYNC_THREAD_JOIN_TIMEOUT_S,
            )

    def _finalize_in_background(self) -> None:
        with self._lock:
            if self._connection_closed:
                return
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO session_state (key, value) VALUES ('clean_close', '1')"
                )
                self._conn.commit()
                harden_sqlite_files(self._db_path)
            except Exception:
                logger.debug("libsql clean_close marker write failed", exc_info=True)
            try:
                self._conn.sync()
            except Exception:
                logger.debug("libsql sync failed during finalize", exc_info=True)

    def close(self) -> None:
        with self._lifecycle_lock:
            if not self._connection_closed:
                self._retain_pending_close()
            worker = self._close_thread
            if worker is None or (not worker.is_alive() and not self._connection_closed):
                self._closed = True
                self._sync_stop.set()
                worker = threading.Thread(
                    target=self._close_in_background,
                    daemon=True,
                    name="libsql-close",
                )
                self._close_thread = worker
                worker.start()
        worker.join(timeout=JOURNAL_LIBSQL_SYNC_THREAD_JOIN_TIMEOUT_S)
        if worker.is_alive():
            logger.warning(
                "libSQL close exceeded %.1fs; cleanup remains runtime-owned",
                JOURNAL_LIBSQL_SYNC_THREAD_JOIN_TIMEOUT_S,
            )

    @property
    def close_complete(self) -> bool:
        """Whether the SDK connection has confirmed physical closure."""
        return self._connection_closed

    def _close_in_background(self) -> None:
        """Finish close after every connection user has released the writer lock."""
        try:
            current = threading.current_thread()
            with self._lifecycle_lock:
                workers = (self._sync_thread, self._finalize_thread)
            for worker in workers:
                if worker is not None and worker is not current:
                    worker.join()

            # Serialize final mutation/sync/close with an append that started
            # just before admission closed. If an SDK call is stuck, only this
            # daemon owner waits; the public close boundary stays bounded.
            with self._lock:
                if self._connection_closed:
                    return
                if self._finalize_requested:
                    try:
                        self._conn.execute(
                            "INSERT OR REPLACE INTO session_state "
                            "(key, value) VALUES ('clean_close', '1')"
                        )
                        self._conn.commit()
                    except Exception:
                        logger.debug("libsql clean_close marker write failed", exc_info=True)
                try:
                    self._conn.execute(
                        "DELETE FROM session_state WHERE key IN ('live_pid', 'live_pid_start')"
                    )
                    self._conn.commit()
                except Exception:
                    logger.debug("libsql live-owner marker cleanup failed", exc_info=True)

                # Remove the local owner marker before the final remote sync.
                # Syncing first would publish the live marker and then close
                # with its deletion only committed to the local replica.
                try:
                    self._conn.sync()
                    harden_sqlite_files(self._db_path)
                except Exception:
                    logger.debug("libsql final sync failed on close", exc_info=True)

                if not self._close_connection_locked():
                    return
                self._connection_closed = True
        finally:
            if self._connection_closed:
                self._release_live_journal()

    def _close_connection_locked(self) -> bool:
        """Close the SDK connection, restoring ownership when close fails."""
        try:
            self._conn.close()
        except Exception:
            logger.warning(
                "libSQL connection close failed; cleanup remains retryable",
                exc_info=True,
            )
            self._restore_owner_after_close_failure_locked()
            return False
        return True

    def _restore_owner_after_close_failure_locked(self) -> None:
        # Closing can fail before the SDK releases the local replica. Restore
        # the marker removed above so another process cannot mistake that
        # still-owned connection for a cleanly released writer.
        try:
            self._restore_live_owner_marker()
            self._conn.commit()
        except Exception:
            logger.warning(
                "libSQL live-owner marker restoration failed after close error",
                exc_info=True,
            )
            return
        try:
            self._conn.sync()
        except Exception:
            logger.warning(
                "libSQL restored owner marker could not sync after close error",
                exc_info=True,
            )

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
            if self._closed or self._degraded:
                return -1
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
                    redaction=self._redaction,
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
