"""Session event bus and playback mark tests."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from easycat._turn_context import TurnContext
from easycat.audio_format import AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AudioOut,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    ErrorStage,
    EventBus,
    Interruption,
    PlaybackMarkAck,
    STTFinal,
    ToolCallResult,
    ToolCallStarted,
    TransportAudioDelivered,
    TurnEnded,
    TurnStarted,
)
from easycat.session._session import Session
from tests.session._session_core_helpers import (
    FakePlaybackAckTransport,
    ReportingTransport,
    _full_config,
    _make_chunk,
)


@pytest.mark.asyncio
async def test_session_event_bus_accessible():
    session = Session(_full_config())
    assert session.event_bus is not None
    received: list = []
    token = session.subscribe_event(STTFinal, lambda e: received.append(e))
    await session.event_bus.emit(STTFinal(text="test"))
    token.unsubscribe()
    await session.event_bus.emit(STTFinal(text="ignored"))

    assert token.active is False
    assert len(received) == 1


@pytest.mark.asyncio
async def test_session_subscribe_agent_events_helper():
    session = Session(_full_config())

    deltas: list[str] = []
    finals: list[str] = []
    tools_started: list[str] = []
    tools_results: list[str] = []

    registrations = session.subscribe_agent_events(
        on_delta=lambda e: deltas.append(e.text),
        on_final=lambda e: finals.append(e.text),
        on_tool_started=lambda e: tools_started.append(e.tool_name),
        on_tool_result=lambda e: tools_results.append(e.result),
    )

    await session.event_bus.emit(AgentDelta(text="chunk"))
    await session.event_bus.emit(AgentFinal(text="done"))
    await session.event_bus.emit(ToolCallStarted(tool_name="lookup", call_id="c1"))
    await session.event_bus.emit(ToolCallResult(call_id="c1", result="42"))

    assert deltas == ["chunk"]
    assert finals == ["done"]
    assert tools_started == ["lookup"]
    assert tools_results == ["42"]

    session.unsubscribe_handlers(registrations)
    await session.event_bus.emit(AgentFinal(text="done again"))
    assert deltas == ["chunk"]
    assert finals == ["done"]


@pytest.mark.asyncio
async def test_session_on_convenience_method():
    """session.on() subscribes with unwrapped callback arguments."""
    session = Session(_full_config())

    transcripts: list[str] = []
    responses: list[str] = []
    deltas: list[str] = []
    tools: list[tuple[str, str]] = []
    tool_results: list[tuple[str, str]] = []
    lifecycle: list[str] = []
    errors: list[tuple[BaseException, str]] = []

    registrations = session.on(
        user_transcript=lambda text: transcripts.append(text),
        agent_response=lambda text: responses.append(text),
        agent_delta=lambda text: deltas.append(text),
        tool_started=lambda name, cid: tools.append((name, cid)),
        tool_result=lambda cid, result: tool_results.append((cid, result)),
        turn_started=lambda: lifecycle.append("turn_started"),
        turn_ended=lambda: lifecycle.append("turn_ended"),
        bot_started_speaking=lambda: lifecycle.append("bot_started"),
        bot_stopped_speaking=lambda: lifecycle.append("bot_stopped"),
        interruption=lambda: lifecycle.append("interruption"),
        error=lambda exc, ctx: errors.append((exc, ctx)),
    )

    # Emit events and verify callbacks receive unwrapped args.
    await session.event_bus.emit(STTFinal(text="hello"))
    await session.event_bus.emit(AgentDelta(text="hi "))
    await session.event_bus.emit(AgentFinal(text="hi there"))
    await session.event_bus.emit(ToolCallStarted(tool_name="search", call_id="c1"))
    await session.event_bus.emit(ToolCallResult(call_id="c1", result="found"))
    await session.event_bus.emit(TurnStarted())
    await session.event_bus.emit(TurnEnded())
    await session.event_bus.emit(BotStartedSpeaking())
    await session.event_bus.emit(BotStoppedSpeaking())
    await session.event_bus.emit(Interruption())
    await session.event_bus.emit(Error(exception=ValueError("boom"), stage=ErrorStage.AGENT))

    assert transcripts == ["hello"]
    assert responses == ["hi there"]
    assert deltas == ["hi "]
    assert tools == [("search", "c1")]
    assert tool_results == [("c1", "found")]
    assert lifecycle == [
        "turn_started",
        "turn_ended",
        "bot_started",
        "bot_stopped",
        "interruption",
    ]
    assert len(errors) == 1
    assert str(errors[0][0]) == "boom"
    assert errors[0][1] == "agent"

    # Unsubscribe and verify no further callbacks.
    session.unsubscribe_handlers(registrations)
    await session.event_bus.emit(STTFinal(text="ignored"))
    assert transcripts == ["hello"]


@pytest.mark.asyncio
async def test_session_events_include_correlation_ids():
    session = Session(_full_config())
    seen: list[TurnStarted | Interruption] = []
    session.event_bus.subscribe(TurnStarted, lambda e: seen.append(e))
    session.event_bus.subscribe(Interruption, lambda e: seen.append(e))

    await session._emit(TurnStarted())
    await session.cancel_turn(barge_in=True)

    assert seen
    for event in seen:
        assert event.session_id == session.session_id


@pytest.mark.asyncio
async def test_playback_mark_ack_scoped_to_current_turn():
    """Playback marks are scoped to the current TurnContext.
    Each new turn has its own playback_mark_to_bytes map, so marks
    from a previous turn are naturally absent from the current turn's map."""
    transport = FakePlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    # Use a small interval so a single test chunk triggers a mark.
    session._playback_mark_bytes_interval = 1

    # ── First turn ──
    session._turn = TurnContext("turn-first", CancelToken())
    await session._outbound_queue.put(_make_chunk())
    await session._audio_router._drain_outbound_audio()
    first_turn_marks = list(session._turn.playback_mark_to_bytes.keys())
    assert len(first_turn_marks) == 1

    # ── Second turn (replaces the TurnContext) ──
    session._is_running = True
    with patch.object(session._stt_committer, "start_event_loop"):
        await session._turn_runner.on_turn_started(TurnStarted())
    session._is_running = False

    await session._outbound_queue.put(_make_chunk())
    await session._audio_router._drain_outbound_audio()
    second_turn_marks = list(session._turn.playback_mark_to_bytes.keys())
    assert len(second_turn_marks) == 1

    # Ack for the second turn's mark works.
    session._audio_router.on_playback_ack(PlaybackMarkAck(mark_name=second_turn_marks[0]))
    assert len(session._turn.playback_ack_log) == 1
    assert session._turn.playback_ack_log[0][1] == 320


