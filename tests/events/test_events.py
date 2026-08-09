import asyncio
import functools
import inspect
import logging

import pytest

import easycat
import easycat.events as events_module
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import (
    ALL_EVENTS,
    DTMF,
    TELEPHONY_EVENTS,
    TRANSPORT_EVENTS,
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
    TransportAudioDelivered,
    TransportDegraded,
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

_SHARED_CHUNK = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)


def _is_shared_chunk(value: object) -> bool:
    return value is _SHARED_CHUNK


def _markers_len_1(value: object) -> bool:
    return len(value) == 1  # type: ignore[arg-type]


def test_agent_delta_can_describe_an_indexed_replacement() -> None:
    event = AgentDelta(text="correct", part_index=2, replacement=True)

    assert event.part_index == 2
    assert event.replacement is True


@pytest.mark.parametrize(
    ("make_event", "field_checks"),
    [
        pytest.param(
            lambda: AudioIn(chunk=_SHARED_CHUNK), [("chunk", _is_shared_chunk)], id="audio-in"
        ),
        pytest.param(lambda: VADStartSpeaking(), [], id="vad-start-speaking"),
        pytest.param(lambda: VADStopSpeaking(), [], id="vad-stop-speaking"),
        pytest.param(lambda: STTPartial(text="hel"), [("text", "hel")], id="stt-partial"),
        pytest.param(lambda: STTFinal(text="hello"), [("text", "hello")], id="stt-final"),
        pytest.param(lambda: AgentRequestStarted(), [], id="agent-request-started"),
        pytest.param(lambda: AgentDelta(text="Hi"), [("text", "Hi")], id="agent-delta"),
        pytest.param(
            lambda: AgentFinal(text="Hi there!"), [("text", "Hi there!")], id="agent-final"
        ),
        pytest.param(
            lambda: TTSAudio(chunk=_SHARED_CHUNK), [("chunk", _is_shared_chunk)], id="tts-audio"
        ),
        pytest.param(
            lambda: TTSMarkers(markers=[{"word": "hello", "offset": 0.0}]),
            [("markers", _markers_len_1)],
            id="tts-markers",
        ),
        pytest.param(lambda: BotStartedSpeaking(), [], id="bot-started-speaking"),
        pytest.param(lambda: BotStoppedSpeaking(), [], id="bot-stopped-speaking"),
        pytest.param(lambda: TurnStarted(), [], id="turn-started"),
        pytest.param(lambda: TurnEnded(), [], id="turn-ended"),
        pytest.param(lambda: Interruption(), [], id="interruption"),
        pytest.param(
            lambda: PlaybackMarkAck(mark_name="m1"),
            [("mark_name", "m1")],
            id="playback-mark-ack",
        ),
        pytest.param(
            lambda: ToolCallStarted(tool_name="search", call_id="abc123"),
            [("tool_name", "search"), ("call_id", "abc123")],
            id="tool-call-started",
        ),
        pytest.param(
            lambda: ToolCallDelta(call_id="abc123", delta="partial"),
            [("delta", "partial")],
            id="tool-call-delta",
        ),
        pytest.param(
            lambda: ToolCallResult(call_id="abc123", result="done"),
            [("result", "done")],
            id="tool-call-result",
        ),
        pytest.param(
            lambda: ReconnectAttempt(provider="deepgram", attempt=1),
            [("provider", "deepgram"), ("attempt", 1)],
            id="reconnect-attempt",
        ),
        pytest.param(
            lambda: ReconnectSuccess(provider="deepgram"),
            [("provider", "deepgram")],
            id="reconnect-success",
        ),
        pytest.param(
            lambda: ReconnectFailure(provider="deepgram", error="timeout"),
            [("error", "timeout")],
            id="reconnect-failure",
        ),
        pytest.param(lambda: DTMF(digit="5"), [("digit", "5")], id="dtmf"),
        pytest.param(
            lambda: DTMFAggregated(sequence="1234#"),
            [("sequence", "1234#")],
            id="dtmf-aggregated",
        ),
        pytest.param(
            lambda: VoicemailDetected(result="machine"),
            [("result", "machine"), ("call_sid", "")],
            id="voicemail-detected-default-call-sid",
        ),
        pytest.param(
            lambda: VoicemailDetected(result="machine", call_sid="CA123"),
            [("result", "machine"), ("call_sid", "CA123")],
            id="voicemail-detected-explicit-call-sid",
        ),
    ],
)
def test_event_construction_and_fields(make_event, field_checks):
    event = make_event()
    assert event.timestamp > 0
    for attr, expected in field_checks:
        actual = getattr(event, attr)
        assert expected(actual) if callable(expected) else actual == expected


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


