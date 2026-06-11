"""Backend selection factory for the execution journal."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from easycat.runtime.journal_memory import InMemoryRingBuffer
from easycat.runtime.journal_sql import (
    LibsqlJournal,
    LitestreamSqliteJournal,
    SqliteJournal,
)

if TYPE_CHECKING:
    from easycat.runtime.artifacts import InMemoryArtifactStore

logger = logging.getLogger(__name__)


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
