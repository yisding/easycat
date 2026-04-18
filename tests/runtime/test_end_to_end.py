"""End-to-end session turn verification.

Runs a full session turn with stub providers and verifies that the session
completes without error. Legacy strangler-fig journal records (EVENT,
SPAN_START, SPAN_END, METRIC) are no longer produced after the WS5 migration
to no-op shims.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import (
    Event,
    STTEvent,
    STTEventType,
    TTSEvent,
    TTSEventType,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig

# ── Stub providers (adapted from test_session_smoke.py) ──────────


def _chunk(n: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n), format=PCM16_MONO_16K)


class _Transport:
    def __init__(self) -> None:
        self.sent: list[AudioChunk] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for _ in range(3):
            yield _chunk()

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk)


class _VAD:
    def __init__(self) -> None:
        self._n = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        self._n += 1
        if self._n == 1:
            yield VADStartSpeaking()
        elif self._n == 3:
            yield VADStopSpeaking()

    def configure(self, **kwargs: object) -> None:
        pass


class _STT:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        await self._queue.put(STTEvent(type=STTEventType.FINAL, text="hello"))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event


class _Agent:
    async def run(self, text: str) -> str:
        return f"Echo: {text}"


class _TTS:
    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class _NoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


# ── Test ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_turn_completes_successfully():
    """One turn with stub providers completes without error."""
    transport = _Transport()
    config = SessionConfig(
        transport=transport,
        vad=_VAD(),
        stt=_STT(),
        agent=_Agent(),
        tts=_TTS(),
        noise_reducer=_NoiseReducer(),
        turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
        session_id="e2e-test",
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.5)  # let the pipeline complete
    await session.stop()

    # The transport should have received at least one audio chunk from TTS
    assert len(transport.sent) > 0
