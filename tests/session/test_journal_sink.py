import asyncio
import hashlib
import threading

import pytest

from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    CallAnswered,
    CallEnded,
    CallFailed,
    CallScreening,
    Error,
    ErrorStage,
    EventBus,
    PlaybackMarkAck,
    ReconnectAttempt,
    STTFinal,
    SupervisorListenerAttached,
    SupervisorListenerDetached,
    ToolCallResult,
    TransportDegraded,
)
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.artifacts import InMemoryArtifactStore
from easycat.runtime.records import JournalRecordKind
from easycat.session._journal_sink import _SIMPLE_EVENT_RECORDS, SessionJournalSink
from easycat.session._session import Session
from easycat.session._types import SessionConfig


def test_simple_event_record_registry_is_complete() -> None:
    expected_names = {
        "AgentDelta": "agent_delta",
        "AgentFinal": "agent_final",
        "AgentRequestStarted": "agent_request_started",
        "BotStartedSpeaking": "bot_started_speaking",
        "BotStoppedSpeaking": "bot_stopped_speaking",
        "CallAnswered": "call_answered",
        "CallEnded": "call_ended",
        "CallFailed": "call_failed",
        "CallScreening": "call_screening",
        "Error": "error",
        "PlaybackMarkAck": "playback_mark_ack",
        "ReconnectAttempt": "ws_reconnect_attempt",
        "ReconnectFailure": "ws_reconnect_failure",
        "ReconnectSuccess": "ws_reconnect_success",
        "STTFinal": "stt_final",
        "STTPartial": "stt_partial",
        "SessionActionCompleted": "session_action_completed",
        "SessionActionFailed": "session_action_failed",
        "SessionActionRequested": "session_action_requested",
        "SessionActionStarted": "session_action_started",
        "SupervisorListenerAttached": "supervisor_listener_attached",
        "SupervisorListenerDetached": "supervisor_listener_detached",
        "ToolCallDelta": "tool_call_delta",
        "ToolCallResult": "tool_call_result",
        "ToolCallStarted": "tool_call_started",
        "TurnEnded": "turn_ended",
        "TurnStarted": "turn_started",
        "VADStartSpeaking": "vad_start_speaking",
        "VADStopSpeaking": "vad_stop_speaking",
    }

    assert {
        spec.event_type.__name__: spec.name for spec in _SIMPLE_EVENT_RECORDS
    } == expected_names
    assert len(_SIMPLE_EVENT_RECORDS) == len(expected_names)
    assert len({spec.name for spec in _SIMPLE_EVENT_RECORDS}) == len(_SIMPLE_EVENT_RECORDS)
    assert all(spec.kind == JournalRecordKind.EVENT for spec in _SIMPLE_EVENT_RECORDS)


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
async def test_journal_sink_preserves_agent_text_replacement_metadata() -> None:
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

    await bus.emit(AgentDelta(text="correct", part_index=2, replacement=True))

    [record] = journal.read()
    assert record.data == {"text": "correct", "part_index": 2, "replacement": True}


@pytest.mark.asyncio
async def test_journal_sink_preserves_reconnect_and_playback_identifiers() -> None:
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

    await bus.emit(ReconnectAttempt(provider="deepgram", attempt=2))
    await bus.emit(PlaybackMarkAck(mark_name="tts-17"))

    records = journal.read()
    assert records[0].data == {"provider": "deepgram", "attempt": 2}
    assert records[1].data == {"mark_name": "tts-17"}


@pytest.mark.asyncio
async def test_journal_sink_uses_selected_redaction_for_tool_results() -> None:
    bus = EventBus()
    journal = InMemoryRingBuffer(redaction="secrets")
    sink = SessionJournalSink(
        event_bus=bus,
        journal=journal,
        artifact_store=None,
        session_id="session-a",
        current_turn_id=lambda turn_id=None: turn_id,
        redaction="secrets",
    )
    sink.subscribe()

    await bus.emit(
        ToolCallResult(
            call_id="call-1",
            result=(
                "url=https://acme.example/orders/123 "
                "request_id=req_abcdef123 phone=+1 415 555 0123 "
                "api_key=secret-value"
            ),
        )
    )

    assert journal.read()[0].data["result"] == (
        "url=https://acme.example/orders/123 "
        "request_id=req_abcdef123 phone=+1 415 555 0123 "
        "api_key=[REDACTED_SECRET]"
    )


