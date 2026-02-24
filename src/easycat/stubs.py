"""No-op stub providers for use as defaults when no real provider is configured."""

from __future__ import annotations

from collections.abc import AsyncIterator

from easycat.audio_format import AudioChunk
from easycat.events import Event, STTEvent, TTSEvent


class NoopSTT:
    """STT provider that does nothing — used as default."""

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        pass

    async def events(self) -> AsyncIterator[STTEvent]:
        return
        yield  # make this an async generator


class NoopTTS:
    """TTS provider that does nothing — used as default."""

    async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
        return
        yield  # make this an async generator

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class NoopVAD:
    """VAD provider that does nothing — used as default."""

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        return
        yield  # make this an async generator

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 300,
        sensitivity: float = 0.5,
        pre_roll_ms: int = 100,
        post_roll_ms: int = 100,
    ) -> None:
        pass


class NoopTransport:
    """Transport that produces no audio — used as default."""

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        return
        yield  # make this an async generator

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def clear_audio(self) -> None:
        pass


class NoopAgent:
    """Agent that echoes input text — used as default for pipeline testing."""

    async def run(self, text: str) -> str:
        return text
