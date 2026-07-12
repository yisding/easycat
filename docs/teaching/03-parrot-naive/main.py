"""Chapter 3 — Parrot, the naive way.

A bot that parrots whatever it thinks you just said. Turn detection
is a fixed silence timeout on STT partials. Deliberately broken.

Run it and break it — "The capital of France is... uh... Paris" is
the canonical killer. Chapter 4 replaces this with a real VAD.

Dependencies:
    uv sync --extra quickstart --extra deepgram --group dev
    export OPENAI_API_KEY=...      # OpenAI TTS
    export DEEPGRAM_API_KEY=...    # mid-speech STT partials
    uv run easycat doctor
    uv run easycat doctor --env-file .env         # if keys live in .env
    uv run easycat doctor --env-file .env --json  # for parseable checks
    Add `--env-file .env` after `uv run` on script commands if keys live in `.env`.
"""

from __future__ import annotations

import asyncio
import os
import time
import types
from contextlib import AsyncExitStack
from pathlib import Path

from easycat import LocalTransportConfig
from easycat.audio_format import PCM16_MONO_24K
from easycat.debug.export import export_debug_bundle
from easycat.events import EventBus, STTEvent, STTEventType
from easycat.recipes import speak
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
from easycat.runtime.capabilities import close_if_supported
from easycat.stt.factory import STTProviderConfig, create_stt_provider
from easycat.transports.local import LocalTransport

SILENCE_TIMEOUT_S = 0.5  # ← the magic number we will watch break things
RUNS_DIR = Path(__file__).parent / "runs"
SESSION_ID = f"ch03-parrot-{int(time.time())}"
STTQueueItem = tuple[int, STTEvent, float] | None


class ParrotEventStreamEndedError(RuntimeError):
    """Private TaskGroup signal: the STT consumer drained its sentinel."""


def record_stt_received(
    journal: InMemoryRingBuffer,
    *,
    event_id: int,
    event: STTEvent,
    offset_ms: float,
    queue_depth: int,
) -> None:
    """Record provider ingress before the consumer can be blocked by TTS."""
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="stt.received",
        session_id=SESSION_ID,
        data={
            "stage": "stt",
            "event_id": event_id,
            "event_type": event.type.value,
            "text": event.text,
            "offset_ms": offset_ms,
            "queue_depth_before_put": queue_depth,
        },
    )


def record_stt_consumed(
    journal: InMemoryRingBuffer,
    *,
    event_id: int,
    event: STTEvent,
    received_offset_ms: float,
    consumed_offset_ms: float,
    queue_depth: int,
) -> None:
    """Record when the parrot finally dequeues one provider event."""
    journal.append(
        kind=JournalRecordKind.EVENT,
        name=f"stt.{event.type.value}",
        session_id=SESSION_ID,
        data={
            "stage": "stt",
            "event_id": event_id,
            "event_type": event.type.value,
            "text": event.text,
            "offset_ms": consumed_offset_ms,
            "received_offset_ms": received_offset_ms,
            "consumer_lag_ms": consumed_offset_ms - received_offset_ms,
            "queue_depth_after_get": queue_depth,
        },
    )


def record_delivery(
    journal: InMemoryRingBuffer,
    *,
    text: str,
    accepted_chunks: int,
    rejected_chunks: int,
    offset_ms: float,
) -> None:
    """Record transport acceptance without claiming speaker playback."""
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="parrot.delivery",
        session_id=SESSION_ID,
        data={
            "stage": "parrot",
            "committed_text": text,
            "accepted_chunks": accepted_chunks,
            "rejected_chunks": rejected_chunks,
            "offset_ms": offset_ms,
        },
    )
    if rejected_chunks:
        print(
            "  transport rejected "
            f"{rejected_chunks}/{accepted_chunks + rejected_chunks} audio chunks"
        )


