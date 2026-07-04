import pytest

from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.events import (
    Error,
    ErrorStage,
    EventBus,
    STTFinal,
    SupervisorListenerAttached,
    SupervisorListenerDetached,
    TransportDegraded,
)
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.artifacts import InMemoryArtifactStore
from easycat.runtime.records import JournalRecordKind
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._session import Session
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


@pytest.mark.asyncio
async def test_journal_sink_records_transport_degraded() -> None:
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
        TransportDegraded(
            provider="webtransport",
            reason="inbound_queue_full",
            detail="dropped 320-byte mic frame; inbound queue full",
        )
    )
    await bus.emit(
        TransportDegraded(
            provider="webtransport",
            reason="control_codec_poisoned",
            detail="oversized control frame poisoned session 4",
            fatal=True,
        )
    )

    records = journal.read()
    assert [r.name for r in records] == ["transport_degraded", "transport_degraded"]
    # Recoverable single-frame drop stays on the EVENT timeline.
    assert records[0].kind == JournalRecordKind.EVENT
    assert records[0].data == {
        "provider": "webtransport",
        "reason": "inbound_queue_full",
        "detail": "dropped 320-byte mic frame; inbound queue full",
        "fatal": False,
    }
    # Fatal teardown is a control-plane record (mirrors ``interruption``).
    assert records[1].kind == JournalRecordKind.CONTROL
    assert records[1].data["reason"] == "control_codec_poisoned"
    assert records[1].data["fatal"] is True


@pytest.mark.asyncio
async def test_journal_sink_records_supervisor_audit_events() -> None:
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
        SupervisorListenerAttached(
            listener_id=1,
            queue_size=4,
            session_id="session-a",
        )
    )
    await bus.emit(
        SupervisorListenerDetached(
            listener_id=1,
            dropped_frames=2,
            reason="close",
            session_id="session-a",
        )
    )

    records = journal.read()
    assert [r.name for r in records] == [
        "supervisor_listener_attached",
        "supervisor_listener_detached",
    ]
    assert records[0].kind == JournalRecordKind.EVENT
    assert records[0].data == {"listener_id": 1, "queue_size": 4}
    assert records[1].data == {
        "listener_id": 1,
        "dropped_frames": 2,
        "reason": "close",
    }


@pytest.mark.asyncio
async def test_journal_sink_truncates_transport_degraded_detail() -> None:
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
        TransportDegraded(
            provider="websocket",
            reason="invalid_sample_rate",
            detail="x" * 600,
        )
    )

    [record] = journal.read()
    assert len(record.data["detail"]) < 560
    assert "truncated 88 chars" in record.data["detail"]


@pytest.mark.asyncio
async def test_journal_sink_records_error_code() -> None:
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

    exc = TimeoutError("agent timed out")
    exc.code = "EASYCAT_E301"  # type: ignore[attr-defined]
    await bus.emit(Error(exception=exc, stage=ErrorStage.AGENT, provider="openai", turn_id="t1"))

    record = journal.read()[0]
    assert record.name == "error"
    assert record.data["code"] == "EASYCAT_E301"
    assert record.data["provider"] == "openai"
    assert record.data["stage"] == "agent"
    assert record.error is not None
    assert record.error.type == "TimeoutError"
    assert record.error.notes == ("stage=agent\nprovider=openai\ncode=EASYCAT_E301\nturn_id=t1")


@pytest.mark.asyncio
async def test_journal_sink_records_error_runtime_context() -> None:
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

    exc = RuntimeError("agent failed")
    await bus.emit(
        Error(
            exception=exc,
            stage=ErrorStage.AGENT,
            turn_id="t1",
            elapsed_ms=12.3456,
            sequence=42,
            record_key="cp_42",
        )
    )

    record = journal.read()[0]
    assert record.data["elapsed_ms"] == 12.3456
    assert record.data["sequence"] == 42
    assert record.data["record_ref"] == "cp_42"
    assert record.error is not None
    assert record.error.notes == (
        "stage=agent\nturn_id=t1\nelapsed_ms=12.346\nsequence=42\nrecord_key=cp_42"
    )


@pytest.mark.asyncio
async def test_journal_sink_normalizes_structured_output() -> None:
    """``structured_output`` (Any-typed) must be normalized to a JSON-native
    shape so SQLite (json.dumps default=str) and in-memory store the same
    thing instead of a repr string (regression for runtime-observability-3).
    """
    import dataclasses
    import json

    from easycat.events import AgentFinal

    @dataclasses.dataclass
    class Weather:
        city: str
        temp_c: int

    class PydanticLike:
        def __init__(self, ok: bool) -> None:
            self._ok = ok

        def model_dump(self, mode: str = "python") -> dict[str, object]:
            return {"ok": self._ok}

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

    await bus.emit(AgentFinal(text="done", structured_output=Weather(city="SF", temp_c=18)))
    await bus.emit(AgentFinal(text="done", structured_output=PydanticLike(ok=True)))

    records = journal.read()
    assert records[0].data["structured_output"] == {"city": "SF", "temp_c": 18}
    assert records[1].data["structured_output"] == {"ok": True}
    # Both must be JSON-serializable without the default=str repr fallback.
    assert json.loads(json.dumps(records[0].data["structured_output"])) == {
        "city": "SF",
        "temp_c": 18,
    }


