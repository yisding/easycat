"""Tests for playback mark tracking and QueuePlaybackTransport integration."""

from __future__ import annotations

import asyncio

import pytest

from easycat import Session, SessionConfig, create_session
from easycat.events import AgentFinal, PlaybackMarkAck
from easycat.turn_manager import TurnManagerConfig

from .harness import (
    EventCollector,
    IdentityNoiseReducer,
    QueuePlaybackTransport,
    RecordingTTS,
    ScriptedSTT,
    ScriptedVAD,
    make_chunk,
    make_test_config,
    patch_provider_factories,
)

pytestmark = pytest.mark.integration_local

FAST_TURN = TurnManagerConfig(end_of_turn_silence_ms=1)


# ── Test agents ──────────────────────────────────────────────────────


class UpperAgent:
    async def run(self, text: str) -> str:
        return text.upper()


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_playback_transport_full_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """QueuePlaybackTransport should work as a drop-in transport for a full turn.

    Since it extends QueueTransport and adds send_playback_mark, the session
    should detect it as a PlaybackAckTransport and complete a turn normally.
    """
    transport = QueuePlaybackTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640, 640))
    vad = ScriptedVAD(["start", "stop"])

    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)
    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)

    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())

        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert agent_final.text == "HELLO"

        # The session should have detected our transport as PlaybackAckTransport.
        assert session._playback_ack_transport is transport
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_replay_gated_audio_enqueues_chunks() -> None:
    """replay_gated_audio should enqueue buffered TTS chunks to the transport.

    Constructs a Session directly via SessionConfig with an audio gate that
    initially buffers, then replays the gated audio.
    """
    from easycat.events import TTSAudio

    transport = QueuePlaybackTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])

    gate_open = False

    def audio_gate() -> bool:
        return not gate_open

    session = Session(
        SessionConfig(
            stt=stt,
            tts=tts,
            vad=vad,
            noise_reducer=IdentityNoiseReducer(),
            transport=transport,
            agent=UpperAgent(),
            turn_manager_config=FAST_TURN,
            audio_gate=audio_gate,
        )
    )

    await session.start()
    try:
        # Push audio to trigger a turn.
        await transport.push_audio(make_chunk(), make_chunk())

        # Wait for the agent to produce output (TTS chunks get gated).
        await asyncio.sleep(0.5)

        # No audio should have been sent to transport because the gate is closed.
        sent_before = len(transport.sent)

        # Open the gate and replay the buffered audio.
        gate_open = True
        gated_events = [TTSAudio(chunk=make_chunk(640)), TTSAudio(chunk=make_chunk(640))]
        await session.replay_gated_audio(gated_events)

        # Give the outbound drainer time to send the replayed chunks.
        await asyncio.sleep(0.3)

        assert len(transport.sent) > sent_before
    finally:
        await transport.finish_input()
        await session.stop()


@pytest.mark.asyncio
async def test_queue_playback_transport_records_marks() -> None:
    """QueuePlaybackTransport.send_playback_mark should record marks sequentially."""
    transport = QueuePlaybackTransport()

    # First call — auto-generated name.
    mark1 = await transport.send_playback_mark()
    assert mark1 == "mark_1"
    assert transport.playback_marks == ["mark_1"]

    # Second call — auto-generated name.
    mark2 = await transport.send_playback_mark()
    assert mark2 == "mark_2"
    assert transport.playback_marks == ["mark_1", "mark_2"]

    # Third call — explicit name.
    mark3 = await transport.send_playback_mark(name="custom_mark")
    assert mark3 == "custom_mark"
    assert transport.playback_marks == ["mark_1", "mark_2", "custom_mark"]


@pytest.mark.asyncio
async def test_playback_mark_ack_event_received(monkeypatch: pytest.MonkeyPatch) -> None:
    """PlaybackMarkAck emitted on the event bus should be processed by the session handler."""
    transport = QueuePlaybackTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])

    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)
    config = make_test_config(transport=transport, agent=UpperAgent(), turn_taking=FAST_TURN)
    session = create_session(config)

    ack_events: list[PlaybackMarkAck] = []
    session.event_bus.subscribe(PlaybackMarkAck, lambda e: ack_events.append(e))

    await session.start()
    try:
        # Simulate a mark being tracked in the session's internal state.
        session._turn_playback_mark_to_bytes["test_mark"] = 1000

        # Emit a PlaybackMarkAck as if the transport reported playback progress.
        await session.event_bus.emit(PlaybackMarkAck(mark_name="test_mark"))

        # The handler should have consumed the mark from the tracking dict.
        assert "test_mark" not in session._turn_playback_mark_to_bytes

        # The ack log should have an entry.
        assert len(session._turn_playback_ack_log) == 1
        _, acked_bytes = session._turn_playback_ack_log[0]
        assert acked_bytes == 1000
    finally:
        await transport.finish_input()
        await session.stop()