def test_all_events_contains_every_concrete_event_type():
    concrete_event_types = {
        value
        for value in vars(events_module).values()
        if inspect.isclass(value)
        and value.__module__ == events_module.__name__
        and value is not Event
        and issubclass(value, Event)
    }

    assert set(ALL_EVENTS) == concrete_event_types
    assert len(ALL_EVENTS) == len(set(ALL_EVENTS))


def test_transport_events_are_in_the_public_event_catalog():
    assert TRANSPORT_EVENTS == (TransportAudioDelivered, TransportDegraded)
    assert all(event_type in ALL_EVENTS for event_type in TRANSPORT_EVENTS)


def test_top_level_exports_include_every_public_event():
    for event_type in ALL_EVENTS:
        assert getattr(easycat, event_type.__name__) is event_type


def test_error_event():
    exc = RuntimeError("boom")
    event = Error(exception=exc, stage=ErrorStage.STT)
    assert event.exception is exc
    assert event.stage == ErrorStage.STT
    assert "stage=stt" in exc.__notes__


def test_error_event_defaults_code_to_none_for_uncoded_exception():
    event = Error(exception=RuntimeError("boom"))
    assert event.code is None


def test_error_event_derives_code_from_exception():
    from easycat.timeouts import AgentTimeoutError

    event = Error(exception=AgentTimeoutError(timeout=1.0), stage=ErrorStage.AGENT)
    assert event.code == "EASYCAT_E302"
    assert "code=EASYCAT_E302" in event.exception.__notes__


def test_error_event_explicit_code_overrides_exception_code():
    from easycat.timeouts import AgentTimeoutError

    event = Error(exception=AgentTimeoutError(timeout=1.0), code="EASYCAT_E999")
    assert event.code == "EASYCAT_E999"
    assert "code=EASYCAT_E999" in event.exception.__notes__


def test_error_event_notes_include_context_and_dedupe_existing_notes():
    exc = RuntimeError("boom")
    exc.add_note("stage=tts")

    Error(
        exception=exc,
        stage=ErrorStage.TTS,
        provider="openai",
        code="EASYCAT_E999",
        session_id="s1",
        turn_id="t1",
    )

    assert exc.__notes__ == [
        "stage=tts",
        "provider=openai",
        "code=EASYCAT_E999",
        "session_id=s1",
        "turn_id=t1",
    ]


def test_error_event_notes_include_runtime_record_context():
    exc = RuntimeError("boom")

    Error(
        exception=exc,
        stage=ErrorStage.AGENT,
        elapsed_ms=12.3456,
        sequence=42,
        record_key="cp_42",
    )

    assert exc.__notes__ == [
        "stage=agent",
        "elapsed_ms=12.346",
        "sequence=42",
        "record_key=cp_42",
    ]


def test_error_event_dedupes_notes_by_key():
    exc = RuntimeError("boom")
    exc.add_note("elapsed_ms=1")

    Error(exception=exc, stage=ErrorStage.AGENT, elapsed_ms=2)

    assert exc.__notes__ == ["elapsed_ms=1", "stage=agent"]


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


def test_eventbus_subscribers_snapshot_excludes_globals_and_parents():
    bus = EventBus()

    def exact(event: STTFinal) -> None: ...

    def global_handler(event: Event) -> None: ...

    bus.subscribe(STTFinal, exact)
    bus.subscribe_all(global_handler)
    # A parent-class subscription must not leak into the exact-type snapshot.
    bus.subscribe(Event, global_handler)

    snapshot = bus.subscribers(STTFinal)
    assert snapshot == [exact]
    # Snapshot is a copy: later subscriptions do not mutate it.
    bus.subscribe(STTFinal, lambda e: None)
    assert snapshot == [exact]
    assert len(bus.subscribers(STTFinal)) == 2


def test_eventbus_subscribers_empty_for_unknown_type():
    bus = EventBus()
    assert bus.subscribers(STTFinal) == []
    # Querying must not create an empty bucket in the underlying defaultdict.
    assert STTFinal not in bus._handlers


@pytest.mark.asyncio
async def test_eventbus_reserved_handler_precedes_global_and_public_observers() -> None:
    bus = EventBus()
    order: list[str] = []
    reserved = bus._subscribe_reserved(STTFinal, lambda _event: order.append("reserved"))
    bus.subscribe_all(lambda _event: order.append("global"))
    public = bus.subscribe(STTFinal, lambda _event: order.append("public"))

    await bus.emit(STTFinal(text="first"))

    assert order == ["reserved", "global", "public"]
    assert len(bus.subscribers(STTFinal)) == 1

    reserved.unsubscribe()
    public.unsubscribe()
    order.clear()
    await bus.emit(STTFinal(text="second"))

    assert not reserved.active
    assert order == ["global"]


