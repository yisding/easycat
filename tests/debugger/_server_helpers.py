from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncIterator

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
from easycat.noise_reduction import PassthroughNoiseReducer
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.artifacts import InMemoryArtifactStore
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.turn_manager import TurnManagerConfig


class _DeterministicAgent:
    async def run(self, text: str, **_kw):  # type: ignore[no-untyped-def]
        return f"reply-{text}"


class _FakeTransport:
    def __init__(self, chunks_in: list[AudioChunk]) -> None:
        self._chunks_in = chunks_in
        self.sent: list[AudioChunk] = []

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for chunk in self._chunks_in:
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk)

    async def clear_audio(self) -> None: ...


class _FakeVAD:
    def __init__(self) -> None:
        self._n = 0

    async def process(self, _chunk: AudioChunk) -> AsyncIterator[Event]:
        self._n += 1
        if self._n == 1:
            yield VADStartSpeaking()
        elif self._n == 2:
            yield VADStopSpeaking()

    def configure(self, **_kw): ...


class _FakeSTT:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None: ...
    async def send_audio(self, _chunk: AudioChunk) -> None: ...
    async def end_stream(self) -> None:
        await self._queue.put(STTEvent(type=STTEventType.FINAL, text="hi"))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            evt = await self._queue.get()
            if evt is None:
                break
            yield evt


class _FakeAgent:
    async def run(self, text: str) -> str:
        return text.upper()


class _DistinctiveTTS:
    async def synthesize(self, _payload) -> AsyncIterator[TTSEvent]:
        for marker in (b"\x11\x22", b"\x33\x44"):
            chunk = AudioChunk(data=marker * 160, format=PCM16_MONO_16K)
            yield TTSEvent(type=TTSEventType.AUDIO, audio=chunk)

    async def stop(self) -> None: ...
    async def cancel(self) -> None: ...


def _silent_chunk() -> AudioChunk:
    return AudioChunk(data=bytes(320), format=PCM16_MONO_16K)


async def _build_voice_bundle(tmp_path: pathlib.Path) -> pathlib.Path:
    """Drive a real Session through a turn and write its bundle."""
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(capacity=1024, artifact_store=artifact_store)
    transport = _FakeTransport(chunks_in=[_silent_chunk(), _silent_chunk()])
    session = Session(
        SessionConfig(
            transport=transport,
            vad=_FakeVAD(),
            stt=_FakeSTT(),
            agent=_FakeAgent(),
            tts=_DistinctiveTTS(),
            noise_reducer=PassthroughNoiseReducer(),
            enable_noise_reduction=False,
            turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
            journal=journal,
            artifact_store=artifact_store,
            session_id="debug-ui-test",
        )
    )
    await session.start()
    await asyncio.sleep(0.3)
    await session.stop()

    bundle_path = tmp_path / "ui.zip"
    session.export_debug_bundle(str(bundle_path))
    return bundle_path


_SAFE_HEADERS = {
    "Host": "localhost:8765",
    "Origin": "http://localhost:8765",
    "Content-Type": "application/json",
}
