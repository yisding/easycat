"""Chapter 6 — Streaming agent + sentence-boundary TTS.

Instead of waiting for the whole LLM response, stream tokens as
they arrive, split on sentence boundaries, and hand each sentence
to TTS as soon as it's complete. Sentence N+1 synthesises while
sentence N is still playing.

First-audio latency drops by ~3× versus chapter 5.

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

PREROLL_FRAMES = 15
MODEL = "gpt-4o-mini"
RUNS_DIR = Path(__file__).parent / "runs"
SESSION_ID = f"ch06-streaming-{int(time.time())}"


class MiniTurnDetector:
    """Same as chapters 4 & 5."""

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


async def stream_sentences_to_tts(
    client: AsyncOpenAI,
    user_text: str,
    sentence_queue: asyncio.Queue[str | None],
    journal: InMemoryRingBuffer,
) -> None:
    """Iterate the LLM's token stream; flush sentence-by-sentence to the queue.

    We accumulate tokens, then after each delta check whether a complete
    sentence exists at the start of the buffer. If so, push it to the
    sentence queue so the TTS drain coroutine can start synth immediately.
    """
    stream = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "You are a helpful voice assistant. Keep it brief."},
            {"role": "user", "content": user_text},
        ],
        stream=True,
    )

    buffer = ""
    first_token_t: float | None = None
    async for chunk in stream:
        delta = chunk.choices[0].delta.content or ""
        if not delta:
            continue
        if first_token_t is None:
            first_token_t = time.monotonic()
            journal.append(
                kind=JournalRecordKind.EVENT,
                name="agent.first_token",
                session_id=SESSION_ID,
                data={"stage": "agent", "t_ms": first_token_t * 1000},
            )
        buffer += delta

        # split_at_sentence_boundaries returns (ready, leftover). ``ready``
        # is a prefix of complete sentences; ``leftover`` is the dangling
        # tail we keep buffering.
        ready, buffer = split_at_sentence_boundaries(buffer)
        if ready.strip():
            spoken = strip_markdown(ready).strip()
            if spoken:
                await sentence_queue.put(spoken)
                journal.append(
                    kind=JournalRecordKind.EVENT,
                    name="agent.sentence",
                    session_id=SESSION_ID,
                    data={"stage": "agent", "text": spoken},
                )

    # Flush any trailing text the LLM ended mid-sentence (no terminal
    # punctuation). The production consume_agent_stream also guards with
    # has_unclosed_markdown_delimiters; we keep the toy simple.
    if buffer.strip():
        spoken = strip_markdown(buffer).strip()
        if spoken:
            await sentence_queue.put(spoken)
    await sentence_queue.put(None)


async def drain_sentences_to_speaker(
    tts, transport, sentence_queue: asyncio.Queue[str | None], journal: InMemoryRingBuffer
) -> tuple[float | None, int, int]:
    """Take one sentence at a time, synthesise, stream audio to speaker.

    Because ``transport.send_audio`` returns as soon as the chunk is
    enqueued for playback, the next ``tts.synthesize`` can start while
    the current sentence is still audible. That is the pipeline overlap.
    """
    first_audio_t: float | None = None
    accepted_chunks = rejected_chunks = 0
    while True:
        sentence = await sentence_queue.get()
        if sentence is None:
            break

        synth_start = time.monotonic()
        sentence_accepted = sentence_rejected = 0
        async for event in tts.synthesize(TTSInput(text=sentence)):
            if event.type == TTSEventType.AUDIO and event.audio is not None:
                accepted = await transport.send_audio(event.audio)
                if accepted:
                    accepted_chunks += 1
                    sentence_accepted += 1
                    if first_audio_t is None:
                        first_audio_t = time.monotonic()
                        journal.append(
                            kind=JournalRecordKind.EVENT,
                            name="tts.first_audio",
                            session_id=SESSION_ID,
                            data={"stage": "tts", "t_ms": first_audio_t * 1000},
                        )
                else:
                    rejected_chunks += 1
                    sentence_rejected += 1
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="stage.tts.execute",
            session_id=SESSION_ID,
            data={
                "stage": "tts",
                "elapsed_ms": (time.monotonic() - synth_start) * 1000,
                "accepted_chunks": sentence_accepted,
                "rejected_chunks": sentence_rejected,
                "text": sentence,
            },
        )
    return first_audio_t, accepted_chunks, rejected_chunks


async def run_turn(transport, stt, client, tts, journal) -> None:
    """STT-final → fan out to LLM-stream → sentence-queue → TTS-drain."""
    final_text = ""
    stt_final_t = None
    async for event in stt.events():
        if event.type == STTEventType.FINAL:
            final_text = event.text
            stt_final_t = time.monotonic()

    if not final_text.strip() or stt_final_t is None:
        return

    journal.append(
        kind=JournalRecordKind.EVENT,
        name="stt.final",
        session_id=SESSION_ID,
        data={"stage": "stt", "text": final_text, "t_ms": stt_final_t * 1000},
    )
    print(f"  user: {final_text!r}")
    sentence_queue: asyncio.Queue[str | None] = asyncio.Queue()
    _, delivery = await asyncio.gather(
        stream_sentences_to_tts(client, final_text, sentence_queue, journal),
        drain_sentences_to_speaker(tts, transport, sentence_queue, journal),
    )
    first_audio_t, accepted_chunks, rejected_chunks = delivery
    reply_enqueue_gap = (time.monotonic() - stt_final_t) * 1000
    total_gap = None if first_audio_t is None else (first_audio_t - stt_final_t) * 1000
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="turn.gap",
        session_id=SESSION_ID,
        data={
            "stage": "turn",
            "total_gap_ms": total_gap,
            "reply_enqueue_gap_ms": reply_enqueue_gap,
            "tts_accepted_chunks": accepted_chunks,
            "tts_rejected_chunks": rejected_chunks,
            "text": final_text,
        },
    )
    if total_gap is None:
        if accepted_chunks:
            print("  (turn gap unavailable — accepted TTS audio had no timestamp)")
        elif rejected_chunks:
            print(
                f"  (turn gap unavailable — transport rejected all {rejected_chunks} TTS chunks)"
            )
        else:
            print("  (turn gap unavailable — TTS produced no audio)")
    else:
        print(f"  (turn gap: {total_gap:.0f} ms — STT final → first audio enqueued)")


async def collect_turns(transport, detector, stt_factory, client, tts, journal) -> None:
    """Stream turns and close every per-turn STT, including on cancellation."""
    stt = None
    try:
        async for tag, chunk in detector.frames(transport.receive_audio()):
            if tag == "speech_started":
                if stt is None:
                    stt = stt_factory()
                    await stt.start_stream()
            elif tag == "frame" and stt is not None:
                await stt.send_audio(chunk)
            elif tag == "speech_ended" and stt is not None:
                active_stt = stt
                stt = None
                try:
                    await active_stt.end_stream()
                    await run_turn(transport, active_stt, client, tts, journal)
                finally:
                    await close_if_supported(active_stt)
    finally:
        if stt is not None:
            try:
                await stt.end_stream()
            finally:
                await close_if_supported(stt)


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

        print("Streaming agent. Ctrl-C to stop.\n")

        try:
            await collect_turns(transport, detector, stt_factory, client, tts, journal)
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
