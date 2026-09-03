"""Shared SQL schema and record encode/decode helpers for journal backends.

Single source of truth for the persisted journal row shape: the schema,
the INSERT statement, the value-tuple builder, and the row decoder live
together so the SQL backends (:class:`~easycat.runtime.journal_sql.SqliteJournal`,
:class:`~easycat.runtime.journal_sql.LibsqlJournal`) and the read-only views
cannot silently diverge.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from easycat.runtime.records import (
    BufferOverflow,
    ErrorInfo,
    JournalDegraded,
    JournalRecord,
    JournalRecordKind,
    RecoveredSessionMarker,
    TimingInfo,
)
from easycat.runtime.safe_defaults import apply_write_filter
from easycat.validation.redaction import RedactionPolicy

logger = logging.getLogger(__name__)


_SQLITE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS journal (
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
    error_children TEXT,
    stage        TEXT,
    observed_stage TEXT
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

_INDEX_MIGRATION_VERSION = 2
_INDEX_BACKFILL_BATCH_SIZE = 500


# Single source of truth for the persisted INSERT shared by every SQL backend
# (SqliteJournal / LibsqlJournal).  Keeping the column list, placeholders, and
# the value-tuple builder (``_encode_journal_row``) together guarantees the two
# backends cannot silently diverge — a column add/reorder is a one-place change
# that ``_row_to_record`` round-trips identically for both.
_JOURNAL_INSERT_SQL = (
    "INSERT INTO journal "
    "(sequence, session_id, kind, name, wall_ns, mono_ns, cpu_ns, "
    "turn_id, data, error_type, error_msg, error_tb, error_notes, "
    "input_ref, output_ref, tags, error_children, stage, observed_stage) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _indexable_token(data: dict[str, Any] | None, key: str) -> str | None:
    """Return a non-empty string token from record data, else ``None``."""
    if not isinstance(data, dict):
        return None
    candidate = data.get(key)
    return candidate if isinstance(candidate, str) and candidate else None


def _stage_of(data: dict[str, Any] | None) -> str | None:
    """Return the primary indexable ``stage`` token from record data."""
    return _indexable_token(data, "stage")


def _observed_stage_of(data: dict[str, Any] | None) -> str | None:
    """Return the indexable ``observed_stage`` token from record data."""
    return _indexable_token(data, "observed_stage")


def _insert_tag_index_rows(conn: Any, sequence: int, tags: frozenset[str]) -> None:
    """Populate the ``journal_tags`` junction for one record's tags.

    Runs inside the caller's open transaction (no extra COMMIT), so tag-index
    rows land atomically with the ``journal`` row itself.  Records without tags
    (the common case) do zero work.
    """
    for tag in tags:
        conn.execute(
            "INSERT OR IGNORE INTO journal_tags (tag, sequence) VALUES (?, ?)",
            (tag, sequence),
        )


def _escape_like(value: str) -> str:
    """Escape SQL ``LIKE`` metacharacters so *value* matches literally.

    Pairs with ``ESCAPE '\\'`` in the predicate.  Backslash is escaped first so
    the escape character itself is treated literally, then ``%`` and ``_`` (the
    LIKE wildcards) are neutralised — without this a tag containing ``%`` or
    ``_`` would match unrelated records.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_slice_where(
    *,
    kind: JournalRecordKind | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    name: str | None = None,
    tags: frozenset[str] | None = None,
    use_tag_index: bool = False,
) -> tuple[str, list[Any]]:
    """Build the ``WHERE`` clause + params shared by every SQL ``slice``.

    ``kind``/``session_id``/``turn_id``/``name`` map to indexed equality
    predicates (``turn_id`` is backed by ``idx_journal_turn_id``).

    ``tags`` uses subset semantics — a record matches when every requested tag
    is present — matching the in-memory backend (``requested <= record.tags``).
    Two tag strategies are available:

    * ``use_tag_index=True`` (live SQL backends): each requested tag becomes a
      ``sequence IN (SELECT sequence FROM journal_tags WHERE tag = ?)``
      predicate served by the ``journal_tags(tag, sequence)`` primary-key
      index — no full-table scan.
    * ``use_tag_index=False`` (default; read-only views over arbitrary/older
      files that may predate the junction table): each requested tag matches
      the comma-wrapped ``tags`` column exactly via
      ``(',' || tags || ',') LIKE '%,tag,%'`` with LIKE metacharacters
      escaped.  This stays correct on files that lack ``journal_tags`` at the
      cost of a scan.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind.value)
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if turn_id is not None:
        clauses.append("turn_id = ?")
        params.append(turn_id)
    if name is not None:
        clauses.append("name = ?")
        params.append(name)
    if tags:
        for tag in sorted(tags):
            if use_tag_index:
                clauses.append("sequence IN (SELECT sequence FROM journal_tags WHERE tag = ?)")
                params.append(tag)
            else:
                clauses.append(r"(',' || tags || ',') LIKE ? ESCAPE '\'")
                params.append(f"%,{_escape_like(tag)},%")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _encode_journal_row(
    *,
    sequence: int,
    session_id: str,
    kind: JournalRecordKind,
    name: str,
    wall_ns: int,
    mono_ns: int,
    cpu_ns: int,
    turn_id: str | None,
    data: dict[str, Any] | None,
    error: ErrorInfo | None,
    tags: frozenset[str],
    input_ref: str | None,
    output_ref: str | None,
) -> tuple[Any, ...]:
    """Build the column-order value tuple for ``_JOURNAL_INSERT_SQL``."""
    if tags and any("," in tag for tag in tags):
        offending = next(tag for tag in tags if "," in tag)
        raise ValueError(f"tag {offending!r} contains ',' — commas are not allowed")
    error_children = (
        json.dumps([_error_info_to_dict(child) for child in error.children], default=str)
        if error is not None and error.children
        else None
    )
    return (
        sequence,
        session_id,
        kind.value,
        name,
        wall_ns,
        mono_ns,
        cpu_ns,
        turn_id,
        json.dumps(data or {}, default=str),
        error.type if error else None,
        error.message if error else None,
        error.traceback if error else None,
        error.notes if error else None,
        input_ref,
        output_ref,
        ",".join(sorted(tags)) if tags else "",
        error_children,
        _stage_of(data),
        _observed_stage_of(data),
    )


def _error_info_to_dict(error: ErrorInfo) -> dict[str, Any]:
    return {
        "type": error.type,
        "message": error.message,
        "traceback": error.traceback,
        "notes": error.notes,
        "children": [_error_info_to_dict(child) for child in error.children],
    }


def _error_info_from_dict(value: Any) -> ErrorInfo | None:
    if not isinstance(value, dict):
        return None
    children: list[ErrorInfo] = []
    raw_children = value.get("children")
    if isinstance(raw_children, list):
        for child_value in raw_children:
            child = _error_info_from_dict(child_value)
            if child is not None:
                children.append(child)
    return ErrorInfo(
        type=str(value.get("type") or ""),
        message=str(value.get("message") or ""),
        traceback=value.get("traceback") if isinstance(value.get("traceback"), str) else None,
        notes=value.get("notes") if isinstance(value.get("notes"), str) else None,
        children=tuple(children),
    )


def _ensure_journal_schema(conn: Any) -> None:
    """Additively migrate an existing ``journal`` table to the current schema.

    ``CREATE TABLE IF NOT EXISTS`` keeps a pre-existing table's *old* column
    set, so a file written by an older EasyCat needs the newer columns, index
    predicates, and side tables added by ``ALTER``/``CREATE ... IF NOT
    EXISTS`` here.  All operations are additive — no data is dropped — so
    crash-dump promotion and the recovered-session marker flow keep working.

    This applies only additive DDL. The stage/tag-index *backfill* is a
    separate step (:func:`_ensure_index_backfill`) that callers run after
    prior-session reconciliation, so rows that are about to be truncated
    (crash-dump promotion, clean reuse) are never rewritten first.
    """
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(journal)").fetchall()}
    if "error_children" not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN error_children TEXT")
    if "stage" not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN stage TEXT")
    if "observed_stage" not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN observed_stage TEXT")
    # Idempotent for a current-schema file (already created by _SQLITE_SCHEMA);
    # this is what backfills the query surface for an older file.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_turn_id ON journal(turn_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_stage ON journal(stage)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_journal_observed_stage ON journal(observed_stage)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS journal_tags ("
        "tag TEXT NOT NULL, sequence INTEGER NOT NULL, PRIMARY KEY (tag, sequence))"
    )


def _ensure_index_backfill(conn: Any) -> None:
    """Backfill derived stage columns and tag-index rows for pre-v2 files.

    Tracked by ``schema_version``: completion is recorded only after every
    historical row has been processed, so a process failure mid-backfill
    safely resumes on the next open. Run this *after* prior-session
    reconciliation — for the SQLite backend the live table is empty by then
    (prior rows were promoted/truncated), so the scan is O(0); for libSQL's
    retained-rows unclean reuse it keeps stage/tag queries correct.
    """
    version_row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    version = int(version_row[0]) if version_row and version_row[0] is not None else 0
    if version < _INDEX_MIGRATION_VERSION:
        _backfill_index_columns(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
            (_INDEX_MIGRATION_VERSION,),
        )


def _index_updates_for_rows(
    rows: list[tuple[Any, Any, Any]],
) -> tuple[list[tuple[str, int]], list[tuple[str, int]], list[tuple[str, int]]]:
    """Decode one migration batch into bulk update parameters."""
    stage_updates: list[tuple[str, int]] = []
    observed_stage_updates: list[tuple[str, int]] = []
    tag_rows: list[tuple[str, int]] = []
    for sequence, data_str, tags_str in rows:
        try:
            data = json.loads(data_str) if data_str else {}
        except (TypeError, ValueError):
            data = {}
        record_data = data if isinstance(data, dict) else None
        stage = _stage_of(record_data)
        observed_stage = _observed_stage_of(record_data)
        if stage is not None:
            stage_updates.append((stage, sequence))
        if observed_stage is not None:
            observed_stage_updates.append((observed_stage, sequence))
        if tags_str:
            tag_rows.extend((tag, sequence) for tag in str(tags_str).split(",") if tag)
    return stage_updates, observed_stage_updates, tag_rows


def _backfill_index_columns(conn: Any) -> None:
    """Idempotently populate derived indexes in bounded keyset batches."""
    last_sequence: int | None = None
    while True:
        if last_sequence is None:
            rows = conn.execute(
                "SELECT sequence, data, tags FROM journal ORDER BY sequence LIMIT ?",
                (_INDEX_BACKFILL_BATCH_SIZE,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT sequence, data, tags FROM journal WHERE sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (last_sequence, _INDEX_BACKFILL_BATCH_SIZE),
            ).fetchall()
        if not rows:
            return

        stage_updates, observed_stage_updates, tag_rows = _index_updates_for_rows(rows)
        if stage_updates:
            conn.executemany(
                "UPDATE journal SET stage = ? WHERE sequence = ?",
                stage_updates,
            )
        if observed_stage_updates:
            conn.executemany(
                "UPDATE journal SET observed_stage = ? WHERE sequence = ?",
                observed_stage_updates,
            )
        if tag_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO journal_tags (tag, sequence) VALUES (?, ?)",
                tag_rows,
            )
        last_sequence = int(rows[-1][0])


def _journal_record_for_append(
    *,
    sequence: int,
    session_id: str,
    kind: JournalRecordKind,
    name: str,
    timing: TimingInfo,
    turn_id: str | None,
    data: dict[str, Any] | None,
    error: ErrorInfo | None,
    tags: frozenset[str],
    input_ref: str | None,
    output_ref: str | None,
    redaction: RedactionPolicy,
) -> JournalRecord:
    return apply_write_filter(
        JournalRecord(
            sequence=sequence,
            session_id=session_id,
            kind=kind,
            name=name,
            timing=timing,
            turn_id=turn_id,
            data=data or {},
            error=error,
            input_ref=input_ref,
            output_ref=output_ref,
            tags=tags,
        ),
        redaction=redaction,
    )


def _persist_degraded_marker(conn: Any, session_id: str, exc: Exception) -> None:
    """Best-effort: record that a SQL-backed journal entered degraded mode.

    Makes degradation recoverable from the persisted file itself (so a bundle
    loaded fresh from disk can tell the journal silently dropped records), to
    match the in-memory backend which appends a ``JournalDegraded`` marker.

    Two complementary signals are written, both best-effort because the very
    write failure that triggered degraded mode may also block these:

    * a ``degraded`` key in ``session_state`` — a cheap durable flag that
      ``ReadonlySqliteJournal`` / bundle loading surface without scanning
      records;
    * a ``JournalDegraded`` row in the ``journal`` table at ``sequence=-1``
      (mirroring ``InMemoryRingBuffer``), so ``slice(kind=DEGRADED)`` and
      ``read(start=-1)`` rehydrate it via the existing ``_row_to_record``
      branch that was otherwise dead for the persistent backends.

    The journal table has a ``sequence INTEGER PRIMARY KEY`` so a second
    failure is idempotent via ``INSERT OR REPLACE``.
    """
    now_wall = time.time_ns()
    now_mono = time.monotonic_ns()
    now_cpu = time.process_time_ns()
    data = json.dumps(
        {"error_type": type(exc).__name__, "error_message": str(exc)},
        default=str,
    )
    try:
        conn.execute("INSERT OR REPLACE INTO session_state (key, value) VALUES ('degraded', '1')")
    except Exception:
        logger.debug("Failed to persist degraded session_state marker", exc_info=True)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO journal "
            "(sequence, session_id, kind, name, wall_ns, mono_ns, cpu_ns, data, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                -1,
                session_id,
                JournalRecordKind.DEGRADED.value,
                "journal_degraded",
                now_wall,
                now_mono,
                now_cpu,
                data,
                "",
            ),
        )
    except Exception:
        logger.debug("Failed to persist degraded journal marker", exc_info=True)
    # Commit so the markers and any preceding open batch survive process death.
    # No later append can reach a normal batch boundary after degraded mode.
    try:
        conn.commit()
    except Exception:
        logger.debug("Failed to commit degraded markers", exc_info=True)


def _normalize_journal_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """Coerce a raw ``SELECT *`` row to the 17 canonical record columns.

    The first 17 columns are the record's own fields; anything after them is a
    derived index column recomputed from ``data`` on write, so it is dropped
    here rather than round-tripped. A 16-column row predates
    ``error_children`` (an older on-disk file read via a read-only view that
    cannot ALTER); pad it with a trailing ``None``.
    """
    n = len(row)
    if n == 16:
        return (*row, None)
    if n >= 18:
        return row[:17]
    if n != 17:
        raise ValueError(f"Unexpected journal row shape with {n} columns.")
    return row


def _row_to_record(row: tuple[Any, ...]) -> JournalRecord:
    row = _normalize_journal_row(row)
    (
        sequence,
        session_id,
        kind_str,
        name,
        wall_ns,
        mono_ns,
        cpu_ns,
        turn_id,
        data_str,
        error_type,
        error_msg,
        error_tb,
        error_notes,
        input_ref,
        output_ref,
        tags_str,
        error_children_str,
    ) = row
    data = json.loads(data_str) if data_str else {}
    error_children: list[ErrorInfo] = []
    raw_error_children = json.loads(error_children_str) if error_children_str else None
    if isinstance(raw_error_children, list):
        for child_value in raw_error_children:
            child = _error_info_from_dict(child_value)
            if child is not None:
                error_children.append(child)
    error = None
    if error_type:
        error = ErrorInfo(
            type=error_type,
            message=error_msg or "",
            traceback=error_tb,
            notes=error_notes,
            children=tuple(error_children),
        )
    tag_set = frozenset(tags_str.split(",")) if tags_str else frozenset()
    kind = JournalRecordKind(kind_str)
    common = {
        "sequence": sequence,
        "session_id": session_id,
        "kind": kind,
        "name": name,
        "timing": TimingInfo(wall_ns=wall_ns, mono_ns=mono_ns, cpu_ns=cpu_ns),
        "turn_id": turn_id,
        "data": data,
        "error": error,
        "input_ref": input_ref,
        "output_ref": output_ref,
        "tags": tag_set,
    }
    # Reconstruct typed subclasses so their schema-declared fields are
    # populated on SQLite round-trip rather than collapsing to the base
    # JournalRecord.  Subclass-only fields are sourced from ``data``.
    if kind is JournalRecordKind.RECOVERY and name == "recovered_session":
        return RecoveredSessionMarker(
            recovered_record_count=int(data.get("recovered_record_count", 0)),
            original_session_id=str(data.get("original_session_id", "")),
            **common,
        )
    if kind is JournalRecordKind.CONTROL and name == "buffer_overflow":
        return BufferOverflow(**common)
    if kind is JournalRecordKind.DEGRADED and name == "journal_degraded":
        return JournalDegraded(**common)
    return JournalRecord(**common)
