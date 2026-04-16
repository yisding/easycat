#!/usr/bin/env python3
"""Demo: run one turn with stub providers and dump journal records.

No API keys required — uses in-process stubs for STT, TTS, and transport.

Usage:
    uv run python examples/journal_demo.py
"""

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
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig

# ── Stub providers ───────────────────────────────────────────────


def _chunk(n: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n), format=PCM16_MONO_16K)


class StubTransport:
    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for _ in range(3):
            yield _chunk()

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass


class StubVAD:
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


class StubSTT:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        await self._queue.put(STTEvent(type=STTEventType.FINAL, text="Hello, how are you?"))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event


class StubAgent:
    async def run(self, text: str) -> str:
        return f"I'm doing great! You said: {text}"


class StubTTS:
    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class StubNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


# ── Main ─────────────────────────────────────────────────────────


async def main() -> None:
    # Create journal — the single source of truth for all observability.
    journal = InMemoryRingBuffer(capacity=10_000)

    config = SessionConfig(
        transport=StubTransport(),
        vad=StubVAD(),
        stt=StubSTT(),
        agent=StubAgent(),
        tts=StubTTS(),
        noise_reducer=StubNoiseReducer(),
        turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
        journal=journal,
    )
    session = Session(config)

    # Run one turn.
    print("Starting session...")
    await session.start()
    await asyncio.sleep(0.5)
    await session.stop()
    print("Session stopped.\n")

    # Dump journal records.
    view = session.journal
    assert view is not None
    records = view.read()

    print(f"{'seq':>4}  {'kind':<24} {'name':<28} data")
    print("-" * 90)
    for r in records:
        data_summary = str(r.data)[:40] if r.data else ""
        print(f"{r.sequence:>4}  {r.kind.value:<24} {r.name:<28} {data_summary}")

    # Summary by kind.
    print("\n--- Summary ---")
    from collections import Counter

    by_kind = Counter(r.kind.value for r in records)
    for kind, count in sorted(by_kind.items()):
        print(f"  {kind}: {count} records")
    print(f"  total: {len(records)} records")


if __name__ == "__main__":
    asyncio.run(main())
