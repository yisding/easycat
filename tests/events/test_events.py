import asyncio
import functools
import logging

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import (
    DTMF,
    TELEPHONY_EVENTS,
    AgentDelta,
    AgentFinal,
    AgentRequestStarted,
    AudioIn,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    CallStateChanged,
    DTMFAggregated,
    Error,
    ErrorStage,
    Event,
    EventBus,
    Interruption,
    IVRAction,
    IVRActionType,
    PlaybackMarkAck,
    ReconnectAttempt,
    ReconnectFailure,
    ReconnectSuccess,
    ScreeningResponse,
    STTEvent,
    STTEventType,
    STTFinal,
    STTPartial,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TTSAudio,
    TTSEvent,
    TTSEventType,
    TTSMarkers,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
    VoicemailDetected,
)

# ── Event dataclass tests ─────────────────────────────────────────


def test_audio_in_event():
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    event = AudioIn(chunk=chunk)
    assert event.chunk is chunk
    assert event.timestamp > 0


def test_vad_events():
    start = VADStartSpeaking()
    stop = VADStopSpeaking()
    assert start.timestamp > 0
    assert stop.timestamp > 0


def test_stt_events():
    partial = STTPartial(text="hel")
    final = STTFinal(text="hello")
    assert partial.text == "hel"
    assert final.text == "hello"


def test_agent_events():
    started = AgentRequestStarted()
    delta = AgentDelta(text="Hi")
    final = AgentFinal(text="Hi there!")
    assert started.timestamp > 0
    assert delta.text == "Hi"
    assert final.text == "Hi there!"


def test_tts_events():
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    audio = TTSAudio(chunk=chunk)
    markers = TTSMarkers(markers=[{"word": "hello", "offset": 0.0}])
    assert audio.chunk is chunk
    assert len(markers.markers) == 1


def test_lifecycle_events():
    bot_start = BotStartedSpeaking()
    bot_stop = BotStoppedSpeaking()
    turn_start = TurnStarted()
    turn_end = TurnEnded()
    assert bot_start.timestamp > 0
    assert bot_stop.timestamp > 0
    assert turn_start.timestamp > 0
    assert turn_end.timestamp > 0


def test_interruption_event():
    event = Interruption()
    assert event.timestamp > 0


def test_playback_mark_ack_event():
    event = PlaybackMarkAck(mark_name="m1")
    assert event.mark_name == "m1"
    assert event.timestamp > 0


def test_tool_events():
    started = ToolCallStarted(tool_name="search", call_id="abc123")
    delta = ToolCallDelta(call_id="abc123", delta="partial")
    result = ToolCallResult(call_id="abc123", result="done")
    assert started.tool_name == "search"
    assert started.call_id == "abc123"
    assert delta.delta == "partial"
    assert result.result == "done"


def test_reconnect_events():
    attempt = ReconnectAttempt(provider="deepgram", attempt=1)
    success = ReconnectSuccess(provider="deepgram")
    failure = ReconnectFailure(provider="deepgram", error="timeout")
    assert attempt.provider == "deepgram"
    assert attempt.attempt == 1
    assert success.provider == "deepgram"
    assert failure.error == "timeout"


def test_dtmf_events():
    dtmf = DTMF(digit="5")
    agg = DTMFAggregated(sequence="1234#")
    assert dtmf.digit == "5"
    assert agg.sequence == "1234#"


def test_voicemail_detected():
    event = VoicemailDetected(result="machine")
    assert event.result == "machine"
    assert event.call_sid == ""


def test_voicemail_detected_accepts_call_sid():
    event = VoicemailDetected(result="machine", call_sid="CA123")
    assert event.call_sid == "CA123"


