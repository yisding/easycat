"""Shared helpers for core Session tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import (
    STTEvent,
    STTEventType,
    TTSEvent,
    TTSEventType,
)
from easycat.noise_reduction import PassthroughNoiseReducer
from easycat.runtime.records import JournalRecordKind
from easycat.session._types import SessionConfig
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig
from tests._fakes import FakeSTT, FakeTransport, FakeTTS, FakeVAD

_FAST_TURN = TurnManagerConfig(end_of_turn_silence_ms=1)


def _make_chunk(n_bytes: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n_bytes), format=PCM16_MONO_16K)


def _make_loud_chunk(n_samples: int = 160, amplitude: int = 6000) -> AudioChunk:
    sample = int(amplitude).to_bytes(2, "little", signed=True)
    return AudioChunk(data=sample * n_samples, format=PCM16_MONO_16K)


class WarmupTransport(FakeTransport):
    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self._calls = calls

    async def connect(self) -> None:
        self._calls.append("transport.connect")
        await super().connect()

    async def warmup(self) -> None:
        self._calls.append("transport.warmup")

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        self._calls.append("transport.receive")
        return
        yield


class FakePlaybackAckTransport(FakeTransport):
    def __init__(self, chunks: list[AudioChunk] | None = None) -> None:
        super().__init__(chunks=chunks)
        self.playback_marks: list[str] = []
        self.playback_mark_sent = asyncio.Event()

    async def send_playback_mark(self, name: str | None = None) -> str:
        mark_name = name or f"mark_{len(self.playback_marks) + 1}"
        self.playback_marks.append(mark_name)
        self.playback_mark_sent.set()
        return mark_name


class ReportingTransport(FakeTransport):
    reports_audio_delivery = True


class WarmupSTT(FakeSTT):
    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self._calls = calls

    async def warmup(self) -> None:
        self._calls.append("stt.warmup")


class SegmentingSTT:
    """STT that supports early segment commits within a single stream."""

    def __init__(self, committed_segments: list[str], final_segment: str = "") -> None:
        self._committed_segments = list(committed_segments)
        self._final_segment = final_segment
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self.commit_calls = 0
        self.start_calls = 0
        self.end_calls = 0

    async def start_stream(self) -> None:
        self.start_calls += 1
        self._queue = asyncio.Queue()

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def commit_segment(self) -> bool:
        if not self._committed_segments:
            return False
        self.commit_calls += 1
        await self._queue.put(
            STTEvent(type=STTEventType.FINAL, text=self._committed_segments.pop(0))
        )
        return True

    async def end_stream(self) -> None:
        self.end_calls += 1
        if self._final_segment:
            await self._queue.put(STTEvent(type=STTEventType.FINAL, text=self._final_segment))
            self._final_segment = ""
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event


class AutoTurnSTT(FakeSTT):
    def __init__(self, transcript: str = "hello flux", *, final_after_chunks: int = 3) -> None:
        super().__init__(transcript=transcript)
        self.final_after_chunks = final_after_chunks
        self.sent_chunks: list[AudioChunk] = []
        self.start_count = 0
        self.end_count = 0
        self._final_emitted = False

    async def start_stream(self) -> None:
        self.start_count += 1

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent_chunks.append(chunk)
        if not self._final_emitted and len(self.sent_chunks) >= self.final_after_chunks:
            self._final_emitted = True
            await self._queue.put(STTEvent(type=STTEventType.FINAL, text=self._transcript))

    async def end_stream(self) -> None:
        self.end_count += 1
        await self._queue.put(None)


class FakeAgent:
    async def run(self, text: str) -> str:
        return text.upper()


class WarmupTTS(FakeTTS):
    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self._calls = calls

    def warmup(self) -> None:
        self._calls.append("tts.warmup")


class MarkerTTS(FakeTTS):
    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        text = payload if isinstance(payload, str) else payload.text
        yield TTSEvent(type=TTSEventType.MARKERS, markers=[{"word": text, "start_ms": 0}])
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_make_chunk())


class TrackingJournal:
    def __init__(self) -> None:
        self.finalize_calls = 0
        self.close_calls = 0

    def append(
        self,
        kind: JournalRecordKind,
        name: str,
        session_id: str,
        turn_id: str | None = None,
        data: dict[str, object] | None = None,
        error: object | None = None,
        tags: frozenset[str] = frozenset(),
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> int:
        return 1

    def read(self, start: int = 0, limit: int | None = None) -> list[object]:
        return []

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
    ) -> list[object]:
        return []

    def close(self) -> None:
        self.close_calls += 1

    def flush(self) -> None:
        pass

    def finalize(self) -> None:
        self.finalize_calls += 1

    @property
    def latest_sequence(self) -> int:
        return 0

    @property
    def degraded(self) -> bool:
        return False


def _full_config(**overrides: Any) -> SessionConfig:
    """Build a SessionConfig with all required providers filled in."""
    defaults: dict[str, Any] = {
        "transport": FakeTransport(),
        "vad": FakeVAD(),
        "stt": FakeSTT(),
        "agent": FakeAgent(),
        "tts": FakeTTS(),
        "noise_reducer": PassthroughNoiseReducer(),
        "enable_noise_reduction": False,
    }
    defaults.update(overrides)
    return SessionConfig(**defaults)