@pytest.mark.asyncio
async def test_playback_mark_ack_tracks_transport_confirmed_name():
    class CanonicalizingPlaybackAckTransport(FakePlaybackAckTransport):
        async def send_playback_mark(self, name: str | None = None) -> str:
            requested_name = name or f"mark_{len(self.playback_marks) + 1}"
            canonical_name = f"canonical::{requested_name}"
            self.playback_marks.append(canonical_name)
            return canonical_name

    transport = CanonicalizingPlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    session._playback_mark_bytes_interval = 1
    session._turn = TurnContext("test-turn", CancelToken())

    await session._outbound_queue.put(_make_chunk())
    await session._audio_router._drain_outbound_audio()

    canonical_mark = transport.playback_marks[-1]
    session._audio_router.on_playback_ack(PlaybackMarkAck(mark_name=canonical_mark))

    assert len(session._turn.playback_ack_log) == 1
    assert session._turn.playback_ack_log[0][1] == 320


@pytest.mark.asyncio
async def test_trailing_playback_mark_emitted_while_session_running():
    transport = FakePlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    session._playback_mark_bytes_interval = 10_000

    await session.start()
    try:
        session._turn = TurnContext("test-turn", CancelToken())
        await session._outbound_queue.put(_make_chunk())

        await asyncio.wait_for(transport.playback_mark_sent.wait(), timeout=1.0)

        assert len(transport.playback_marks) == 1
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_trailing_playback_mark_flushed_after_speaking_turn_already_drained():
    transport = FakePlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    session._audio_router._playback_mark_bytes_interval = 10_000

    await session.start()
    try:
        turn = TurnContext("test-turn", CancelToken())
        session._turn = turn
        await session._turn_manager.bot_started_speaking()
        await session._outbound_queue.put(_make_chunk())

        def drained_without_mark() -> bool:
            return (
                turn.bytes_since_last_mark > 0
                and session._outbound_queue.empty()
                and session._audio_router._outbound_in_flight == 0
            )

        for _ in range(100):
            if drained_without_mark():
                break
            await asyncio.sleep(0.01)

        assert drained_without_mark()
        assert transport.playback_marks == []

        await session._tts_scheduler.finalize_speaking_turn(turn)

        assert len(transport.playback_marks) == 1
        assert turn.bytes_since_last_mark == 0
        assert list(turn.playback_mark_to_bytes.values()) == [320]
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_trailing_playback_mark_not_flushed_for_replaced_turn():
    transport = FakePlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    session._audio_router._playback_mark_bytes_interval = 10_000

    await session.start()
    try:
        old_turn = TurnContext("old-turn", CancelToken())
        session._turn = old_turn
        await session._turn_manager.bot_started_speaking()
        await session._outbound_queue.put(_make_chunk())

        def old_turn_drained_without_mark() -> bool:
            return (
                old_turn.bytes_since_last_mark > 0
                and session._outbound_queue.empty()
                and session._audio_router._outbound_in_flight == 0
            )

        for _ in range(100):
            if old_turn_drained_without_mark():
                break
            await asyncio.sleep(0.01)

        assert old_turn_drained_without_mark()
        assert transport.playback_marks == []

        new_turn = TurnContext("new-turn", CancelToken())
        session._turn = new_turn

        await session._tts_scheduler.finalize_speaking_turn(
            old_turn,
            turn_generation=old_turn.generation,
        )

        assert transport.playback_marks == []
        assert old_turn.bytes_since_last_mark == 320
        assert session._turn is new_turn
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_buffered_transport_delivery_is_counted_only_after_report() -> None:
    transport = ReportingTransport()
    session = Session(_full_config(transport=transport))
    session._turn = TurnContext("test-turn", CancelToken())
    seen: list[AudioOut] = []
    session.event_bus.subscribe(AudioOut, lambda event: seen.append(event))

    chunk = _make_chunk()
    await session._outbound_queue.put(chunk)
    await session._audio_router._drain_outbound_audio()

    assert transport.sent == [chunk]
    assert session._turn.audio_bytes_sent == 0
    assert seen == []

    await session.event_bus.emit(
        TransportAudioDelivered(
            chunk=chunk,
            turn_id=session._turn.id,
            turn_ref=session._turn,
        )
    )

    assert session._turn.audio_bytes_sent == len(chunk.data)
    assert len(seen) == 1
    assert seen[0].chunk is chunk
    assert seen[0].turn_id == "test-turn"


