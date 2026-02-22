"""Tests for Session lifecycle, cancellation, pipeline, and CancelToken."""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch

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
    PlaybackMarkAck,
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
from easycat.tracing import InMemoryTraceExporter, SpanStatus, Tracer
from easycat.turn_manager import TurnManagerConfig

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


class FakePlaybackAckTransport(FakeTransport):
    def __init__(self, chunks: list[AudioChunk] | None = None) -> None:
        super().__init__(chunks=chunks)
        self.playback_marks: list[str] = []

    async def send_playback_mark(self, name: str | None = None) -> str:
        mark_name = name or f"mark_{len(self.playback_marks) + 1}"
        self.playback_marks.append(mark_name)
        return mark_name


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


_FAST_TURN = TurnManagerConfig(end_of_turn_silence_ms=1)


def _full_config(**overrides) -> SessionConfig:
    """Build a SessionConfig with all required providers filled in."""
    defaults = dict(
        transport=FakeTransport(),
        vad=FakeVAD(),
        stt=FakeSTT(),
        agent=FakeAgent(),
        tts=FakeTTS(),
        noise_reducer=NoopNoiseReducer(),
        enable_noise_reduction=False,
    )
    defaults.update(overrides)
    return SessionConfig(**defaults)


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
    session = Session(_full_config())
    assert session.turn_state == TurnState.IDLE
    assert not session.is_running
    assert session.cancel_token is None


@pytest.mark.asyncio
async def test_session_start_and_stop():
    transport = FakeTransport()
    config = _full_config(transport=transport)
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
    config = _full_config(transport=transport)
    session = Session(config)

    await session.start()
    await session.shutdown()

    assert not session.is_running
    assert transport.disconnected


@pytest.mark.asyncio
async def test_session_start_idempotent():
    session = Session(_full_config())
    await session.start()
    await session.start()
    assert session.is_running
    await session.stop()


@pytest.mark.asyncio
async def test_session_stop_idempotent():
    session = Session(_full_config())
    await session.stop()
    assert not session.is_running


# ── Cancellation tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_turn_resets_state():
    session = Session(_full_config())
    session._turn_state = TurnState.LISTENING
    session._cancel_token = CancelToken()
    await session.cancel_turn()
    assert session.turn_state == TurnState.IDLE
    assert session._cancel_token.is_cancelled


@pytest.mark.asyncio
async def test_cancel_turn_barge_in_emits_interruption():
    session = Session(_full_config())
    session._turn_state = TurnState.BOT_SPEAKING
    session._cancel_token = CancelToken()

    received: list = []
    session.event_bus.subscribe(Interruption, lambda e: received.append(e))

    await session.cancel_turn(barge_in=True)
    assert len(received) == 1
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_cancel_tts_playback_resets_state():
    session = Session(_full_config())
    session._turn_state = TurnState.BOT_SPEAKING
    session._cancel_token = CancelToken()
    await session.cancel_tts_playback()
    assert session.turn_state == TurnState.IDLE
    assert session._cancel_token.is_cancelled


@pytest.mark.asyncio
async def test_reset_state():
    session = Session(_full_config())
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
    config = _full_config(transport=transport, enable_vad=False)
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
    config = _full_config(
        transport=transport, noise_reducer=nr, enable_vad=False, enable_noise_reduction=True
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.05)
    await session.stop()

    assert nr.processed


@pytest.mark.asyncio
async def test_pipeline_tracing_emits_noise_reduction_and_vad_spans():
    chunk = _make_chunk()
    transport = FakeTransport(chunks=[chunk])
    exporter = InMemoryTraceExporter()
    tracer = Tracer(exporter=exporter)

    class TrackingNoiseReducer:
        async def process(self, c: AudioChunk) -> AudioChunk:
            return c

    class SilentVAD:
        async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
            if False:
                yield VADStartSpeaking()

        def configure(self, **kwargs: object) -> None:
            pass

    config = _full_config(
        transport=transport,
        tracer=tracer,
        noise_reducer=TrackingNoiseReducer(),
        vad=SilentVAD(),
        enable_noise_reduction=True,
        enable_vad=True,
    )
    session = Session(config)
    session._spans.begin_turn()
    session._is_running = True

    await session._run_pipeline()

    span_names = [span.name for span in exporter.spans]
    assert Tracer.NOISE_REDUCTION in span_names
    assert Tracer.VAD in span_names


