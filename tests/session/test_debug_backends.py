import pytest

from easycat.events import EventBus
from easycat.runtime import InMemoryRingBuffer, JournalView
from easycat.runtime.artifacts import InMemoryArtifactStore
from easycat.runtime.records import JournalRecordKind
from easycat.session._debug_backends import SessionDebugBackends
from easycat.session._journal_sink import SessionJournalSink
from tests.session._session_core_helpers import TrackingJournal


def _debug_backends(journal: TrackingJournal) -> SessionDebugBackends:
    sink = SessionJournalSink(
        event_bus=EventBus(),
        journal=journal,
        artifact_store=None,
        session_id="session-a",
        current_turn_id=lambda turn_id=None: turn_id,
    )
    return SessionDebugBackends(
        journal=journal,
        journal_view=JournalView(journal),
        artifact_store=None,
        journal_sink=sink,
    )


def test_debug_backends_close_retries_after_finalize_failure() -> None:
    class FailingOnceJournal(TrackingJournal):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_finalize = True

        def finalize(self) -> None:
            self.finalize_calls += 1
            if self.fail_next_finalize:
                self.fail_next_finalize = False
                raise RuntimeError("finalize failed")

    journal = FailingOnceJournal()
    backends = _debug_backends(journal)

    with pytest.raises(RuntimeError, match="finalize failed"):
        backends.close()
    backends.close()

    assert journal.finalize_calls == 2
    assert journal.close_calls == 0


def test_debug_backends_destroy_retries_after_backend_close_failure() -> None:
    class FailingOnceJournal(TrackingJournal):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_close = True

        def close(self) -> None:
            self.close_calls += 1
            if self.fail_next_close:
                self.fail_next_close = False
                raise RuntimeError("close failed")

    journal = FailingOnceJournal()
    backends = _debug_backends(journal)

    with pytest.raises(RuntimeError, match="close failed"):
        backends.destroy()
    backends.destroy()

    assert journal.finalize_calls == 1
    assert journal.close_calls == 2


def test_debug_backends_destroy_preserves_read_only_memory_backends() -> None:
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(artifact_store=artifact_store)
    ref = artifact_store.put(b"artifact-bytes")
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="before_destroy",
        session_id="session-a",
        input_ref=ref,
    )
    view = JournalView(journal)
    sink = SessionJournalSink(
        event_bus=EventBus(),
        journal=journal,
        artifact_store=artifact_store,
        session_id="session-a",
        current_turn_id=lambda turn_id=None: turn_id,
    )

    state = SessionDebugBackends(
        journal=journal,
        journal_view=view,
        artifact_store=artifact_store,
        journal_sink=sink,
    ).destroy()

    assert state.journal is not journal
    assert state.artifact_store is not artifact_store
    assert artifact_store.get(ref) is None
    assert state.artifact_store is not None
    assert state.artifact_store.get(ref) == b"artifact-bytes"
    assert [record.name for record in view.read()] == ["before_destroy"]
    assert (
        state.journal is not None
        and state.journal.append(
            kind=JournalRecordKind.EVENT,
            name="late",
            session_id="session-a",
        )
        == -1
    )
    assert [record.name for record in view.read()] == ["before_destroy"]
    assert sink.journal is state.journal
    assert sink.artifact_store is state.artifact_store