@pytest.mark.asyncio
async def test_buffered_transport_delivery_on_shared_bus_stays_session_scoped() -> None:
    bus = EventBus()
    victim = Session(
        _full_config(
            transport=ReportingTransport(),
            event_bus=bus,
            session_id="victim-session",
        )
    )
    other = Session(
        _full_config(
            transport=ReportingTransport(),
            event_bus=bus,
            session_id="other-session",
        )
    )
    victim._turn = TurnContext("victim-turn", CancelToken())

    seen: list[AudioOut] = []
    bus.subscribe(AudioOut, lambda event: seen.append(event))
    chunk = _make_chunk(16)

    await bus.emit(
        TransportAudioDelivered(
            chunk=chunk,
            turn_id=victim._turn.id,
            turn_ref=victim._turn,
        )
    )

    assert victim._turn.audio_bytes_sent == len(chunk.data)
    assert len(seen) == 1
    assert seen[0].session_id == "victim-session"
    assert seen[0].turn_id == "victim-turn"

    scoped_chunk = _make_chunk(24)
    await bus.emit(
        TransportAudioDelivered(
            chunk=scoped_chunk,
            session_id=victim.session_id,
            turn_id=victim._turn.id,
            turn_ref=victim._turn,
        )
    )

    assert victim._turn.audio_bytes_sent == len(chunk.data) + len(scoped_chunk.data)
    assert len(seen) == 2
    assert {event.session_id for event in seen} == {"victim-session"}
    assert all(event.turn_id == "victim-turn" for event in seen)
    assert other.session_id not in {event.session_id for event in seen}


