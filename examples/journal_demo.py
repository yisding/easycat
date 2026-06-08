#!/usr/bin/env python3
"""Demo: run one turn with stub providers and dump journal records.

No API keys required.  The stubs here implement the provider Protocols
structurally — inheriting from ``easycat.stubs`` Noop classes would
trigger Session's noop guard, so each stub stands alone.

Setup:
    uv sync --group dev

Run:
    uv run python examples/journal_demo.py
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from easycat import EasyConfig, TurnManagerConfig, create_session
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


def _chunk(n: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n), format=PCM16_MONO_16K)


class StubTransport:
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def send_audio(self, chunk: AudioChunk) -> bool:
        return True

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for _ in range(3):
            yield _chunk()

    def version_info(self) -> dict[str, str]:
        return {"provider": "stub-transport"}


class StubVAD:
    def __init__(self) -> None:
        self._n = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        self._n += 1
        if self._n == 1:
            yield VADStartSpeaking()
        elif self._n == 3:
            yield VADStopSpeaking()

    def configure(self, **kwargs: object) -> None: ...

    def version_info(self) -> dict[str, str]:
        return {"provider": "stub-vad"}


class StubSTT:
    def __init__(self) -> None:
        self._closed = asyncio.Event()

    async def start_stream(self) -> None: ...
    async def send_audio(self, chunk: AudioChunk) -> None: ...

    async def commit_segment(self) -> bool:
        return False

    async def end_stream(self) -> None:
        self._closed.set()

    async def events(self) -> AsyncIterator[STTEvent]:
        await self._closed.wait()
        yield STTEvent(type=STTEventType.FINAL, text="Hello, how are you?")

    def version_info(self) -> dict[str, str]:
        return {"provider": "stub-stt"}


class StubAgent:
    async def run(self, text: str) -> str:
        return f"I'm doing great! You said: {text}"


class StubTTS:
    supports_ssml = False

    async def synthesize(self, payload: object) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk(640))

    async def stop(self) -> None: ...
    async def cancel(self) -> None: ...

    def version_info(self) -> dict[str, str]:
        return {"provider": "stub-tts"}


async def main() -> None:
    config = EasyConfig.mic(
        transport=StubTransport(),
        vad=StubVAD(),
        stt=StubSTT(),
        agent=StubAgent(),
        tts=StubTTS(),
        turn_taking=TurnManagerConfig(end_of_turn_silence_ms=1),
        debug="light",
    )
    async with create_session(config) as session:
        await asyncio.sleep(0.5)

    assert session.journal is not None
    records = session.journal.read()

    print(f"{'seq':>4}  {'kind':<24} {'name':<28} data")
    print("-" * 90)
    for r in records:
        data_summary = str(r.data)[:40] if r.data else ""
        print(f"{r.sequence:>4}  {r.kind.value:<24} {r.name:<28} {data_summary}")

    print("\n--- Summary ---")
    from collections import Counter

    by_kind = Counter(r.kind.value for r in records)
    for kind, count in sorted(by_kind.items()):
        print(f"  {kind}: {count} records")
    print(f"  total: {len(records)} records")


if __name__ == "__main__":
    asyncio.run(main())
