import pytest

from easycat.cancel import CancelToken
from easycat.events import EventBus, STTFinal
from easycat.runtime.artifacts import InMemoryArtifactStore
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.runtime.records import JournalRecordKind
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._session import Session
from easycat.session._turn_context import TurnContext
from easycat.session._types import SessionConfig


@pytest.mark.asyncio
async def test_journal_sink_subscribes_session_events() -> None:
    bus = EventBus()
    journal = InMemoryRingBuffer()
    sink = SessionJournalSink(
        event_bus=bus,
        journal=journal,
        artifact_store=None,
        session_id="session-a",
        current_turn_id=lambda turn_id=None: turn_id,
    )
    sink.subscribe()

    await bus.emit(
        STTFinal(text="hello", track="caller", session_id="event-session", turn_id="t1")
    )

    records = journal.read()
    assert len(records) == 1
    assert records[0].name == "stt_final"
    assert records[0].session_id == "event-session"
    assert records[0].turn_id == "t1"
    assert records[0].data == {"text": "hello", "track": "caller"}


def test_journal_sink_stores_artifact_refs_before_record() -> None:
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(artifact_store=artifact_store)
    sink = SessionJournalSink(
        event_bus=EventBus(),
        journal=journal,
        artifact_store=artifact_store,
        session_id="session-a",
        current_turn_id=lambda turn_id=None: turn_id,
    )

    sink.append_record(
        name="artifact_record",
        input_bytes=b"input",
        output_bytes=b"output",
        input_artifact_class="replay_critical",
    )

    record = journal.read()[0]
    assert record.name == "artifact_record"
    assert record.input_ref is not None
    assert record.output_ref is not None
    assert artifact_store.has(record.input_ref)
    assert artifact_store.has(record.output_ref)


def test_session_markdown_strip_delegates_to_journal_sink() -> None:
    journal = InMemoryRingBuffer()
    session = Session(SessionConfig(runtime_mode="text_session", journal=journal))
    session._turn = TurnContext("turn-markdown", CancelToken())

    session._tts_scheduler._record_markdown_strip(
        phase="streaming_final",
        original_text="Go to **Settings**.",
        stripped_text="Go to Settings.",
        turn_id=session._turn.id,
    )

    record = journal.read()[0]
    assert record.name == "markdown_stripped"
    assert record.turn_id == "turn-markdown"
    assert record.data == {
        "phase": "streaming_final",
        "changed": True,
        "original_text": "Go to **Settings**.",
        "stripped_text": "Go to Settings.",
    }


def test_session_queue_drop_delegates_to_journal_sink() -> None:
    journal = InMemoryRingBuffer()
    session = Session(SessionConfig(runtime_mode="text_session", journal=journal))

    session._on_queue_drop(
        queue_name="outbound_audio",
        kind="oldest",
        queue_len=200,
        total_drops=3,
    )

    record = journal.read()[0]
    assert record.name == "audio_queue_drop"
    assert record.data == {
        "queue": "outbound_audio",
        "kind": "oldest",
        "queue_len": 200,
        "total_drops": 3,
    }


@pytest.mark.asyncio
async def test_session_journal_stays_read_only_after_shutdown() -> None:
    bus = EventBus()
    journal = InMemoryRingBuffer()
    session = Session(SessionConfig(runtime_mode="text_session", event_bus=bus, journal=journal))
    view = session.journal

    session._on_queue_drop(
        queue_name="outbound_audio",
        kind="oldest",
        queue_len=200,
        total_drops=1,
    )
    await session.shutdown()

    assert view is not None
    records = view.read()
    assert [record.name for record in records] == ["audio_queue_drop"]

    await bus.emit(STTFinal(text="late", turn_id="late-turn"))

    assert [record.name for record in view.read()] == ["audio_queue_drop"]
    assert view.read()[0].kind == JournalRecordKind.EVENT
