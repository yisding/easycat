"""Read-only journal views: persisted-SQLite wrapper and frozen in-memory snapshot."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from easycat.runtime._journal_codec import _build_slice_where, _row_to_record
from easycat.runtime.records import ErrorInfo, JournalRecord, JournalRecordKind


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
        turn_id: str | None = None,
        name: str | None = None,
        tags: frozenset[str] | None = None,
    ) -> list[JournalRecord]:
        where, params = _build_slice_where(
            kind=kind, session_id=session_id, turn_id=turn_id, name=name, tags=tags
        )
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
        # Honor an explicit flag from the live backend, but also surface the
        # persisted ``degraded`` session_state marker so a bundle loaded fresh
        # from the .sqlite file (no live signal) still reports degradation.
        if self._degraded:
            return True
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM session_state WHERE key = 'degraded'"
                ).fetchone()
        except sqlite3.Error:
            return False
        return bool(row and row[0] == "1")

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)

    def _query(self, sql: str, params: list[Any]) -> list[JournalRecord]:
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_record(r) for r in rows]


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
        turn_id: str | None = None,
        name: str | None = None,
        tags: frozenset[str] | None = None,
    ) -> list[JournalRecord]:
        out = list(self._records)
        if kind is not None:
            out = [r for r in out if r.kind == kind]
        if session_id is not None:
            out = [r for r in out if r.session_id == session_id]
        if turn_id is not None:
            out = [r for r in out if r.turn_id == turn_id]
        if name is not None:
            out = [r for r in out if r.name == name]
        if tags:
            out = [r for r in out if tags <= r.tags]
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