@pytest.mark.asyncio
async def test_journal_sink_records_telephony_lifecycle_events() -> None:
    bus = EventBus()
    journal = InMemoryRingBuffer(redaction="pii")
    sink = SessionJournalSink(
        event_bus=bus,
        journal=journal,
        artifact_store=None,
        session_id="session-a",
        current_turn_id=lambda turn_id=None: turn_id,
        redaction="pii",
    )
    sink.subscribe()

    await bus.emit(CallAnswered(call_sid="CA1", answered_by="human"))
    await bus.emit(CallScreening(call_sid="CA1", platform="ios"))
    await bus.emit(CallFailed(call_sid="CA1", reason="busy", sip_code=486, number="+15551234567"))
    await bus.emit(
        CallEnded(
            call_sid="CA1",
            duration_s=42.5,
            disposition="completed",
            number="+15551234567",
        )
    )

    records = journal.read()
    assert [record.name for record in records] == [
        "call_answered",
        "call_screening",
        "call_failed",
        "call_ended",
    ]
    assert records[0].data == {"call_sid": "CA1", "answered_by": "human"}
    assert records[1].data == {"call_sid": "CA1", "platform": "ios"}
    assert records[2].data == {
        "call_sid": "CA1",
        "reason": "busy",
        "sip_code": 486,
        "number": "[REDACTED_PHONE]",
    }
    assert records[3].data == {
        "call_sid": "CA1",
        "duration_s": 42.5,
        "disposition": "completed",
        "number": "[REDACTED_PHONE]",
    }


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
async def test_journal_sink_preserves_zero_error_context_and_omits_empty_ids() -> None:
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
        Error(
            exception=RuntimeError("failed immediately"),
            stage=ErrorStage.STT,
            provider="",
            code="",
            elapsed_ms=0.0,
            sequence=0,
            record_key="",
        )
    )

    [record] = journal.read()
    assert record.data == {"stage": "stt", "elapsed_ms": 0.0, "sequence": 0}
    assert record.error is not None


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
        "scheduled_at": datetime.datetime(2026, 6, 28, 12, 0, 0, tzinfo=datetime.UTC),
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