@pytest.mark.asyncio
async def test_buffered_transport_delivery_unscoped_falls_back_to_active_turn() -> None:
    # A custom reporting transport on a private (single-session) bus may emit
    # a bare TransportAudioDelivered with no session_id/turn_id/turn_ref. That
    # fully-unscoped callback must still count bytes against the active turn
    # and emit exactly one AudioOut — otherwise older reporting transports
    # regress to silently dropping buffered-playback accounting.
    session = Session(_full_config(transport=ReportingTransport()))
    session._turn = TurnContext("test-turn", CancelToken())

    seen: list[AudioOut] = []
    session.event_bus.subscribe(AudioOut, lambda event: seen.append(event))

    chunk = _make_chunk(16)
    await session.event_bus.emit(TransportAudioDelivered(chunk=chunk))

    assert session._turn.audio_bytes_sent == len(chunk.data)
    assert len(seen) == 1
    assert seen[0].chunk is chunk
    assert seen[0].turn_id == "test-turn"


@pytest.mark.asyncio
async def test_buffered_transport_delivery_unscoped_is_dropped_on_shared_bus() -> None:
    bus = EventBus()
    victim = Session(
        _full_config(
            transport=ReportingTransport(),
            event_bus=bus,
            session_id="victim-session",
        )
    )
    other = Session(
        _full_config(
            transport=ReportingTransport(),
            event_bus=bus,
            session_id="other-session",
        )
    )
    victim._turn = TurnContext("victim-turn", CancelToken())
    other._turn = TurnContext("other-turn", CancelToken())

    seen: list[AudioOut] = []
    bus.subscribe(AudioOut, lambda event: seen.append(event))

    await bus.emit(TransportAudioDelivered(chunk=_make_chunk(16)))

    assert victim._turn.audio_bytes_sent == 0
    assert other._turn.audio_bytes_sent == 0
    assert seen == []


@pytest.mark.asyncio
async def test_buffered_transport_delivery_unscoped_idle_session_is_dropped() -> None:
    session = Session(_full_config(transport=ReportingTransport()))

    seen: list[AudioOut] = []
    session.event_bus.subscribe(AudioOut, lambda event: seen.append(event))

    await session.event_bus.emit(TransportAudioDelivered(chunk=_make_chunk(16)))

    assert seen == []


@pytest.mark.asyncio
async def test_failed_send_does_not_emit_audio_out_or_count_bytes() -> None:
    class RejectingTransport(FakePlaybackAckTransport):
        async def send_audio(self, chunk: AudioChunk) -> bool:
            return False

    transport = RejectingTransport()
    session = Session(_full_config(transport=transport))
    session._playback_mark_bytes_interval = 1
    session._turn = TurnContext("test-turn", CancelToken())
    seen: list[AudioOut] = []
    session.event_bus.subscribe(AudioOut, lambda event: seen.append(event))

    await session._outbound_queue.put(_make_chunk())
    await session._audio_router._drain_outbound_audio()

    assert session._turn.audio_bytes_sent == 0
    assert session._turn.bytes_since_last_mark == 0
    assert transport.playback_marks == []
    assert seen == []