@pytest.mark.asyncio
async def test_run_basic_agent_cancellation_marks_agent_span_cancelled():
    exporter = InMemoryTraceExporter()
    tracer = Tracer(exporter=exporter)

    class BlockingAgent:
        async def run(self, text: str) -> str:
            await asyncio.Event().wait()
            return text

    session = Session(_full_config(agent=BlockingAgent(), tracer=tracer))
    session._spans.begin_turn()

    task = asyncio.create_task(session._run_basic_agent("hello", token=None))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    agent_spans = [span for span in exporter.spans if span.name == Tracer.AGENT]
    assert len(agent_spans) == 1
    assert agent_spans[0].status == SpanStatus.CANCELLED


@pytest.mark.asyncio
async def test_pipeline_full_turn_with_provider_events():
    """Full pipeline using provider-scoped events (STTEvent, TTSEvent)."""
    chunks = [_make_chunk(), _make_chunk()]
    transport = FakeTransport(chunks=chunks)
    vad = FakeVAD()
    stt = FakeSTT(transcript="hello")
    agent = FakeAgent()
    tts = FakeTTS()

    config = _full_config(
        transport=transport,
        vad=vad,
        stt=stt,
        agent=agent,
        tts=tts,
        turn_manager_config=_FAST_TURN,
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
    assert "TurnEnded" in type_names
    assert "STTFinal" in type_names
    assert "AgentFinal" in type_names
    assert "BotStartedSpeaking" in type_names
    assert "TTSAudio" in type_names
    assert "BotStoppedSpeaking" in type_names

    turn_end_idx = type_names.index("TurnEnded")
    bot_start_idx = type_names.index("BotStartedSpeaking")
    bot_stop_idx = type_names.index("BotStoppedSpeaking")
    assert turn_end_idx < bot_start_idx
    assert turn_end_idx < bot_stop_idx

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

    config = _full_config(
        transport=transport,
        vad=vad,
        stt=stt,
        agent=TrackingAgent(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.15)
    await session.stop()

    assert not agent_ran


@pytest.mark.asyncio
async def test_session_event_bus_accessible():
    session = Session(_full_config())
    assert session.event_bus is not None
    received: list = []
    session.event_bus.subscribe(STTFinal, lambda e: received.append(e))
    await session.event_bus.emit(STTFinal(text="test"))
    assert len(received) == 1


@pytest.mark.asyncio
async def test_playback_mark_names_are_unique_across_turns():
    transport = FakePlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    # Use a small interval so a single test chunk triggers a mark.
    session._playback_mark_bytes_interval = 1

    await session._outbound_queue.put(_make_chunk())
    await session._drain_outbound_audio()
    first_mark = transport.playback_marks[-1]

    session._is_running = True
    with patch.object(session, "_start_stt_event_task"):
        await session._on_turn_started(TurnStarted())
    session._is_running = False

    await session._outbound_queue.put(_make_chunk())
    await session._drain_outbound_audio()
    second_mark = transport.playback_marks[-1]

    assert first_mark != second_mark

    session._on_playback_mark_ack(PlaybackMarkAck(mark_name=first_mark))
    assert len(session._turn_playback_ack_log) == 0

    session._on_playback_mark_ack(PlaybackMarkAck(mark_name=second_mark))
    assert len(session._turn_playback_ack_log) == 1
    assert session._turn_playback_ack_log[0][1] == 320


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

    await session._outbound_queue.put(_make_chunk())
    await session._drain_outbound_audio()

    canonical_mark = transport.playback_marks[-1]
    session._on_playback_mark_ack(PlaybackMarkAck(mark_name=canonical_mark))

    assert len(session._turn_playback_ack_log) == 1
    assert session._turn_playback_ack_log[0][1] == 320


@pytest.mark.asyncio
async def test_trailing_playback_mark_emitted_while_session_running():
    transport = FakePlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    session._playback_mark_bytes_interval = 10_000

    await session.start()
    await session._outbound_queue.put(_make_chunk())

    for _ in range(20):
        if transport.playback_marks:
            break
        await asyncio.sleep(0.01)

    assert len(transport.playback_marks) == 1
    await session.stop()
