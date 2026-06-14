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
    error_children TEXT
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


# Single source of truth for the persisted INSERT shared by every SQL backend
# (SqliteJournal / LibsqlJournal).  Keeping the column list, placeholders, and
# the value-tuple builder (``_encode_journal_row``) together guarantees the two
# backends cannot silently diverge — a column add/reorder is a one-place change
# that ``_row_to_record`` round-trips identically for both.
_JOURNAL_INSERT_SQL = (
    "INSERT INTO journal "
    "(sequence, session_id, kind, name, wall_ns, mono_ns, cpu_ns, "
    "turn_id, data, error_type, error_msg, error_tb, error_notes, "
    "input_ref, output_ref, tags, error_children) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
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
) -> tuple[str, list[Any]]:
    """Build the ``WHERE`` clause + params shared by every SQL ``slice``.

    ``kind``/``session_id``/``turn_id``/``name`` map to indexed equality
    predicates.  ``tags`` is stored as a sorted comma-joined string (see
    :data:`_SQLITE_SCHEMA`), so each requested tag matches the comma-wrapped
    column exactly — ``(',' || tags || ',') LIKE '%,tag,%'`` with LIKE
    metacharacters in the tag escaped.  This gives the same exact-subset
    semantics as the in-memory backend (``requested <= record.tags``) rather
    than a loose substring match (so ``"stt"`` never matches ``"not_stt"``).
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
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(journal)").fetchall()}
    if "error_children" not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN error_children TEXT")


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
        )
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
    # Commit so the markers survive process death even though no further
    # append() (which would otherwise COMMIT) will run after degraded mode.
    try:
        conn.commit()
    except Exception:
        logger.debug("Failed to commit degraded markers", exc_info=True)


def _row_to_record(row: tuple[Any, ...]) -> JournalRecord:
    if len(row) == 16:
        row = (*row, None)
    elif len(row) != 17:
        raise ValueError(f"Unexpected journal row shape with {len(row)} columns.")
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
    common = dict(
        sequence=sequence,
        session_id=session_id,
        kind=kind,
        name=name,
        timing=TimingInfo(wall_ns=wall_ns, mono_ns=mono_ns, cpu_ns=cpu_ns),
        turn_id=turn_id,
        data=data,
        error=error,
        input_ref=input_ref,
        output_ref=output_ref,
        tags=tag_set,
    )
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
