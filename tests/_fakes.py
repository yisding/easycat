"""Protocol-conformant provider fakes shared across test domains."""

from __future__ import annotations

import asyncio
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
from easycat.providers import STTProvider, Transport, TTSProvider, VADProvider
from easycat.tts.input import TTSInput


def _chunk(n_bytes: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n_bytes), format=PCM16_MONO_16K)


class FakeTransport:
    """Configurable in-memory transport with the production return contract."""

    def __init__(
        self,
        chunks: list[AudioChunk] | None = None,
        *,
        accepted: bool = True,
    ) -> None:
        self.chunks = chunks or []
        self.accepted = accepted
        self.sent: list[AudioChunk] = []
        self.connected = False
        self.disconnected = False
        self.clear_calls = 0

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for chunk in self.chunks:
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> bool:
        self.sent.append(chunk)
        return self.accepted

    async def clear_audio(self) -> None:
        self.clear_calls += 1

    def version_info(self) -> dict[str, str]:
        return {"provider": "fake-transport", "api_version": "test"}


class FakeVAD:
    """Emit a start event on the first chunk and a stop event on the second."""

    def __init__(self) -> None:
        self._call_count = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        self._call_count += 1
        if self._call_count == 1:
            yield VADStartSpeaking()
        elif self._call_count == 2:
            yield VADStopSpeaking()

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
    ) -> None:
        _ = min_speech_duration_ms, min_silence_duration_ms, sensitivity

    def version_info(self) -> dict[str, str]:
        return {"provider": "fake-vad", "api_version": "test"}


class FakeSTT:
    """Queue one configurable final transcript when its stream ends."""

    def __init__(self, transcript: str = "hello world") -> None:
        self._transcript = transcript
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def commit_segment(self) -> bool:
        return False

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

    def version_info(self) -> dict[str, str]:
        return {"provider": "fake-stt", "api_version": "test"}


class FakeTTS:
    """Record synthesized text and return one deterministic audio event."""

    def __init__(self) -> None:
        self.synthesized_texts: list[str] = []

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        text = payload if isinstance(payload, str) else payload.text
        self.synthesized_texts.append(text)
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {"provider": "fake-tts", "api_version": "test"}


assert isinstance(FakeTransport(), Transport)
assert isinstance(FakeVAD(), VADProvider)
assert isinstance(FakeSTT(), STTProvider)
assert isinstance(FakeTTS(), TTSProvider)
