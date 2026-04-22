"""Chapter 9b — cancel on barge-in.

Same architecture as ``ignore.py`` plus a ``CancelToken`` per turn.
When user speech starts while the bot is talking, we cancel:

- The agent stream (stop pulling tokens from OpenAI).
- The TTS drain loop (stop enqueueing the current sentence).
- The transport's outbound buffer (flush audio already queued for
  playback — so the bot shuts up *immediately*, not "after the
  current chunk").

What we still don't do: update conversation history. The LLM will
think next turn that it said its whole response. Version 9c fixes
that.

Dependencies:
    uv sync --extra quickstart --group dev
    export OPENAI_API_KEY=...
    export DEEPGRAM_API_KEY=...
"""

from __future__ import annotations

import asyncio
import collections
import os
import time
import types
from pathlib import Path

from openai import AsyncOpenAI

from easycat import (
    CancelToken,
    LocalTransport,
    LocalTransportConfig,
    create_stt_provider,
    create_tts_provider,
    create_vad,
)
from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.debug.export import export_debug_bundle
from easycat.events import (
    EventBus,
    STTEventType,
    TTSEventType,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
from easycat.session import split_at_sentence_boundaries
from easycat.strip_markdown import strip_markdown
from easycat.stt.factory import STTProviderConfig
from easycat.tts.factory import TTSProviderConfig
from easycat.tts.input import TTSInput
from easycat.vad import VADConfig

MODEL = "gpt-4o-mini"
PREROLL_FRAMES = 15
RUNS_DIR = Path(__file__).parent / "runs"
SESSION_ID = f"ch09b-cancel-{int(time.time())}"


class MiniTurnDetector:
    """Same as chapter 4."""

    def __init__(self, vad, preroll_frames: int = PREROLL_FRAMES) -> None:
        self._vad = vad
        self._preroll: collections.deque[AudioChunk] = collections.deque(maxlen=preroll_frames)
        self._speaking = False

    async def frames(self, audio_iter):
        async for chunk in audio_iter:
            vad_events = [ev async for ev in self._vad.process(chunk)]
            for ev in vad_events:
                if isinstance(ev, VADStartSpeaking):
                    while self._preroll:
                        yield "speech_started", self._preroll.popleft()
                    self._speaking = True
                elif isinstance(ev, VADStopSpeaking):
                    self._speaking = False
                    yield "speech_ended", None
            if self._speaking:
                yield "frame", chunk
            else:
                self._preroll.append(chunk)


async def mic_producer(detector, transport, queue: asyncio.Queue) -> None:
    async for tag, chunk in detector.frames(transport.receive_audio()):
        await queue.put((tag, chunk))


async def run_agent(client, user_text, sentence_queue, cancel: CancelToken):
    """Consume the agent stream until cancelled."""
    stream = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a helpful voice assistant. "
                    "Give a long-ish answer so the reader has something to interrupt."
                ),
            },
            {"role": "user", "content": user_text},
        ],
        stream=True,
    )
    buffer = ""
    async for chunk in stream:
        if cancel.is_cancelled:
            break
        delta = chunk.choices[0].delta.content or ""
        if not delta:
            continue
        buffer += delta
        ready, buffer = split_at_sentence_boundaries(buffer)
        if ready.strip():
            spoken = strip_markdown(ready).strip()
            if spoken:
                await sentence_queue.put(spoken)
    if buffer.strip() and not cancel.is_cancelled:
        spoken = strip_markdown(buffer).strip()
        if spoken:
            await sentence_queue.put(spoken)
    await sentence_queue.put(None)


async def drain_to_speaker(tts, transport, sentence_queue, cancel: CancelToken, journal):
    """Synthesise and play, checking cancel between chunks."""
    while True:
        sentence = await sentence_queue.get()
        if sentence is None or cancel.is_cancelled:
            break
        synth_start = time.monotonic()
        async for event in tts.synthesize(TTSInput(text=sentence)):
            if cancel.is_cancelled:
                await tts.cancel()
                break
            if event.type == TTSEventType.AUDIO and event.audio is not None:
                await transport.send_audio(event.audio)
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="stage.tts.execute",
            session_id=SESSION_ID,
            data={
                "stage": "tts",
                "elapsed_ms": (time.monotonic() - synth_start) * 1000,
                "text": sentence,
                "cancelled": cancel.is_cancelled,
            },
        )


async def coordinator(mic_queue, stt_factory, client, tts, transport, journal):
    stt = None
    bot_task: asyncio.Task | None = None
    active_cancel: CancelToken | None = None

    while True:
        tag, chunk = await mic_queue.get()

        # Barge-in detection: user speech while bot talks → cancel.
        if bot_task is not None and not bot_task.done():
            if tag == "speech_started" and active_cancel is not None:
                journal.append(
                    kind=JournalRecordKind.EVENT,
                    name="interruption.start",
                    session_id=SESSION_ID,
                    data={"stage": "vad", "t_ms": time.monotonic() * 1000},
                )
                active_cancel.cancel()
                # Flush audio already in the speaker queue so the bot
                # shuts up *now*, not after the current chunk finishes.
                await transport.clear_audio()
            continue

        if tag == "speech_started":
            if stt is None:
                stt = stt_factory()
                await stt.start_stream()
            await stt.send_audio(chunk)
        elif tag == "frame" and stt is not None:
            await stt.send_audio(chunk)
        elif tag == "speech_ended" and stt is not None:
            await stt.end_stream()
            final_text = ""
            async for ev in stt.events():
                if ev.type == STTEventType.FINAL:
                    final_text = ev.text
            stt = None
            if not final_text.strip():
                continue
            print(f"  user: {final_text!r}")

            cancel = CancelToken()
            active_cancel = cancel

            async def _bot(text=final_text, ct=cancel):
                q: asyncio.Queue = asyncio.Queue()
                await asyncio.gather(
                    run_agent(client, text, q, ct),
                    drain_to_speaker(tts, transport, q, ct, journal),
                )

            bot_task = asyncio.create_task(_bot())


async def main() -> None:
    if not (os.getenv("OPENAI_API_KEY") and os.getenv("DEEPGRAM_API_KEY")):
        raise SystemExit("Set OPENAI_API_KEY and DEEPGRAM_API_KEY.")

    journal = InMemoryRingBuffer(capacity=10_000)
    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))
    vad = create_vad(VADConfig())
    detector = MiniTurnDetector(vad)
    client = AsyncOpenAI()
    tts = create_tts_provider(
        TTSProviderConfig(provider="openai", settings={"api_key": os.environ["OPENAI_API_KEY"]})
    )

    def stt_factory():
        return create_stt_provider(
            STTProviderConfig(
                provider="deepgram",
                api_key=os.environ["DEEPGRAM_API_KEY"],
                params={"sample_rate": 24000, "event_bus": EventBus()},
            )
        )

    await transport.connect()
    print("Cancel on barge-in. Interrupt freely. Ctrl-C to stop.\n")

    mic_queue: asyncio.Queue = asyncio.Queue()
    try:
        await asyncio.gather(
            mic_producer(detector, transport, mic_queue),
            coordinator(mic_queue, stt_factory, client, tts, transport, journal),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
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
