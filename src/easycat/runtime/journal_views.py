"""Read-only journal views: persisted-SQLite wrapper and frozen in-memory snapshot."""

from __future__ import annotations

import copy
import sqlite3
from pathlib import Path
from typing import Any

from easycat.runtime._journal_codec import _build_slice_where, _row_to_record
from easycat.runtime._private_files import sqlite_readonly_uri
from easycat.runtime.journal import _read_records, _slice_records, _validate_read_limit
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
        _validate_read_limit(limit)
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

    def slice_by_stage(self, stage_name: str) -> list[JournalRecord]:
        # Indexed stage query avoids full scan for read-only postmortem views (gh 1026).
        # Fall back to scan for legacy journals without stage columns.
        try:
            return self._query(
                "SELECT * FROM journal WHERE stage = ? OR observed_stage = ? ORDER BY sequence",
                [stage_name, stage_name],
            )
        except Exception:  # noqa: BLE001
            # Legacy file without stage columns — fall back to full scan
            return [
                r
                for r in self._query("SELECT * FROM journal ORDER BY sequence", [])
                if getattr(r, "stage", None) == stage_name
                or (
                    isinstance(getattr(r, "data", None), dict)
                    and (
                        r.data.get("stage") == stage_name
                        or r.data.get("observed_stage") == stage_name
                    )
                )
            ]

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
        return sqlite3.connect(sqlite_readonly_uri(self._db_path), uri=True)

    def _query(self, sql: str, params: list[Any]) -> list[JournalRecord]:
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_record(r) for r in rows]


# ── Frozen snapshot (read-only in-memory journal) ────────────────


class FrozenJournalSnapshot:
    """Immutable point-in-time copy of an in-memory journal."""

    def __init__(
        self,
        records: list[JournalRecord],
        *,
        degraded: bool = False,
        latest_sequence: int | None = None,
        dropped_records: int = 0,
    ) -> None:
        self._records = tuple(copy.deepcopy(records))
        self._degraded = degraded
        self._dropped_records = dropped_records
        self._latest_sequence = (
            latest_sequence
            if latest_sequence is not None
            else max((record.sequence for record in records if record.sequence >= 0), default=0)
        )

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
        return copy.deepcopy(_read_records(self._records, start=start, limit=limit))

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        name: str | None = None,
        tags: frozenset[str] | None = None,
    ) -> list[JournalRecord]:
        return copy.deepcopy(
            _slice_records(
                self._records,
                kind=kind,
                session_id=session_id,
                turn_id=turn_id,
                name=name,
                tags=tags,
            )
        )

    def slice_by_stage(self, stage_name: str) -> list[JournalRecord]:
        def _matches(record: JournalRecord) -> bool:
            data = record.data
            if not isinstance(data, dict):
                return False
            return stage_name in (data.get("stage"), data.get("observed_stage"))

        return copy.deepcopy([r for r in self._records if _matches(r)])

    def close(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def finalize(self) -> None:
        pass

    @property
    def latest_sequence(self) -> int:
        return self._latest_sequence

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def dropped_records(self) -> int:
        return self._dropped_records
