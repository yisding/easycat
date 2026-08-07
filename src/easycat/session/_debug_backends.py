"""Post-stop debug backend preservation for Session."""

from __future__ import annotations

from dataclasses import dataclass

from easycat.runtime.artifacts import ArtifactStore, SnapshotArtifactStore
from easycat.runtime.journal import ExecutionJournal, JournalView
from easycat.runtime.journal_memory import InMemoryRingBuffer
from easycat.runtime.journal_views import ReadonlySqliteJournal
from easycat.session._journal_sink import SessionJournalSink


@dataclass(frozen=True, slots=True)
class DebugBackendState:
    """Current journal/artifact handles after a backend lifecycle transition."""

    journal: ExecutionJournal | None
    artifact_store: ArtifactStore | None


class SessionDebugBackends:
    """Own finalization and postmortem preservation of debug backends."""

    def __init__(
        self,
        *,
        journal: ExecutionJournal | None,
        journal_view: JournalView | None,
        artifact_store: ArtifactStore | None,
        journal_sink: SessionJournalSink,
    ) -> None:
        self._journal = journal
        self._journal_view = journal_view
        self._artifact_store = artifact_store
        self._journal_sink = journal_sink
        self._flushed = False
        self._destroyed = False
        self._pending_journal_close: ExecutionJournal | None = None

    @property
    def state(self) -> DebugBackendState:
        return DebugBackendState(
            journal=self._journal,
            artifact_store=self._artifact_store,
        )

    def close(self) -> DebugBackendState:
        """Write the clean-close marker without tearing down live backends."""
        if self._flushed:
            return self.state
        if self._journal is not None:
            self._journal.finalize()
        self._flushed = True
        return self.state

    def destroy(self) -> DebugBackendState:
        """Close live debug backends while preserving read-only inspection."""
        self._retry_pending_journal_close()
        if self._destroyed:
            return self.state
        self.close()

        if self._journal is not None:
            live_journal = self._journal
            replacement = _preserve_journal_after_destroy(live_journal)
            live_journal.close()
            if not _journal_close_complete(live_journal):
                # LibsqlJournal bounds public close by delegating a stuck SDK
                # operation to a daemon worker. The journal's process-global
                # pending-close registry owns the live object after this
                # Session disappears; this local handle lets a later
                # destroy()/Session.stop() service the retry directly.
                self._pending_journal_close = live_journal
            self._journal = replacement
            if self._journal_view is not None:
                # Keep previously-cached ``session.journal`` views useful
                # after the live backend has been closed.
                self._journal_view._journal = replacement

        if self._artifact_store is not None:
            live_store = self._artifact_store
            replacement_store = _preserve_artifacts_after_destroy(live_store)
            live_store.close()
            self._artifact_store = replacement_store

        self._journal_sink.replace_backends(
            journal=self._journal,
            artifact_store=self._artifact_store,
        )
        self._destroyed = True
        return self.state

    def _retry_pending_journal_close(self) -> None:
        pending = self._pending_journal_close
        if pending is None:
            return
        pending.close()
        if _journal_close_complete(pending):
            self._pending_journal_close = None


def _journal_close_complete(journal: ExecutionJournal) -> bool:
    """Treat synchronous journals as complete unless they expose a pending close."""
    return bool(getattr(journal, "close_complete", True))


def _preserve_journal_after_destroy(journal: ExecutionJournal) -> ExecutionJournal:
    db_path = getattr(journal, "db_path", None)
    if db_path is not None:
        return ReadonlySqliteJournal(db_path, degraded=journal.degraded)
    if isinstance(journal, InMemoryRingBuffer):
        return journal.snapshot()
    return journal


def _preserve_artifacts_after_destroy(artifact_store: ArtifactStore) -> ArtifactStore:
    store = getattr(artifact_store, "_store", None)
    if isinstance(store, dict):
        return SnapshotArtifactStore(store)
    return artifact_store
