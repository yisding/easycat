"""Stub providers for no-op defaults and no-key scripted demos."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

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
from easycat.tts.input import TTSInput, coerce_tts_input

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, annotation only
    from easycat.config import EasyConfig

_NOOP_VERSION = {
    "provider": "noop",
    "model": "unknown",
    "api_version": "unknown",
    "sdk_version": "unknown",
}


class NoopSTT:
    """STT provider that does nothing — used as default."""

    is_passthrough_provider = True

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def commit_segment(self) -> bool:
        return False

    async def end_stream(self) -> None:
        pass

    async def events(self) -> AsyncIterator[STTEvent]:
        return
        yield  # make this an async generator

    def version_info(self) -> dict[str, str]:
        return {**_NOOP_VERSION, "provider": "noop-stt"}


class NoopTTS:
    """TTS provider that does nothing — used as default."""

    is_passthrough_provider = True

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        _ = coerce_tts_input(payload)
        return
        yield  # make this an async generator

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {**_NOOP_VERSION, "provider": "noop-tts"}


class NoopVAD:
    """VAD provider that does nothing — used as default."""

    is_passthrough_provider = True

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        return
        yield  # make this an async generator

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
    ) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {**_NOOP_VERSION, "provider": "noop-vad"}


class NoopTransport:
    """Transport that produces no audio — used as default."""

    transport_kind = "noop"
    is_passthrough_provider = True

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        return
        yield  # make this an async generator

    async def send_audio(self, chunk: AudioChunk) -> bool:
        return True

    async def clear_audio(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {**_NOOP_VERSION, "provider": "noop-transport"}


class NoopAgent:
    """Agent that echoes input text — used as default for pipeline testing."""

    is_passthrough_provider = True

    async def run(self, text: str) -> str:
        return text


def silent_pcm16_chunk(byte_count: int = 320) -> AudioChunk:
    """Return a silent 16 kHz PCM16 chunk for scripted local demos/tests."""
    return AudioChunk(data=bytes(byte_count), format=PCM16_MONO_16K)


def _scripted_chunks(
    chunks: int | Sequence[AudioChunk],
    *,
    byte_count: int,
    label: str,
) -> tuple[AudioChunk, ...]:
    if isinstance(chunks, int):
        if chunks <= 0:
            raise ValueError(f"{label} must contain at least one chunk")
        return tuple(silent_pcm16_chunk(byte_count) for _ in range(chunks))
    normalized = tuple(chunks)
    if not normalized:
        raise ValueError(f"{label} must contain at least one chunk")
    return normalized


class ScriptedTransport:
    """Transport that yields a finite sequence of local audio chunks."""

    transport_kind = "scripted"

    def __init__(self, chunks: int | Sequence[AudioChunk] = 3) -> None:
        self._chunks = _scripted_chunks(chunks, byte_count=320, label="chunks")
        self.sent: list[AudioChunk] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for chunk in self._chunks:
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> bool:
        self.sent.append(chunk)
        return True

    async def clear_audio(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {**_NOOP_VERSION, "provider": "scripted-transport"}


class ScriptedVAD:
    """VAD that marks speech over a configurable chunk window."""

    def __init__(self, *, start_chunk: int = 1, stop_chunk: int = 3) -> None:
        self._start_chunk = start_chunk
        self._stop_chunk = stop_chunk
        self._seen = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        self._seen += 1
        if self._seen == self._start_chunk:
            yield VADStartSpeaking()
        if self._seen == self._stop_chunk:
            yield VADStopSpeaking()

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
    ) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {**_NOOP_VERSION, "provider": "scripted-vad"}


class ScriptedSTT:
    """STT that emits one final transcript after the stream ends."""

    def __init__(self, transcript: str = "hello") -> None:
        self._transcript = transcript
        self._events: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._ended = False

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def commit_segment(self) -> bool:
        return False

    async def end_stream(self) -> None:
        if self._ended:
            return
        self._ended = True
        await self._events.put(STTEvent(type=STTEventType.FINAL, text=self._transcript))
        await self._events.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    def version_info(self) -> dict[str, str]:
        return {**_NOOP_VERSION, "provider": "scripted-stt"}


class ScriptedAgent:
    """Agent that returns a fixed or transcript-derived reply."""

    def __init__(self, reply: str | Callable[[str], str] | None = None) -> None:
        self._reply = reply

    async def run(self, text: str) -> str:
        if callable(self._reply):
            return self._reply(text)
        if self._reply is not None:
            return self._reply
        return f"Echo: {text}"


class ScriptedTTS:
    """TTS that returns one or more silent audio chunks for any text."""

    def __init__(self, chunks: int | Sequence[AudioChunk] = 1) -> None:
        self._chunks = _scripted_chunks(chunks, byte_count=640, label="chunks")

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        _ = coerce_tts_input(payload)
        for chunk in self._chunks:
            yield TTSEvent(type=TTSEventType.AUDIO, audio=chunk)

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {**_NOOP_VERSION, "provider": "scripted-tts"}


@dataclass(frozen=True, slots=True)
class ScriptedTurnProviders:
    """Providers that drive one synthetic speech turn without API keys."""

    transport: ScriptedTransport
    vad: ScriptedVAD
    stt: ScriptedSTT
    agent: ScriptedAgent
    tts: ScriptedTTS


def scripted_turn_providers(
    *,
    transcript: str = "hello",
    reply: str | Callable[[str], str] | None = None,
    input_chunks: int | Sequence[AudioChunk] = 3,
    output_chunks: int | Sequence[AudioChunk] = 1,
) -> ScriptedTurnProviders:
    """Build providers for one no-key local turn."""
    if isinstance(input_chunks, int):
        input_count = input_chunks
    else:
        input_count = len(tuple(input_chunks))
    if input_count <= 0:
        raise ValueError("input_chunks must contain at least one chunk")
    return ScriptedTurnProviders(
        transport=ScriptedTransport(input_chunks),
        vad=ScriptedVAD(stop_chunk=input_count),
        stt=ScriptedSTT(transcript),
        agent=ScriptedAgent(reply),
        tts=ScriptedTTS(output_chunks),
    )


def scripted_turn_config(
    *,
    agent: object = None,
    transcript: str = "hello",
    reply: str | Callable[[str], str] | None = None,
    debug: Literal["off", "light", "full"] = "light",
    record_to: str | Path | None = None,
) -> EasyConfig:
    """Build an ``EasyConfig`` that drives one scripted, key-free audio turn.

    Wires :func:`scripted_turn_providers` into ``EasyConfig.mic(...)`` so the
    transport → VAD → STT → agent → TTS pipeline really runs with no
    microphone, no API key, no provider extra and no network.  Pass *agent* to
    substitute your own agent object for the scripted echo agent.

    The audio is synthetic: this exercises pipeline wiring, not speech quality.
    """
    # Imported in the body on purpose: a module-level import would invert the
    # existing ``easycat.config`` → ``easycat.stubs`` edge.
    from easycat.config import EasyConfig
    from easycat.turn_manager import TurnManagerConfig

    providers = scripted_turn_providers(transcript=transcript, reply=reply)
    return EasyConfig.mic(
        transport=providers.transport,
        vad=providers.vad,
        stt=providers.stt,
        agent=agent if agent is not None else providers.agent,
        tts=providers.tts,
        turn_taking=TurnManagerConfig(end_of_turn_silence_ms=1),
        debug=debug,
        record_to=record_to,
    )


__all__ = [
    "NoopAgent",
    "NoopSTT",
    "NoopTTS",
    "NoopTransport",
    "NoopVAD",
    "ScriptedAgent",
    "ScriptedSTT",
    "ScriptedTTS",
    "ScriptedTransport",
    "ScriptedTurnProviders",
    "ScriptedVAD",
    "scripted_turn_config",
    "scripted_turn_providers",
    "silent_pcm16_chunk",
]
