"""Chapter 3 — Parrot, the naive way.

A bot that parrots whatever it thinks you just said. Turn detection
is a fixed silence timeout on STT partials. Deliberately broken.

Run it and break it — "The capital of France is... uh... Paris" is
the canonical killer. Chapter 4 replaces this with a real VAD.

Dependencies:
    uv sync --extra quickstart --group dev
    export OPENAI_API_KEY=...      # OpenAI TTS
    export DEEPGRAM_API_KEY=...    # mid-speech STT partials
"""

from __future__ import annotations

import asyncio
import os
import time
import types
from pathlib import Path

from easycat import LocalTransportConfig
from easycat.audio_format import PCM16_MONO_24K
from easycat.debug.export import export_debug_bundle
from easycat.events import EventBus, STTEventType
from easycat.quick import speak
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
from easycat.stt.factory import STTProviderConfig, create_stt_provider
from easycat.transports.local import LocalTransport

SILENCE_TIMEOUT_S = 0.5  # ← the magic number we will watch break things
RUNS_DIR = Path(__file__).parent / "runs"
SESSION_ID = f"ch03-parrot-{int(time.time())}"


async def main() -> None:
    oai_key = os.getenv("OPENAI_API_KEY")
    dg_key = os.getenv("DEEPGRAM_API_KEY")
    if not oai_key or not dg_key:
        raise SystemExit("Set OPENAI_API_KEY (for TTS) and DEEPGRAM_API_KEY (for STT).")

    journal = InMemoryRingBuffer(capacity=10_000)
    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))

    # Deepgram emits partials mid-speech, which is what this chapter needs
    # to feel break. Its STT factory config takes provider-specific args via
    # ``params``. ``sample_rate=24000`` matches our LocalTransport's mic
    # format; ``event_bus`` is only used by Deepgram for WebSocket-reconnect
    # telemetry — we wire a fresh bus here with no subscribers to satisfy
    # the provider's constructor.
    stt = create_stt_provider(
        STTProviderConfig(
            provider="deepgram",
            api_key=dg_key,
            params={"sample_rate": 24000, "event_bus": EventBus()},
        )
    )

    await transport.connect()
    await stt.start_stream()
    start = time.monotonic()
    print("Naive parrot. Talk to it. Ctrl-C when you're sick of it.")

    # Bridge STT events into an asyncio.Queue so the parrot loop can use
    # ``asyncio.wait_for`` to implement "silence timeout since last event."
    ev_queue: asyncio.Queue = asyncio.Queue()

    async def feed_audio() -> None:
        async for chunk in transport.receive_audio():
            await stt.send_audio(chunk)

    async def listen_stt() -> None:
        async for event in stt.events():
            await ev_queue.put(event)
        await ev_queue.put(None)

    async def parrot() -> None:
        last_text = ""
        while True:
            try:
                # If no new event arrives within SILENCE_TIMEOUT_S, we
                # interpret silence as "user is done" — the whole bug.
                event = await asyncio.wait_for(ev_queue.get(), timeout=SILENCE_TIMEOUT_S)
            except TimeoutError:
                if last_text:
                    offset_ms = (time.monotonic() - start) * 1000
                    print(f"  t+{offset_ms:6.0f}ms  PARROT → {last_text!r}")
                    journal.append(
                        kind=JournalRecordKind.EVENT,
                        name="parrot.fire",
                        session_id=SESSION_ID,
                        data={
                            "stage": "parrot",
                            "committed_text": last_text,
                            "silence_timeout_s": SILENCE_TIMEOUT_S,
                            "offset_ms": offset_ms,
                        },
                    )
                    await speak(transport, last_text)
                    last_text = ""
                continue
            if event is None:
                break
            # Deliberately acting on partials — chapter 2's rule, broken
            # on purpose. Chapter 4 restores it by waiting for a real
            # turn boundary from the VAD.
            last_text = event.text
            kind = "FINAL" if event.type == STTEventType.FINAL else "part "
            offset_ms = (time.monotonic() - start) * 1000
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
                },
            )

    try:
        await asyncio.gather(feed_audio(), listen_stt(), parrot())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await stt.end_stream()
        await transport.disconnect()

    RUNS_DIR.mkdir(exist_ok=True)
    bundle_path = RUNS_DIR / f"{SESSION_ID}.bundle"
    session_stub = types.SimpleNamespace(journal=journal)
    export_debug_bundle(session_stub, bundle_path, overwrite=True)
    print(f"\nWrote bundle → {bundle_path.relative_to(Path.cwd())}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
