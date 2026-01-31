"""Tests for Session lifecycle, cancellation, pipeline, and CancelToken."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    AgentFinal,
    AudioIn,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Event,
    Interruption,
    STTEvent,
    STTEventType,
    STTFinal,
    TTSAudio,
    TTSEvent,
    TTSEventType,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.session import Session, SessionConfig, TurnState
from easycat.stubs import NoopNoiseReducer

# ── Test helpers ───────────────────────────────────────────────────


def _make_chunk(n_bytes: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n_bytes), format=PCM16_MONO_16K)


class FakeTransport:
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
    """STT that uses provider-scoped STTEvent via events() iterator."""

    def __init__(self, transcript: str = "hello world") -> None:
        self._transcript = transcript
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        if self._transcript:
            await self._queue.put(STTEvent(type=STTEventType.FINAL, text=self._transcript))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event


class FakeAgent:
    async def run(self, text: str) -> str:
        return text.upper()


class FakeTTS:
    """TTS that uses provider-scoped TTSEvent."""

    async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(
            type=TTSEventType.AUDIO,
            audio=_make_chunk(),
        )

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


# ── CancelToken tests ──────────────────────────────────────────────


def test_cancel_token_initial_state():
    token = CancelToken()
    assert not token.is_cancelled


def test_cancel_token_cancel():
    token = CancelToken()
    token.cancel()
    assert token.is_cancelled


@pytest.mark.asyncio
async def test_cancel_token_wait():
    token = CancelToken()

    async def cancel_later():
        await asyncio.sleep(0.01)
        token.cancel()

    asyncio.create_task(cancel_later())
    await token.wait()
    assert token.is_cancelled


# ── Session lifecycle tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_default_construction():
    session = Session()
    assert session.turn_state == TurnState.IDLE
    assert not session.is_running
    assert session.cancel_token is None


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
    await session.start()
    assert session.is_running
    await session.stop()


@pytest.mark.asyncio
async def test_session_stop_idempotent():
    session = Session()
    await session.stop()
    assert not session.is_running


# ── Cancellation tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_turn_resets_state():
    session = Session()
    session._turn_state = TurnState.LISTENING
    session._cancel_token = CancelToken()
    await session.cancel_turn()
    assert session.turn_state == TurnState.IDLE
    assert session._cancel_token.is_cancelled


@pytest.mark.asyncio
async def test_cancel_turn_barge_in_emits_interruption():
    session = Session()
    session._turn_state = TurnState.BOT_SPEAKING
    session._cancel_token = CancelToken()

    received: list = []
    session.event_bus.subscribe(Interruption, lambda e: received.append(e))

    await session.cancel_turn(barge_in=True)
    assert len(received) == 1
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_cancel_tts_playback_resets_state():
    session = Session()
    session._turn_state = TurnState.BOT_SPEAKING
    session._cancel_token = CancelToken()
    await session.cancel_tts_playback()
    assert session.turn_state == TurnState.IDLE
    assert session._cancel_token.is_cancelled


@pytest.mark.asyncio
async def test_reset_state():
    session = Session()
    session._turn_state = TurnState.PROCESSING
    session._cancel_token = CancelToken()
    await session.reset_state()
    assert session.turn_state == TurnState.IDLE
    assert session._cancel_token.is_cancelled


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
async def test_pipeline_full_turn_with_provider_events():
    """Full pipeline using provider-scoped events (STTEvent, TTSEvent)."""
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
    for et in [
        AudioIn,
        VADStartSpeaking,
        VADStopSpeaking,
        TurnStarted,
        STTFinal,
        AgentFinal,
        BotStartedSpeaking,
        TTSAudio,
        BotStoppedSpeaking,
        TurnEnded,
    ]:
        session.event_bus.subscribe(et, lambda e: events_received.append(e))

    await session.start()
    await asyncio.sleep(0.2)
    await session.stop()

    type_names = [type(e).__name__ for e in events_received]
    assert "AudioIn" in type_names
    assert "VADStartSpeaking" in type_names
    assert "VADStopSpeaking" in type_names
    assert "TurnStarted" in type_names
    assert "STTFinal" in type_names
    assert "AgentFinal" in type_names
    assert "BotStartedSpeaking" in type_names
    assert "TTSAudio" in type_names
    assert "BotStoppedSpeaking" in type_names
    assert "TurnEnded" in type_names

    # Verify agent uppercased the transcript
    agent_finals = [e for e in events_received if isinstance(e, AgentFinal)]
    assert len(agent_finals) == 1
    assert agent_finals[0].text == "HELLO"

    # Verify transport received TTS audio
    assert len(transport.sent) > 0


@pytest.mark.asyncio
async def test_pipeline_skips_empty_transcript():
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
    received: list = []
    session.event_bus.subscribe(STTFinal, lambda e: received.append(e))
    await session.event_bus.emit(STTFinal(text="test"))
    assert len(received) == 1