@pytest.mark.asyncio
async def test_journal_sink_records_session_action_failure_reason() -> None:
    """A failed session action must journal *why* it failed, not just that it
    did. ``SessionActionFailed`` carries its detail in an ``error`` str (no
    ``exception`` attribute), so the reason has to land in the record data.
    """
    from easycat.events import SessionActionFailed
    from easycat.session.actions import CustomAction

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
        SessionActionFailed(
            action=CustomAction(name="unsupported"),
            error="No session action executor supports CustomAction",
        )
    )

    records = journal.read()
    assert len(records) == 1
    record = records[0]
    assert record.name == "session_action_failed"
    assert record.data is not None
    assert record.data["error"] == "No session action executor supports CustomAction"
    assert record.data["action"]["type"] == "custom"


@pytest.mark.asyncio
async def test_journal_sink_coerces_non_json_native_action_payload() -> None:
    """``CustomAction.payload`` may carry non-JSON-native leaves (a set /
    datetime). ``asdict`` alone leaves them live, so the in-memory backend
    keeps the live object while a persistent backend
    ``json.dumps(default=str)``-stringifies it — divergent shapes. The sink
    must coerce them so every backend stores an identical JSON-native shape.
    """
    import datetime
    import json

    from easycat.events import SessionActionRequested
    from easycat.session.actions import CustomAction

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

    payload = {
        "tags": {"vip"},
        "scheduled_at": datetime.datetime(2026, 6, 28, 12, 0, 0),
    }
    await bus.emit(SessionActionRequested(action=CustomAction(name="schedule", payload=payload)))

    record = journal.read()[0]
    assert record.name == "session_action_requested"
    # The persistent backend's json.dumps(default=str) round-trip is now a
    # no-op, so the in-memory and persistent backends store an identical shape.
    assert record.data == json.loads(json.dumps(record.data, default=str))
    assert record.data["action"]["payload"] == "[REDACTED_SESSION_ACTION_PAYLOAD]"


@pytest.mark.asyncio
async def test_journal_sink_redacts_sensitive_session_action_fields() -> None:
    from easycat.events import SessionActionCompleted, SessionActionRequested
    from easycat.session.actions import (
        SendDTMFAction,
        SendSMSAction,
        SessionActionResult,
        TransferCallAction,
        TransferPlan,
    )

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

    await bus.emit(SessionActionRequested(action=SendDTMFAction(digits="1234#")))
    await bus.emit(
        SessionActionRequested(
            action=TransferCallAction(
                target="+15551234567",
                reason="escalation",
                plan=TransferPlan(post_dial_digits="9876", caller_id="+15557654321"),
            )
        )
    )
    await bus.emit(
        SessionActionCompleted(
            action=SendSMSAction(to="+15550001111", body="PIN 1234 for +15551234567"),
            executor="TwilioSessionActionExecutor",
            result=SessionActionResult(metadata={"message_sid": "SM123", "to": "+15550001111"}),
        )
    )

    dtmf, transfer, sms = journal.read()
    assert dtmf.data["action"]["digits"] == "[REDACTED_SESSION_ACTION_VALUE]"
    assert transfer.data["action"]["target"] == "[REDACTED_SESSION_ACTION_VALUE]"
    assert transfer.data["action"]["plan"]["post_dial_digits"] == (
        "[REDACTED_SESSION_ACTION_VALUE]"
    )
    assert transfer.data["action"]["plan"]["caller_id"] == "[REDACTED_SESSION_ACTION_VALUE]"
    assert transfer.data["action"]["reason"] == "escalation"
    assert sms.data["action"]["body"] == "[REDACTED_SESSION_ACTION_VALUE]"
    assert sms.data["action"]["to"] == "[REDACTED_SESSION_ACTION_VALUE]"
    assert sms.data["result"]["metadata"]["message_sid"] == "SM123"
    assert sms.data["result"]["metadata"]["to"] == "[REDACTED_SESSION_ACTION_VALUE]"


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
    await session.stop(force=True)

    assert view is not None
    records = view.read()
    assert [record.name for record in records] == ["audio_queue_drop"]

    await bus.emit(STTFinal(text="late", turn_id="late-turn"))

    assert [record.name for record in view.read()] == ["audio_queue_drop"]
    assert view.read()[0].kind == JournalRecordKind.EVENT