@pytest.mark.asyncio
async def test_async_artifact_write_finishes_referencing_record_before_cancellation() -> None:
    class BlockingArtifactStore(InMemoryArtifactStore):
        writes_block = True

        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            super().__init__()
            self._loop = loop
            self.started = asyncio.Event()
            self.release = threading.Event()
            self.finished = asyncio.Event()

        def put_with_cleanup_token(
            self,
            payload: bytes,
            *,
            artifact_class: str = "debug_verbose",
        ):
            self._loop.call_soon_threadsafe(self.started.set)
            try:
                if not self.release.wait(timeout=5):
                    raise AssertionError("timed out waiting to release artifact write")
                return super().put_with_cleanup_token(
                    payload,
                    artifact_class=artifact_class,
                )
            finally:
                self._loop.call_soon_threadsafe(self.finished.set)

    artifact_store = BlockingArtifactStore(asyncio.get_running_loop())
    journal = InMemoryRingBuffer(artifact_store=artifact_store)
    sink = SessionJournalSink(
        event_bus=EventBus(),
        journal=journal,
        artifact_store=artifact_store,
        session_id="session-a",
        current_turn_id=lambda turn_id=None: turn_id,
    )
    payload = b"cancelled artifact write"
    task = asyncio.create_task(
        sink.append_record_async(name="artifact_record", input_bytes=payload)
    )

    await asyncio.wait_for(artifact_store.started.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    operation_still_owned = not task.done()
    journal_was_empty_while_blocked = journal.read() == []
    artifact_store.release.set()
    await asyncio.wait_for(artifact_store.finished.wait(), timeout=5)

    with pytest.raises(asyncio.CancelledError):
        await task

    assert operation_still_owned
    assert journal_was_empty_while_blocked
    [record] = journal.read()
    expected_ref = hashlib.sha256(payload).hexdigest()
    assert record.input_ref == expected_ref
    assert artifact_store.has(expected_ref)


@pytest.mark.asyncio
async def test_rejected_journal_append_reclaims_new_artifacts() -> None:
    class RejectingJournal(InMemoryRingBuffer):
        def append(self, *args, **kwargs) -> int:
            del args, kwargs
            return -1

    artifact_store = InMemoryArtifactStore()
    journal = RejectingJournal(artifact_store=artifact_store)
    sink = SessionJournalSink(
        event_bus=EventBus(),
        journal=journal,
        artifact_store=artifact_store,
        session_id="session-a",
        current_turn_id=lambda turn_id=None: turn_id,
    )
    payloads = (
        b"sync-input",
        b"sync-output",
        b"async-input",
        b"async-output",
        b"sync-shared",
        b"async-shared",
    )

    assert (
        sink.append_record(
            name="artifact_record",
            input_bytes=payloads[0],
            output_bytes=payloads[1],
        )
        == -1
    )
    await sink.append_record_async(
        name="artifact_record",
        input_bytes=payloads[2],
        output_bytes=payloads[3],
    )
    assert (
        sink.append_record(
            name="artifact_record",
            input_bytes=payloads[4],
            output_bytes=payloads[4],
            input_artifact_class="debug_verbose",
            output_artifact_class="replay_critical",
        )
        == -1
    )
    await sink.append_record_async(
        name="artifact_record",
        input_bytes=payloads[5],
        output_bytes=payloads[5],
        input_artifact_class="debug_verbose",
        output_artifact_class="replay_critical",
    )

    assert all(not artifact_store.has(hashlib.sha256(payload).hexdigest()) for payload in payloads)
    assert artifact_store._current_bytes == 0


def test_rejected_journal_append_preserves_preexisting_artifact() -> None:
    class RejectingJournal(InMemoryRingBuffer):
        def append(self, *args, **kwargs) -> int:
            del args, kwargs
            return -1

    artifact_store = InMemoryArtifactStore()
    payload = b"shared"
    ref = artifact_store.put(payload)
    journal = RejectingJournal(artifact_store=artifact_store)
    sink = SessionJournalSink(
        event_bus=EventBus(),
        journal=journal,
        artifact_store=artifact_store,
        session_id="session-a",
        current_turn_id=lambda turn_id=None: turn_id,
    )

    assert sink.append_record(name="artifact_record", input_bytes=payload) == -1
    assert artifact_store.has(ref)


@pytest.mark.asyncio
async def test_journal_sink_skips_artifact_puts_when_journal_is_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(artifact_store=artifact_store)
    journal._degraded = True
    sink = SessionJournalSink(
        event_bus=EventBus(),
        journal=journal,
        artifact_store=artifact_store,
        session_id="session-a",
        current_turn_id=lambda turn_id=None: turn_id,
    )
    put_calls: list[bytes] = []

    def unexpected_put(
        payload: bytes,
        *,
        artifact_class: str = "debug_verbose",
    ) -> str:
        del artifact_class
        put_calls.append(payload)
        return "unexpected"

    monkeypatch.setattr(artifact_store, "put", unexpected_put)

    payloads = (b"sync-input", b"sync-output", b"async-input", b"async-output")
    sequence = sink.append_record(
        name="artifact_record",
        input_bytes=payloads[0],
        output_bytes=payloads[1],
    )
    await sink.append_record_async(
        name="artifact_record",
        input_bytes=payloads[2],
        output_bytes=payloads[3],
    )

    assert sequence == -1
    assert put_calls == []
    assert all(not artifact_store.has(hashlib.sha256(payload).hexdigest()) for payload in payloads)


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
async def test_session_journal_stays_read_only_after_stop() -> None:
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
