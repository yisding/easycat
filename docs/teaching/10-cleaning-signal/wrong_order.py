"""Chapter 10 — wrong-version-first for pipeline ordering.

The same NR + AEC + VAD components as `main.py`, wired wrong. Two
modes, both technically running every component, both producing a
bundle that looks healthy on a surface scan — but the journal
shows neither one does anything useful.

    --mode nr-after-vad      NR runs *after* VAD has already decided.
                             VAD's verdicts are unchanged → false-fires
                             on keystrokes and fan noise persist.

    --mode aec-no-reference  AEC runs but no one ever calls
                             feed_reference(). The adaptive filter has
                             nothing to subtract → bot interrupts itself
                             on speakerphone exactly as if AEC were off.

Both modes show "✓ NR backend loaded" and "✓ AEC backend loaded"
to the user, exactly like `main.py`. The journal is the only place
the bug is visible.

Run on speakerphone (no headphones) for the AEC mode to land —
otherwise there's no echo to cancel and the mis-wiring is invisible.

Dependencies:
    uv sync --extra quickstart --group dev
    For real NR/AEC: uv pip install -e '.[rnnoise,aec]'
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

from easycat import CancelToken, LocalTransportConfig
from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.debug.export import export_debug_bundle
from easycat.echo_cancellation import EchoCancellationConfig, create_echo_canceller
from easycat.events import (
    EventBus,
    STTEventType,
    TTSEventType,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
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


class MiniTurnDetector:
    def __init__(self, vad, preroll_frames: int = PREROLL_FRAMES) -> None:
        self._vad = vad
        self._preroll: collections.deque[AudioChunk] = collections.deque(maxlen=preroll_frames)
        self._speaking = False

    async def frames(self, audio_iter):
        async for chunk in audio_iter:
            for ev in [e async for e in self._vad.process(chunk)]:
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


async def wrong_order_pipeline(transport, nr, aec, mode: str, journal, session_id):
    """Run NR + AEC, but in the wrong order or without a reference."""
    if mode == "nr-after-vad":
        # VAD will run inside MiniTurnDetector on raw chunks. NR runs on
        # whatever VAD passes through, *after* the verdict is recorded.
        # The journal records that NR ran but VAD never saw its output.
        async for chunk in transport.receive_audio():
            # AEC still runs in its right place (before VAD), but with a
            # live reference so isolation of the NR ordering is clean.
            chunk = await aec.process(chunk)
            yield chunk
            # NR runs *after* yielding — completely irrelevant to VAD.
            _ = await nr.process(chunk)
            journal.append(
                kind=JournalRecordKind.EVENT,
                name="nr.applied_after_vad",
                session_id=session_id,
                data={"stage": "nr", "t_ms": time.monotonic() * 1000},
            )
    elif mode == "aec-no-reference":
        # NR runs in its right place. AEC runs in its right place too —
        # but we deliberately never call feed_reference() from the TTS
        # drain. AEC's adaptive filter has nothing to subtract.
        async for chunk in transport.receive_audio():
            chunk = await nr.process(chunk)
            chunk = await aec.process(chunk)
            yield chunk
    else:
        raise SystemExit(f"unknown --mode {mode!r}")


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


async def drain_to_speaker(tts, transport, aec, sentence_queue, cancel, session_id, journal, mode):
    """Emit TTS audio. In aec-no-reference mode, we do *not* call feed_reference."""
    feeds = 0
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
                if mode != "aec-no-reference":
                    aec.feed_reference(event.audio)
                    feeds += 1
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="stage.tts.execute",
            session_id=session_id,
            data={"stage": "tts", "text": sentence, "aec_reference_feeds": feeds},
        )
    if mode == "aec-no-reference":
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="aec.no_reference",
            session_id=session_id,
            data={"stage": "aec", "feed_count": 0, "note": "AEC ran but never saw the TTS audio"},
        )


async def coordinator(
    mic_queue, stt_factory, client, tts, transport, aec, session_id, journal, mode
):
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
                    drain_to_speaker(tts, transport, aec, q, ct, session_id, journal, mode),
                )

            bot_task = asyncio.create_task(_bot())


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("nr-after-vad", "aec-no-reference"), required=True)
    args = ap.parse_args()

    if not (os.getenv("OPENAI_API_KEY") and os.getenv("DEEPGRAM_API_KEY")):
        raise SystemExit("Set OPENAI_API_KEY and DEEPGRAM_API_KEY.")

    session_id = f"ch10-wrong-{args.mode}-{int(time.time())}"
    journal = InMemoryRingBuffer(capacity=10_000)

    nr = create_noise_reducer(NoiseReducerConfig())
    aec = create_echo_canceller(EchoCancellationConfig(enabled=True))
    nr_backend = nr.version_info().get("provider", "unknown")
    aec_backend = aec.version_info().get("provider", "unknown")

    print(f"NR backend: {nr_backend}  (loaded ✓)")
    print(f"AEC backend: {aec_backend} (loaded ✓)")
    print(f"Mode: {args.mode}")
    print("Surface check: both stages are 'on.' Read the journal afterwards")
    print("to see why neither one does anything.\n")

    journal.append(
        kind=JournalRecordKind.EVENT,
        name="audio.config",
        session_id=session_id,
        data={
            "stage": "audio",
            "nr": nr_backend,
            "aec": aec_backend,
            "wiring": args.mode,
            "intentionally_wrong": True,
        },
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
    if args.mode == "nr-after-vad":
        print("Type loudly while you talk. NR is loaded but runs after VAD")
        print("has already decided — keystrokes still fire VAD-on. Ctrl-C to stop.\n")
    else:
        print("Use a speakerphone (no headphones) and ask the bot a long question.")
        print("AEC is loaded but never sees the TTS audio — bot will interrupt")
        print("itself on its own voice. Ctrl-C to stop.\n")

    mic_queue: asyncio.Queue = asyncio.Queue()
    cleaned = wrong_order_pipeline(transport, nr, aec, args.mode, journal, session_id)
    try:
        await asyncio.gather(
            mic_producer(detector, cleaned, mic_queue),
            coordinator(
                mic_queue, stt_factory, client, tts, transport, aec, session_id, journal, args.mode
            ),
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
    if args.mode == "nr-after-vad":
        print("Check the journal: count `nr.applied_after_vad` records. Each one is")
        print("an NR call whose output went nowhere. Then check `stage.vad.execute`")
        print("verdicts on keystroke-heavy chunks — they should still fire 'speech.'")
    else:
        print("Check the journal: `aec.no_reference` is the smoking gun.")
        print("Also: `stage.tts.execute` records show `aec_reference_feeds: 0`.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
