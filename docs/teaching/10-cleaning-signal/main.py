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
we sent to the speaker). We feed the reference every time the
transport accepts a complete TTS chunk.

Dependencies:
    uv sync --extra quickstart --extra deepgram --group dev
    RNNoise is included in quickstart; Krisp requires its own SDK.
    For real AEC:  uv sync --extra aec --group dev
    Missing selected backends fall back to passthrough — the
    journal tells you which backend is live.

    export OPENAI_API_KEY=...
    export DEEPGRAM_API_KEY=...
    uv run easycat doctor
    uv run easycat doctor --env-file .env         # if keys live in .env
    uv run easycat doctor --env-file .env --json  # for parseable checks
    Add `--env-file .env` after `uv run` on script commands if keys live in `.env`.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import time
import types
from contextlib import AsyncExitStack
from pathlib import Path

from openai import AsyncOpenAI

from easycat import (
    CancelToken,
    LocalTransportConfig,
)
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


class _Passthrough:
    """Stand-in for --nr off / --aec off paths: no-op both directions.

    Matches ``replay.py``'s passthrough so both entry points take the
    same shape when a stage is disabled.
    """

    async def process(self, chunk):
        return chunk

    def feed_reference(self, chunk):
        pass

    def version_info(self):
        return {"provider": "off"}


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
    """Emit TTS audio and feed accepted chunks to AEC as the far-end reference."""
    while True:
        sentence = await sentence_queue.get()
        if sentence is None or cancel.is_cancelled:
            break
        async for event in tts.synthesize(TTSInput(text=sentence)):
            if cancel.is_cancelled:
                await tts.cancel()
                break
            if event.type == TTSEventType.AUDIO and event.audio is not None:
                if await transport.send_audio(event.audio):
                    # The crucial dual-input line: AEC needs to know what
                    # the speaker accepted, so it can subtract that pattern
                    # from the mic. Rejected or partial writes return False.
                    aec.feed_reference(event.audio)
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="stage.tts.execute",
            session_id=session_id,
            data={"stage": "tts", "text": sentence},
        )


async def observe_bot_task(bot_task: asyncio.Task, journal, session_id: str) -> None:
    """Retrieve a background result so failures never become orphan warnings."""
    (result,) = await asyncio.gather(bot_task, return_exceptions=True)
    if isinstance(result, Exception):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="bot_task.error",
            session_id=session_id,
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


async def shutdown_coordinator(stt, bot_task, active_cancel, journal, session_id) -> None:
    """Release both possible in-flight owners before shared providers close."""
    try:
        if stt is not None:
            await close_started_stt(stt)
    finally:
        if bot_task is not None:
            if active_cancel is not None:
                active_cancel.cancel()
            if not bot_task.done():
                bot_task.cancel()
            await observe_bot_task(bot_task, journal, session_id)


async def route_barge_in(tag, bot_task, active_cancel, transport, journal, session_id):
    """Cancel active output while preserving speech_started for STT below."""
    if bot_task is None:
        return bot_task, active_cancel, False
    if bot_task.done():
        await observe_bot_task(bot_task, journal, session_id)
        return None, None, False
    if tag != "speech_started" or active_cancel is None:
        return bot_task, active_cancel, True

    started_at = time.monotonic()
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="interruption.start",
        session_id=session_id,
        data={"stage": "vad", "t_ms": started_at * 1000},
    )
    active_cancel.cancel()
    await transport.clear_audio()
    clear_returned_at = time.monotonic()
    await observe_bot_task(bot_task, journal, session_id)
    bot_returned_at = time.monotonic()
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="interruption.cancel_complete",
        session_id=session_id,
        data={
            "stage": "interruption",
            "cancel_to_clear_audio_return_ms": (clear_returned_at - started_at) * 1000,
            "cancel_to_bot_task_return_ms": (bot_returned_at - started_at) * 1000,
            "t_ms": bot_returned_at * 1000,
        },
    )
    return None, None, False


async def coordinator(mic_queue, stt_factory, client, tts, transport, aec, session_id, journal):
    stt = None
    bot_task: asyncio.Task | None = None
    active_cancel: CancelToken | None = None

    try:
        while True:
            tag, chunk = await mic_queue.get()

            bot_task, active_cancel, consumed = await route_barge_in(
                tag, bot_task, active_cancel, transport, journal, session_id
            )
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

                cancel = CancelToken()
                active_cancel = cancel

                async def _bot(text=final_text, ct=cancel):
                    q: asyncio.Queue = asyncio.Queue()
                    await asyncio.gather(
                        run_agent(client, text, q, ct),
                        drain_to_speaker(tts, transport, aec, q, ct, session_id, journal),
                    )

                bot_task = asyncio.create_task(_bot())
    finally:
        await shutdown_coordinator(stt, bot_task, active_cancel, journal, session_id)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nr", choices=("on", "off"), default="off")
    ap.add_argument("--aec", choices=("on", "off"), default="off")
    args = ap.parse_args()

    if not (os.getenv("OPENAI_API_KEY") and os.getenv("DEEPGRAM_API_KEY")):
        raise SystemExit("Set OPENAI_API_KEY and DEEPGRAM_API_KEY.")

    session_id = f"ch10-nr{args.nr}-aec{args.aec}-{int(time.time())}"
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

        # Factory-wired stages. NR/AEC both fall back to passthrough if the
        # optional deps aren't installed; the journal records which one is live.
        if args.nr == "on":
            nr = create_noise_reducer(NoiseReducerConfig())
            nr_backend = nr.version_info().get("provider", "unknown")
        else:
            nr = _Passthrough()
            nr_backend = "off"
        resources.push_async_callback(close_if_supported, nr)

        if args.aec == "on":
            aec = create_echo_canceller(EchoCancellationConfig(enabled=True))
            aec_backend = aec.version_info().get("provider", "unknown")
        else:
            aec = _Passthrough()
            aec_backend = "off"
        resources.push_async_callback(close_if_supported, aec)

        print(f"NR backend: {nr_backend}    AEC backend: {aec_backend}")
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="audio.config",
            session_id=session_id,
            data={"stage": "audio", "nr": nr_backend, "aec": aec_backend},
        )

        vad = create_vad(VADConfig())
        resources.push_async_callback(close_if_supported, vad)
        detector = MiniTurnDetector(vad)

        client = AsyncOpenAI()
        resources.push_async_callback(close_if_supported, client)
        tts = create_tts_provider(
            TTSProviderConfig(provider="openai", api_key=os.environ["OPENAI_API_KEY"])
        )
        resources.push_async_callback(close_if_supported, tts)

        print("Talk. Ctrl-C to stop.\n")

        mic_queue: asyncio.Queue = asyncio.Queue()
        cleaned = clean_audio_pipeline(transport, nr, aec)
        try:
            await asyncio.gather(
                mic_producer(detector, cleaned, mic_queue),
                coordinator(
                    mic_queue, stt_factory, client, tts, transport, aec, session_id, journal
                ),
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    RUNS_DIR.mkdir(exist_ok=True)
    bundle_path = RUNS_DIR / f"{session_id}.bundle"
    session_stub = types.SimpleNamespace(journal=journal)
    export_debug_bundle(session_stub, bundle_path, overwrite=True)
    print(f"\nWrote bundle → {bundle_path.relative_to(Path.cwd())}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
