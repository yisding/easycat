"""Chapter 9a — ignore barge-in.

The bot plows through its full reply no matter what you do. Like
talking to an answering machine. This is the baseline.

The architectural change vs chapter 6 is *mic stays live during
bot speech*. We detect user speech while the bot is talking — we
just deliberately do nothing about it. Chapter 9b / 9c will act on
the same signal.

Dependencies:
    uv sync --extra quickstart --extra deepgram --group dev
    export OPENAI_API_KEY=...
    export DEEPGRAM_API_KEY=...
    uv run easycat doctor
    uv run easycat doctor --env-file .env         # if keys live in .env
    uv run easycat doctor --env-file .env --json  # for parseable checks
    Add `--env-file .env` after `uv run` on script commands if keys live in `.env`.
"""

from __future__ import annotations

import asyncio
import collections
import os
import time
import types
from contextlib import AsyncExitStack
from pathlib import Path

from openai import AsyncOpenAI

from easycat import LocalTransportConfig
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
from easycat.runtime.capabilities import close_if_supported
from easycat.session import split_at_sentence_boundaries
from easycat.strip_markdown import strip_markdown
from easycat.stt.factory import STTProviderConfig, create_stt_provider
from easycat.transports.local import LocalTransport
from easycat.tts.factory import TTSProviderConfig, create_tts_provider
from easycat.tts.input import TTSInput
from easycat.vad import VADConfig
from easycat.vad.factory import create_vad

MODEL = "gpt-4o-mini"
PREROLL_FRAMES = 15
RUNS_DIR = Path(__file__).parent / "runs"
SESSION_ID = f"ch09a-ignore-{int(time.time())}"


class MiniTurnDetector:
    """Same as chapters 4-6."""

    def __init__(self, vad, preroll_frames: int = PREROLL_FRAMES) -> None:
        self._vad = vad
        self._preroll: collections.deque[AudioChunk] = collections.deque(maxlen=preroll_frames)
        self._speaking = False

    async def frames(self, audio_iter):
        async for chunk in audio_iter:
            vad_events = [ev async for ev in self._vad.process(chunk)]
            for ev in vad_events:
                if isinstance(ev, VADStartSpeaking):
                    self._speaking = True
                    yield "speech_started", None
                    while self._preroll:
                        yield "frame", self._preroll.popleft()
                elif isinstance(ev, VADStopSpeaking):
                    self._speaking = False
                    yield "speech_ended", None
            if self._speaking:
                yield "frame", chunk
            else:
                self._preroll.append(chunk)


async def mic_producer(detector, transport, queue: asyncio.Queue) -> None:
    """Keep the VAD+detector running at all times.

    Regardless of who is talking, every user speech boundary lands
    on the queue. Chapter 9a ignores barge-ins during bot speech;
    9b / 9c act on them.
    """
    async for tag, chunk in detector.frames(transport.receive_audio()):
        await queue.put((tag, chunk))


async def run_agent(client, user_text, sentence_queue):
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
        delta = chunk.choices[0].delta.content or ""
        if not delta:
            continue
        buffer += delta
        ready, buffer = split_at_sentence_boundaries(buffer)
        if ready.strip():
            spoken = strip_markdown(ready).strip()
            if spoken:
                await sentence_queue.put(spoken)
    if buffer.strip():
        spoken = strip_markdown(buffer).strip()
        if spoken:
            await sentence_queue.put(spoken)
    await sentence_queue.put(None)


async def drain_to_speaker(tts, transport, sentence_queue, journal):
    while True:
        sentence = await sentence_queue.get()
        if sentence is None:
            break
        synth_start = time.monotonic()
        async for event in tts.synthesize(TTSInput(text=sentence)):
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
            },
        )


async def observe_bot_task(bot_task: asyncio.Task, journal) -> None:
    """Retrieve a background result so failures never become orphan warnings."""
    (result,) = await asyncio.gather(bot_task, return_exceptions=True)
    if isinstance(result, Exception):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="bot_task.error",
            session_id=SESSION_ID,
            data={"stage": "coordinator", "error": repr(result)},
        )


