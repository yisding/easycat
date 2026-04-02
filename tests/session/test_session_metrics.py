"""Tests verifying that metrics are recorded during session turns."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.agent_runner import AgentStreamEvent, AgentStreamEventType
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    Error,
    Event,
    STTEvent,
    STTEventType,
    TTSEvent,
    TTSEventType,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.metrics import (
    AGENT_LATENCY,
    ERRORS,
    STT_LATENCY,
    TTS_TTFB,
    TURN_E2E,
    InMemoryMetrics,
)
from easycat.session import Session, SessionConfig
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig

_FAST_TURN = TurnManagerConfig(end_of_turn_silence_ms=1)


def _chunk(n: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n), format=PCM16_MONO_16K)


# ── Fakes ─────────────────────────────────────────────────────────


class FakeTransport:
    def __init__(self, chunks: list[AudioChunk] | None = None) -> None:
        self.chunks = chunks or []
        self.sent: list[AudioChunk] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for chunk in self.chunks:
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk)

    async def clear_audio(self) -> None:
        pass


class FakeVAD:
    def __init__(self) -> None:
        self._n = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        self._n += 1
        if self._n == 1:
            yield VADStartSpeaking()
        elif self._n == 2:
            yield VADStopSpeaking()

    def configure(self, **kwargs: object) -> None:
        pass


class FakeSTT:
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


class FakeTTS:
    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class FakeNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


class FakeAgent:
    async def run(self, text: str) -> str:
        return text.upper()


class FailingAgent:
    async def run(self, text: str) -> str:
        raise RuntimeError("agent broke")


class StreamingAgent:
    async def run(self, text: str) -> str:
        return text.upper()

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        words = text.upper().split()
        for i, word in enumerate(words):
            delta = word if i == 0 else f" {word}"
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=delta)
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text=" ".join(words))


def _config(metrics: InMemoryMetrics, **overrides) -> SessionConfig:
    defaults = dict(
        transport=FakeTransport(chunks=[_chunk(), _chunk()]),
        vad=FakeVAD(),
        stt=FakeSTT(),
        agent=FakeAgent(),
        tts=FakeTTS(),
        noise_reducer=FakeNoiseReducer(),
        enable_noise_reduction=False,
        turn_manager_config=_FAST_TURN,
        metrics=metrics,
    )
    defaults.update(overrides)
    return SessionConfig(**defaults)


# ── Tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stt_latency_recorded() -> None:
    metrics = InMemoryMetrics()
    session = Session(_config(metrics))
    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    stats = metrics.get_latency(STT_LATENCY)
    assert stats is not None
    assert stats.count >= 1
    assert stats.min_ms > 0


@pytest.mark.asyncio
async def test_agent_latency_recorded_basic() -> None:
    metrics = InMemoryMetrics()
    session = Session(_config(metrics))
    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    stats = metrics.get_latency(AGENT_LATENCY)
    assert stats is not None
    assert stats.count >= 1
    assert stats.min_ms > 0


@pytest.mark.asyncio
async def test_agent_latency_recorded_streaming() -> None:
    metrics = InMemoryMetrics()
    session = Session(_config(metrics, agent=StreamingAgent()))
    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    stats = metrics.get_latency(AGENT_LATENCY)
    assert stats is not None
    assert stats.count >= 1


@pytest.mark.asyncio
async def test_tts_ttfb_recorded() -> None:
    metrics = InMemoryMetrics()
    session = Session(_config(metrics))
    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    stats = metrics.get_latency(TTS_TTFB)
    assert stats is not None
    assert stats.count >= 1
    assert stats.min_ms >= 0


@pytest.mark.asyncio
async def test_turn_e2e_recorded() -> None:
    metrics = InMemoryMetrics()
    session = Session(_config(metrics))
    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    stats = metrics.get_latency(TURN_E2E)
    assert stats is not None
    assert stats.count >= 1
    assert stats.min_ms > 0


@pytest.mark.asyncio
async def test_error_counter_incremented() -> None:
    metrics = InMemoryMetrics()
    session = Session(_config(metrics, agent=FailingAgent()))

    errors: list[Error] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))

    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    assert len(errors) >= 1
    assert metrics.get_counter(ERRORS) >= 1
