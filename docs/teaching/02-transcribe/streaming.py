"""Chapter 2 — streaming transcription.

Open a mic transport, stream audio into an STT provider, and print
partial + final transcripts with timestamps as they arrive. Writes a
debug bundle to ``runs/``.

Dependencies:
    uv sync --extra quickstart --group dev
    export OPENAI_API_KEY=...   # or DEEPGRAM_API_KEY for mid-speech partials
"""

from __future__ import annotations

import asyncio
import os
import time
import types
from pathlib import Path

from easycat import (
    LocalTransport,
    LocalTransportConfig,
    create_stt_provider,
)
from easycat.audio_format import PCM16_MONO_24K
from easycat.debug.export import export_debug_bundle
from easycat.events import STTEventType
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
from easycat.stt.factory import STTProviderConfig

DURATION_S = 5
RUNS_DIR = Path(__file__).parent / "runs"
SESSION_ID = f"ch02-streaming-{int(time.time())}"


async def main() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Set OPENAI_API_KEY in your environment first.")

    journal = InMemoryRingBuffer(capacity=10_000)
    # The same STT factory from batch.py — we just hand it a config
    # instead of calling the `transcribe_file` shortcut. No consumer
    # code would change if we swapped "openai" for "deepgram".
    stt = create_stt_provider(STTProviderConfig(provider="openai", api_key=api_key))

    # LocalTransport's default 24 kHz matches chapters 3+. OpenAI STT
    # ingests WAV at whatever sample rate it's given, so this is fine.
    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))

    await transport.connect()
    await stt.start_stream()
    start = time.monotonic()
    print(f"Speak for {DURATION_S} seconds...")

    async def feed_audio() -> None:
        """Push mic chunks into STT until DURATION_S seconds elapse."""
        async for chunk in transport.receive_audio():
            await stt.send_audio(chunk)
            if time.monotonic() - start >= DURATION_S:
                break
        # Closing the STT stream is what triggers the upload (for
        # OpenAI's batch provider) or the final commit (for Deepgram).
        # For OpenAI this call blocks for the full round-trip: the
        # partials you see start arriving *after* we get here.
        await stt.end_stream()

    async def consume_events() -> None:
        """Print every partial / final as soon as it arrives."""
        async for event in stt.events():
            offset_ms = (time.monotonic() - start) * 1000
            kind = "FINAL" if event.type == STTEventType.FINAL else "part "
            print(f"  t+{offset_ms:6.0f}ms  [{kind}] {event.text}")
            journal.append(
                kind=JournalRecordKind.EVENT,
                name=f"stt.{event.type.value}",
                session_id=SESSION_ID,
                data={
                    "stage": "stt",
                    "event_type": event.type.value,
                    "text": event.text,
                    "offset_ms": offset_ms,
                    # t_ms mirrors the later chapters' field so downstream
                    # scripts (ch 12's evals.py, etc.) can read this bundle
                    # without a translator.
                    "t_ms": time.monotonic() * 1000,
                },
            )

    try:
        await asyncio.gather(feed_audio(), consume_events())
    finally:
        await transport.disconnect()

    RUNS_DIR.mkdir(exist_ok=True)
    bundle_path = RUNS_DIR / f"{SESSION_ID}.bundle"
    session_stub = types.SimpleNamespace(journal=journal)
    export_debug_bundle(session_stub, bundle_path, overwrite=True)
    print(f"\nWrote bundle → {bundle_path.relative_to(Path.cwd())}")


if __name__ == "__main__":
    asyncio.run(main())