async def finish_stt_turn(stt) -> str:
    """End, drain, and close one completed STT turn."""
    try:
        await stt.end_stream()
        final_text = ""
        async for event in stt.events():
            if event.type == STTEventType.FINAL:
                final_text = event.text
        return final_text
    finally:
        await close_if_supported(stt)


async def close_started_stt(stt) -> None:
    """End and close an STT turn whose speech boundary never arrived."""
    try:
        await stt.end_stream()
    finally:
        await close_if_supported(stt)


async def shutdown_coordinator(stt, bot_task, journal) -> None:
    """Release both possible in-flight owners before shared providers close."""
    try:
        if stt is not None:
            await close_started_stt(stt)
    finally:
        if bot_task is not None:
            if not bot_task.done():
                bot_task.cancel()
            await observe_bot_task(bot_task, journal)


async def route_ignored_event(tag: str, bot_task: asyncio.Task | None, journal):
    """Observe completed work or consume an event while the bot is active."""
    if bot_task is None:
        return bot_task, False
    if bot_task.done():
        await observe_bot_task(bot_task, journal)
        return None, False
    if tag == "speech_started":
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="user.barge_in.ignored",
            session_id=SESSION_ID,
            data={"stage": "vad", "t_ms": time.monotonic() * 1000},
        )
    return bot_task, True


async def coordinator(mic_queue, stt_factory, client, tts, transport, journal):
    stt = None
    bot_task: asyncio.Task | None = None

    try:
        while True:
            tag, chunk = await mic_queue.get()

            # During bot speech, log-only — the chapter's whole point.
            bot_task, consumed = await route_ignored_event(tag, bot_task, journal)
            if consumed:
                continue

            if tag == "speech_started":
                if stt is None:
                    stt = stt_factory()
                    await stt.start_stream()
            elif tag == "frame" and stt is not None:
                await stt.send_audio(chunk)
            elif tag == "speech_ended" and stt is not None:
                active_stt = stt
                stt = None
                final_text = await finish_stt_turn(active_stt)
                if not final_text.strip():
                    continue
                print(f"  user: {final_text!r}")

                async def _bot(text=final_text):
                    q: asyncio.Queue = asyncio.Queue()
                    await asyncio.gather(
                        run_agent(client, text, q),
                        drain_to_speaker(tts, transport, q, journal),
                    )

                bot_task = asyncio.create_task(_bot())
    finally:
        await shutdown_coordinator(stt, bot_task, journal)


async def main() -> None:
    if not (os.getenv("OPENAI_API_KEY") and os.getenv("DEEPGRAM_API_KEY")):
        raise SystemExit("Set OPENAI_API_KEY and DEEPGRAM_API_KEY.")

    journal = InMemoryRingBuffer(capacity=10_000)
    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))

    def stt_factory():
        return create_stt_provider(
            STTProviderConfig(
                provider="deepgram",
                api_key=os.environ["DEEPGRAM_API_KEY"],
                params={"sample_rate": 24000, "event_bus": EventBus()},
            )
        )

    async with AsyncExitStack() as resources:
        resources.push_async_callback(transport.disconnect)
        await transport.connect()

        vad = create_vad(VADConfig())
        resources.push_async_callback(close_if_supported, vad)
        detector = MiniTurnDetector(vad)

        client = AsyncOpenAI()
        resources.push_async_callback(close_if_supported, client)
        tts = create_tts_provider(
            TTSProviderConfig(provider="openai", api_key=os.environ["OPENAI_API_KEY"])
        )
        resources.push_async_callback(close_if_supported, tts)

        print("Ignore barge-in. Ask something, then try to interrupt. Ctrl-C to stop.\n")

        mic_queue: asyncio.Queue = asyncio.Queue()
        try:
            await asyncio.gather(
                mic_producer(detector, transport, mic_queue),
                coordinator(mic_queue, stt_factory, client, tts, transport, journal),
            )
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
