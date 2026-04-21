"""Chapter 10 — Cleaning the signal.

Add noise reduction (NR) and acoustic echo cancellation (AEC) to
the ch9 pipeline. Toggle each independently via CLI flags, and
read the journal to see which backend is actually running.

    # Nothing on: the baseline.
    --nr off --aec off

    # NR alone: fan noise, keyboard clicks get filtered.
    --nr on  --aec off

    # AEC alone: bot-through-speaker bleed gets subtracted.
    --nr off --aec on

    # Both: prod-style.
    --nr on  --aec on

NR is single-input — it only sees the mic. AEC is dual-input —
it needs both the mic *and* the far-end reference (the TTS audio
we sent to the speaker). We feed the reference every time we
emit a TTS chunk.

Dependencies:
    uv sync --extra quickstart --group dev
    For real NR:   uv pip install -e '.[rnnoise]'
    For real AEC:  uv pip install -e '.[aec]'
    Otherwise both silently fall back to passthrough — the
    journal tells you which backend is live.

    export OPENAI_API_KEY=...
    export DEEPGRAM_API_KEY=...
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import time
import types
from pathlib import Path

from openai import AsyncOpenAI

from easycat import (
    CancelToken,
    EchoCancellationConfig,
    LocalTransport,
    LocalTransportConfig,
    NoiseReducerConfig,
    create_echo_canceller,
    create_noise_reducer,
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


class MiniTurnDetector:
    """Unchanged from chapter 4."""

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


async def clean_audio_pipeline(transport, nr, aec):
    """Pipeline order: transport → NR → AEC → (downstream).

    NR runs first so it sees the rawest noise spectrum possible.
    AEC then subtracts the bot's own voice (reference-fed elsewhere).
    VAD and STT live downstream in the detector + coordinator.
    """
    async for chunk in transport.receive_audio():
        chunk = await nr.process(chunk)
        chunk = await aec.process(chunk)
        yield chunk


async def mic_producer(detector, cleaned_audio, queue: asyncio.Queue) -> None:
    async for tag, chunk in detector.frames(cleaned_audio):
        await queue.put((tag, chunk))


async def run_agent(client, user_text, sentence_queue, cancel: CancelToken):
    stream = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "You are a helpful voice assistant. Keep it brief."},
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


async def drain_to_speaker(tts, transport, aec, sentence_queue, cancel, session_id, journal):
    """Emit TTS audio to the speaker AND feed it to AEC as the far-end reference."""
    while True:
        sentence = await sentence_queue.get()
        if sentence is None or cancel.is_cancelled:
            break
        async for event in tts.synthesize(TTSInput(text=sentence)):
            if cancel.is_cancelled:
                await tts.cancel()
                break
            if event.type == TTSEventType.AUDIO and event.audio is not None:
                await transport.send_audio(event.audio)
                # The crucial dual-input line: AEC needs to know what we
                # asked the speaker to play, so it can subtract that
                # pattern from the mic.
                aec.feed_reference(event.audio)
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="stage.tts.execute",
            session_id=session_id,
            data={"stage": "tts", "text": sentence},
        )


async def coordinator(mic_queue, stt_factory, client, tts, transport, aec, session_id, journal):
    stt = None
    bot_task: asyncio.Task | None = None
    active_cancel: CancelToken | None = None

    while True:
        tag, chunk = await mic_queue.get()

        if bot_task is not None and not bot_task.done():
            if tag == "speech_started" and active_cancel is not None:
                journal.append(
                    kind=JournalRecordKind.EVENT,
                    name="interruption.start",
                    session_id=session_id,
                    data={"stage": "vad", "t_ms": time.monotonic() * 1000},
                )
                active_cancel.cancel()
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
                    drain_to_speaker(tts, transport, aec, q, ct, session_id, journal),
                )

            bot_task = asyncio.create_task(_bot())


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nr", choices=("on", "off"), default="off")
    ap.add_argument("--aec", choices=("on", "off"), default="off")
    args = ap.parse_args()

    if not (os.getenv("OPENAI_API_KEY") and os.getenv("DEEPGRAM_API_KEY")):
        raise SystemExit("Set OPENAI_API_KEY and DEEPGRAM_API_KEY.")

    session_id = f"ch10-nr{args.nr}-aec{args.aec}-{int(time.time())}"
    journal = InMemoryRingBuffer(capacity=10_000)

    # Factory-wired stages. NR/AEC both fall back to passthrough if the
    # optional deps aren't installed; the journal records which one is live.
    if args.nr == "on":
        nr = create_noise_reducer(NoiseReducerConfig())
        nr_backend = nr.version_info().get("provider", "unknown")
    else:
        nr = _Passthrough()
        nr_backend = "off"

    if args.aec == "on":
        aec = create_echo_canceller(EchoCancellationConfig(enabled=True))
        aec_backend = aec.version_info().get("provider", "unknown")
    else:
        aec = create_echo_canceller(EchoCancellationConfig(enabled=False))
        aec_backend = "off"

    print(f"NR backend: {nr_backend}    AEC backend: {aec_backend}")
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="audio.config",
        session_id=session_id,
        data={"stage": "audio", "nr": nr_backend, "aec": aec_backend},
    )

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
    print("Talk. Ctrl-C to stop.\n")

    mic_queue: asyncio.Queue = asyncio.Queue()
    cleaned = clean_audio_pipeline(transport, nr, aec)
    try:
        await asyncio.gather(
            mic_producer(detector, cleaned, mic_queue),
            coordinator(mic_queue, stt_factory, client, tts, transport, aec, session_id, journal),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await transport.disconnect()

    RUNS_DIR.mkdir(exist_ok=True)
    bundle_path = RUNS_DIR / f"{session_id}.bundle"
    session_stub = types.SimpleNamespace(journal=journal)
    export_debug_bundle(session_stub, bundle_path, overwrite=True)
    print(f"\nWrote bundle → {bundle_path.relative_to(Path.cwd())}")


class _Passthrough:
    """Stand-in for --nr off / --aec off paths: no-op both directions."""

    async def process(self, chunk):
        return chunk

    def feed_reference(self, chunk):
        pass

    def version_info(self):
        return {"provider": "off"}


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