def test_telephony_helper_payloads_are_events():
    screening = ScreeningResponse(
        text="Hi, this is Sarah",
        mode="static",
        session_id="session-1",
        turn_id="turn-1",
        timestamp=123.0,
    )
    action = IVRAction(
        type=IVRActionType.DTMF,
        digits="1",
        menu_depth=2,
        session_id="session-1",
        turn_id="turn-1",
        timestamp=124.0,
    )
    changed = CallStateChanged(
        old="classifying",
        new="human",
        call_sid="CA123",
        session_id="session-1",
        turn_id="turn-1",
        timestamp=125.0,
    )

    assert isinstance(screening, Event)
    assert screening.session_id == "session-1"
    assert screening.turn_id == "turn-1"
    assert screening.timestamp == 123.0

    assert isinstance(action, Event)
    assert action.session_id == "session-1"
    assert action.turn_id == "turn-1"
    assert action.timestamp == 124.0

    assert isinstance(changed, Event)
    assert changed.call_sid == "CA123"
    assert changed.session_id == "session-1"
    assert changed.turn_id == "turn-1"
    assert changed.timestamp == 125.0


def test_telephony_events_include_helper_payloads():
    assert ScreeningResponse in TELEPHONY_EVENTS
    assert IVRAction in TELEPHONY_EVENTS
    assert CallStateChanged in TELEPHONY_EVENTS


def test_top_level_exports_include_call_lifecycle_events_and_groups():
    import easycat

    assert easycat.CallInitiated.__name__ == "CallInitiated"
    assert easycat.CallRinging.__name__ == "CallRinging"
    assert easycat.CallAnswered.__name__ == "CallAnswered"
    assert easycat.CallScreening.__name__ == "CallScreening"
    assert easycat.ScreeningTimedOut.__name__ == "ScreeningTimedOut"
    assert easycat.CallFailed.__name__ == "CallFailed"
    assert easycat.CallEnded.__name__ == "CallEnded"
    assert easycat.CallStateChanged is CallStateChanged
    assert easycat.ScreeningResponse is ScreeningResponse
    assert easycat.IVRAction is IVRAction
    assert easycat.TELEPHONY_EVENTS is TELEPHONY_EVENTS


def test_error_event():
    exc = RuntimeError("boom")
    event = Error(exception=exc, stage=ErrorStage.STT)
    assert event.exception is exc
    assert event.stage == ErrorStage.STT


def test_event_base_fields_are_keyword_only():
    ts = 123.456
    exc = RuntimeError("boom")

    stt_final = STTFinal("hello", timestamp=ts)
    agent_final = AgentFinal("hello", None, timestamp=ts)
    tool_started = ToolCallStarted("search", "c1", timestamp=ts)
    error = Error(exc, timestamp=ts)

    assert stt_final.timestamp == ts
    assert stt_final.session_id is None
    assert stt_final.turn_id is None

    assert agent_final.timestamp == ts
    assert agent_final.session_id is None
    assert agent_final.turn_id is None

    assert tool_started.timestamp == ts
    assert tool_started.session_id is None
    assert tool_started.turn_id is None

    assert error.timestamp == ts
    assert error.session_id is None
    assert error.turn_id is None


# ── Provider-scoped event tests ────────────────────────────────────


def test_stt_event_partial():
    event = STTEvent(type=STTEventType.PARTIAL, text="hel")
    assert event.type == STTEventType.PARTIAL
    assert event.text == "hel"


def test_stt_event_final():
    event = STTEvent(type=STTEventType.FINAL, text="hello")
    assert event.type == STTEventType.FINAL
    assert event.text == "hello"


def test_tts_event_audio():
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    event = TTSEvent(type=TTSEventType.AUDIO, audio=chunk)
    assert event.type == TTSEventType.AUDIO
    assert event.audio is chunk
    assert event.markers is None


def test_tts_event_markers():
    markers = [{"word": "hi", "offset": 0.0}]
    event = TTSEvent(type=TTSEventType.MARKERS, markers=markers)
    assert event.type == TTSEventType.MARKERS
    assert event.markers == markers
    assert event.audio is None


# ── EventBus tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_eventbus_subscribe_and_emit():
    bus = EventBus()
    received: list = []

    def handler(event: STTFinal) -> None:
        received.append(event)

    bus.subscribe(STTFinal, handler)
    event = STTFinal(text="hello")
    await bus.emit(event)

    assert len(received) == 1
    assert received[0].text == "hello"