async def speak_and_record(
    transport, journal: InMemoryRingBuffer, text: str, start: float
) -> None:
    """Speak once, then preserve every transport acceptance result."""
    accepted_chunks, rejected_chunks = await speak(transport, text)
    record_delivery(
        journal,
        text=text,
        accepted_chunks=accepted_chunks,
        rejected_chunks=rejected_chunks,
        offset_ms=(time.monotonic() - start) * 1000,
    )


async def feed_audio(stt, transport) -> None:
    async for chunk in transport.receive_audio():
        await stt.send_audio(chunk)


async def listen_stt(
    stt,
    ev_queue: asyncio.Queue[STTQueueItem],
    journal: InMemoryRingBuffer,
    start: float,
) -> None:
    event_id = 0
    async for event in stt.events():
        event_id += 1
        received_offset_ms = (time.monotonic() - start) * 1000
        record_stt_received(
            journal,
            event_id=event_id,
            event=event,
            offset_ms=received_offset_ms,
            queue_depth=ev_queue.qsize(),
        )
        await ev_queue.put((event_id, event, received_offset_ms))
    await ev_queue.put(None)


async def parrot_events(
    transport,
    ev_queue: asyncio.Queue[STTQueueItem],
    journal: InMemoryRingBuffer,
    start: float,
) -> None:
    last_text = ""
    while True:
        try:
            # If no new event arrives within SILENCE_TIMEOUT_S, we
            # interpret silence as "user is done" — the whole bug.
            item = await asyncio.wait_for(ev_queue.get(), timeout=SILENCE_TIMEOUT_S)
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
                await speak_and_record(transport, journal, last_text, start)
                last_text = ""
            continue
        if item is None:
            break
        event_id, event, received_offset_ms = item
        # Deliberately acting on partials — chapter 2's rule, broken
        # on purpose. Chapter 4 restores it by waiting for a real
        # turn boundary from the VAD.
        last_text = event.text
        kind = "FINAL" if event.type == STTEventType.FINAL else "part "
        offset_ms = (time.monotonic() - start) * 1000
        print(f"  t+{offset_ms:6.0f}ms  [{kind}] {event.text}")
        record_stt_consumed(
            journal,
            event_id=event_id,
            event=event,
            received_offset_ms=received_offset_ms,
            consumed_offset_ms=offset_ms,
            queue_depth=ev_queue.qsize(),
        )


async def stop_when_parrot_ends(
    transport,
    ev_queue: asyncio.Queue[STTQueueItem],
    journal: InMemoryRingBuffer,
    start: float,
) -> None:
    """Turn normal queue exhaustion into a TaskGroup-wide stop signal."""
    await parrot_events(transport, ev_queue, journal, start)
    raise ParrotEventStreamEndedError


async def run_parrot(stt, transport, journal: InMemoryRingBuffer) -> None:
    """Own one parrot stream until cancellation, failure, or STT exhaustion."""
    async with AsyncExitStack() as resources:
        # These objects exist before connect(), so register final cleanup
        # before the first fallible acquisition step.
        resources.push_async_callback(transport.disconnect)
        resources.push_async_callback(close_if_supported, stt)
        await transport.connect()

        await stt.start_stream()
        # A logical stream exists only after start_stream() succeeds.
        resources.push_async_callback(stt.end_stream)

        start = time.monotonic()
        print("Naive parrot. Talk to it. Ctrl-C when you're sick of it.")
        ev_queue: asyncio.Queue[STTQueueItem] = asyncio.Queue()

        try:
            async with asyncio.TaskGroup() as streams:
                streams.create_task(feed_audio(stt, transport))
                streams.create_task(listen_stt(stt, ev_queue, journal, start))
                streams.create_task(stop_when_parrot_ends(transport, ev_queue, journal, start))
        except* ParrotEventStreamEndedError:
            # ``parrot_events`` consumed the listener's None sentinel. Raising
            # inside its wrapper makes TaskGroup cancel and join the infinite
            # microphone feeder before resource teardown begins.
            pass


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

    try:
        await run_parrot(stt, transport, journal)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

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
