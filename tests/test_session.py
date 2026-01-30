"""Tests for Session lifecycle, cancellation, and pipeline orchestration."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import (
    AgentFinal,
    AudioIn,
    Event,
    STTFinal,
    TTSAudio,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.session import Session, SessionConfig, TurnState
from easycat.stubs import NoopNoiseReducer

# ── Test helpers ───────────────────────────────────────────────────


def _make_chunk(n_bytes: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n_bytes), format=PCM16_MONO_16K)


class FakeTransport:
    """Transport that yields a fixed sequence of audio chunks, then stops."""

    def __init__(self, chunks: list[AudioChunk] | None = None) -> None:
        self.chunks = chunks or []
        self.sent: list[AudioChunk] = []
        self.connected = False
        self.disconnected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for chunk in self.chunks:
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk)


class FakeVAD:
    """VAD that emits start on first chunk, stop on second chunk."""

    def __init__(self) -> None:
        self._call_count = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        self._call_count += 1
        if self._call_count == 1:
            yield VADStartSpeaking()
        elif self._call_count == 2:
            yield VADStopSpeaking()

    def configure(self, **kwargs: object) -> None:
        pass


class FakeSTT:
    """STT that returns a fixed transcript on end_stream."""

    def __init__(self, transcript: str = "hello world") -> None:
        self._transcript = transcript
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()

    async def start_stream(self) -> AsyncIterator[Event]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        if self._transcript:
            await self._queue.put(STTFinal(text=self._transcript))
        await self._queue.put(None)  # sentinel to end iteration


class FakeAgent:
    """Agent that uppercases input text."""

    async def run(self, text: str) -> str:
        return text.upper()


class FakeTTS:
    """TTS that returns a single audio chunk for any input."""

    async def synthesize(self, text: str) -> AsyncIterator[AudioChunk]:
        yield _make_chunk()

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


# ── Session lifecycle tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_default_construction():
    session = Session()
    assert session.turn_state == TurnState.IDLE
    assert not session.is_running


@pytest.mark.asyncio
async def test_session_start_and_stop():
    transport = FakeTransport()
    config = SessionConfig(transport=transport)
    session = Session(config)

    await session.start()
    assert session.is_running
    assert transport.connected

    await session.stop()
    assert not session.is_running
    assert transport.disconnected
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_session_shutdown():
    transport = FakeTransport()
    config = SessionConfig(transport=transport)
    session = Session(config)

    await session.start()
    await session.shutdown()

    assert not session.is_running
    assert transport.disconnected


@pytest.mark.asyncio
async def test_session_start_idempotent():
    session = Session()
    await session.start()
    await session.start()  # second start is a no-op
    assert session.is_running
    await session.stop()


@pytest.mark.asyncio
async def test_session_stop_idempotent():
    session = Session()
    await session.stop()  # stop before start is a no-op
    assert not session.is_running


# ── Cancellation tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_turn_resets_state():
    session = Session()
    session._turn_state = TurnState.LISTENING
    await session.cancel_turn()
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_cancel_tts_playback_resets_state():
    session = Session()
    session._turn_state = TurnState.BOT_SPEAKING
    await session.cancel_tts_playback()
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_reset_state():
    session = Session()
    session._turn_state = TurnState.PROCESSING
    await session.reset_state()
    assert session.turn_state == TurnState.IDLE


# ── Pipeline orchestration tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_emits_audio_in_events():
    chunks = [_make_chunk(), _make_chunk()]
    transport = FakeTransport(chunks=chunks)
    config = SessionConfig(transport=transport, enable_vad=False)
    session = Session(config)

    received: list[AudioIn] = []
    session.event_bus.subscribe(AudioIn, lambda e: received.append(e))

    await session.start()
    # Let the pipeline process
    await asyncio.sleep(0.05)
    await session.stop()

    assert len(received) == 2


@pytest.mark.asyncio
async def test_pipeline_noise_reduction():
    chunk = _make_chunk()
    transport = FakeTransport(chunks=[chunk])

    class TrackingNoiseReducer:
        def __init__(self) -> None:
            self.processed = False

        async def process(self, c: AudioChunk) -> AudioChunk:
            self.processed = True
            return c

    nr = TrackingNoiseReducer()
    config = SessionConfig(transport=transport, noise_reducer=nr, enable_vad=False)
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.05)
    await session.stop()

    assert nr.processed


@pytest.mark.asyncio
async def test_pipeline_full_turn_with_stubs():
    """Full pipeline: audio in -> VAD start -> STT -> agent -> TTS -> audio out."""
    chunks = [_make_chunk(), _make_chunk()]
    transport = FakeTransport(chunks=chunks)
    vad = FakeVAD()
    stt = FakeSTT(transcript="hello")
    agent = FakeAgent()
    tts = FakeTTS()

    config = SessionConfig(
        transport=transport,
        vad=vad,
        stt=stt,
        agent=agent,
        tts=tts,
        noise_reducer=NoopNoiseReducer(),
    )
    session = Session(config)

    events_received: list[Event] = []
    session.event_bus.subscribe(AudioIn, lambda e: events_received.append(e))
    session.event_bus.subscribe(VADStartSpeaking, lambda e: events_received.append(e))
    session.event_bus.subscribe(VADStopSpeaking, lambda e: events_received.append(e))
    session.event_bus.subscribe(STTFinal, lambda e: events_received.append(e))
    session.event_bus.subscribe(AgentFinal, lambda e: events_received.append(e))
    session.event_bus.subscribe(TTSAudio, lambda e: events_received.append(e))

    await session.start()
    await asyncio.sleep(0.2)
    await session.stop()

    # Verify the event sequence
    event_types = [type(e).__name__ for e in events_received]
    assert "AudioIn" in event_types
    assert "VADStartSpeaking" in event_types
    assert "VADStopSpeaking" in event_types
    assert "STTFinal" in event_types
    assert "AgentFinal" in event_types
    assert "TTSAudio" in event_types

    # Verify agent uppercased the transcript
    agent_finals = [e for e in events_received if isinstance(e, AgentFinal)]
    assert len(agent_finals) == 1
    assert agent_finals[0].text == "HELLO"

    # Verify transport received TTS audio
    assert len(transport.sent) > 0


@pytest.mark.asyncio
async def test_pipeline_skips_empty_transcript():
    """If STT returns empty text, agent and TTS should not run."""
    chunks = [_make_chunk(), _make_chunk()]
    transport = FakeTransport(chunks=chunks)
    vad = FakeVAD()
    stt = FakeSTT(transcript="")

    agent_ran = False

    class TrackingAgent:
        async def run(self, text: str) -> str:
            nonlocal agent_ran
            agent_ran = True
            return text

    config = SessionConfig(
        transport=transport,
        vad=vad,
        stt=stt,
        agent=TrackingAgent(),
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.15)
    await session.stop()

    assert not agent_ran


@pytest.mark.asyncio
async def test_session_event_bus_accessible():
    session = Session()
    assert session.event_bus is not None
    # Can subscribe
    received: list = []
    session.event_bus.subscribe(STTFinal, lambda e: received.append(e))
    await session.event_bus.emit(STTFinal(text="test"))
    assert len(received) == 1