@pytest.mark.asyncio
async def test_eventbus_async_handler():
    bus = EventBus()
    received: list = []

    async def handler(event: STTFinal) -> None:
        await asyncio.sleep(0)
        received.append(event)

    bus.subscribe(STTFinal, handler)
    await bus.emit(STTFinal(text="async hello"))

    assert len(received) == 1
    assert received[0].text == "async hello"


@pytest.mark.asyncio
async def test_eventbus_multiple_handlers():
    bus = EventBus()
    results: list[str] = []

    bus.subscribe(STTFinal, lambda e: results.append("a"))
    bus.subscribe(STTFinal, lambda e: results.append("b"))

    await bus.emit(STTFinal(text="x"))
    assert results == ["a", "b"]


@pytest.mark.asyncio
async def test_eventbus_no_cross_event_dispatch():
    bus = EventBus()
    received: list = []

    bus.subscribe(STTFinal, lambda e: received.append(e))
    await bus.emit(STTPartial(text="partial"))

    assert len(received) == 0


@pytest.mark.asyncio
async def test_eventbus_unsubscribe():
    bus = EventBus()
    received: list = []

    def handler(event: STTFinal) -> None:
        received.append(event)

    bus.subscribe(STTFinal, handler)
    bus.unsubscribe(STTFinal, handler)

    await bus.emit(STTFinal(text="hello"))
    assert len(received) == 0


@pytest.mark.asyncio
async def test_eventbus_handler_error_does_not_stop_others():
    bus = EventBus()
    received: list = []

    def bad_handler(event: STTFinal) -> None:
        raise RuntimeError("handler error")

    def good_handler(event: STTFinal) -> None:
        received.append(event)

    bus.subscribe(STTFinal, bad_handler)
    bus.subscribe(STTFinal, good_handler)

    await bus.emit(STTFinal(text="hello"))
    assert len(received) == 1


@pytest.mark.asyncio
async def test_eventbus_handler_error_with_partial_logs_and_continues(
    caplog: pytest.LogCaptureFixture,
):
    bus = EventBus()
    received: list[STTFinal] = []

    def boom(event: STTFinal) -> None:
        raise RuntimeError("handler error")

    def good_handler(event: STTFinal) -> None:
        received.append(event)

    bus.subscribe(STTFinal, functools.partial(boom))
    bus.subscribe(STTFinal, good_handler)

    with caplog.at_level(logging.ERROR):
        await bus.emit(STTFinal(text="hello"))

    assert len(received) == 1
    assert any(
        "Error in handler boom for event STTFinal" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_eventbus_subscribe_all_receives_multiple_event_types():
    bus = EventBus()
    received: list[str] = []

    def handler(event: object) -> None:
        received.append(type(event).__name__)

    bus.subscribe_all(handler)
    await bus.emit(STTPartial(text="p"))
    await bus.emit(STTFinal(text="f"))

    assert received == ["STTPartial", "STTFinal"]


@pytest.mark.asyncio
async def test_eventbus_event_subscriber_receives_telephony_helper_events():
    bus = EventBus()
    received: list[Event] = []

    bus.subscribe(Event, received.append)

    screening = ScreeningResponse(text="Hi", mode="static")
    action = IVRAction(type=IVRActionType.WAIT)
    changed = CallStateChanged(old="classifying", new="human", call_sid="CA123")
    await bus.emit(screening)
    await bus.emit(action)
    await bus.emit(changed)

    assert received == [screening, action, changed]


@pytest.mark.asyncio
async def test_eventbus_unsubscribe_all():
    bus = EventBus()
    received: list[str] = []

    def handler(event: object) -> None:
        received.append(type(event).__name__)

    bus.subscribe_all(handler)
    bus.unsubscribe_all(handler)
    await bus.emit(STTFinal(text="hello"))

    assert not received