@pytest.mark.asyncio
async def test_eventbus_reserved_failure_prevents_public_observation() -> None:
    bus = EventBus(handler_error_policy="continue")
    observed: list[STTFinal] = []

    def fail(_event: STTFinal) -> None:
        raise RuntimeError("private lifecycle failed")

    bus._subscribe_reserved(STTFinal, fail)
    bus.subscribe_all(observed.append)
    bus.subscribe(STTFinal, observed.append)

    with pytest.raises(RuntimeError, match="private lifecycle failed"):
        await bus.emit(STTFinal(text="hidden"))

    assert observed == []


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
async def test_eventbus_subscription_token_unsubscribes_idempotently():
    bus = EventBus()
    received: list[STTFinal] = []

    token = bus.subscribe(STTFinal, received.append)
    assert token.active is True

    token.unsubscribe()
    token.unsubscribe()

    await bus.emit(STTFinal(text="hello"))

    assert token.active is False
    assert received == []


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
    assert bus.handler_failures == 1
    assert bus.last_handler_error is not None
    assert bus.last_handler_error.handler_name == "bad_handler"
    assert bus.last_handler_error.event_type == "STTFinal"
    assert isinstance(bus.last_handler_error.exception, RuntimeError)


def test_eventbus_rejects_unknown_handler_error_policy():
    with pytest.raises(ValueError, match="handler_error_policy"):
        EventBus(handler_error_policy="strict")  # type: ignore[arg-type]


def test_eventbus_rejects_negative_slow_handler_threshold():
    with pytest.raises(ValueError, match="slow_handler_threshold_s"):
        EventBus(slow_handler_threshold_s=-0.001)


@pytest.mark.parametrize(
    "threshold",
    [float("nan"), float("inf"), float("-inf"), True],
)
def test_eventbus_rejects_non_finite_slow_handler_threshold(threshold):
    with pytest.raises(ValueError, match="slow_handler_threshold_s"):
        EventBus(slow_handler_threshold_s=threshold)


@pytest.mark.asyncio
async def test_eventbus_raise_error_policy_stops_and_propagates():
    bus = EventBus(handler_error_policy="raise")
    received: list[STTFinal] = []

    def bad_handler(event: STTFinal) -> None:
        raise RuntimeError("handler error")

    def good_handler(event: STTFinal) -> None:
        received.append(event)

    bus.subscribe(STTFinal, bad_handler)
    bus.subscribe(STTFinal, good_handler)

    with pytest.raises(RuntimeError, match="handler error"):
        await bus.emit(STTFinal(text="hello"))

    assert received == []
    assert bus.handler_error_policy == "raise"
    assert bus.handler_failures == 1
    assert bus.last_handler_error is not None
    assert bus.last_handler_error.handler_name == "bad_handler"


@pytest.mark.asyncio
async def test_eventbus_raise_error_policy_propagates_async_handler_error():
    bus = EventBus(handler_error_policy="raise")
    received: list[STTFinal] = []

    async def bad_handler(event: STTFinal) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("async handler error")

    bus.subscribe(STTFinal, bad_handler)
    bus.subscribe(STTFinal, received.append)

    with pytest.raises(RuntimeError, match="async handler error"):
        await bus.emit(STTFinal(text="hello"))

    assert received == []
    assert bus.handler_failures == 1
    assert bus.last_handler_error is not None
    assert bus.last_handler_error.handler_name == "bad_handler"


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


@pytest.mark.asyncio
async def test_eventbus_subscribe_all_token_unsubscribes_idempotently():
    bus = EventBus()
    received: list[str] = []

    token = bus.subscribe_all(lambda event: received.append(type(event).__name__))
    token.unsubscribe()
    token.unsubscribe()

    await bus.emit(STTFinal(text="hello"))

    assert token.active is False
    assert received == []


@pytest.mark.asyncio
async def test_eventbus_warns_for_slow_handlers(caplog: pytest.LogCaptureFixture):
    bus = EventBus(slow_handler_threshold_s=0.0)

    def handler(event: STTFinal) -> None:
        return None

    bus.subscribe(STTFinal, handler)

    with caplog.at_level(logging.WARNING):
        await bus.emit(STTFinal(text="hello"))

    assert any(
        "Slow handler handler for event STTFinal took" in record.getMessage()
        for record in caplog.records
    )
