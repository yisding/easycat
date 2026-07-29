"""Chapter 9c — cancel + estimate what the user could have heard.

Same as ``cancel.py`` plus: we track bytes of TTS audio accepted by
the transport before the cancel fires, compute the character position
in the bot's *text* reply that corresponds to those bytes, and rewrite
the assistant turn in the conversation history to end there. Next turn,
the LLM has a closer picture of what the user could have heard.

The byte-to-char estimate is deliberately simple: accepted bytes ÷
expected bytes × total chars. The production
`easycat.session.interruption` estimator is a lot more careful
about silence, SSML, markdown, and playback-ack fudge factors —
read it after you understand the toy.

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
from collections.abc import Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI

from easycat import CancelToken, LocalTransportConfig
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

MODEL = "gpt-5.6-luna"
PREROLL_FRAMES = 15
RUNS_DIR = Path(__file__).parent / "runs"
SESSION_ID = f"ch09c-estimate-{int(time.time())}"

# OpenAI TTS emits PCM16 mono at 24 kHz = 48,000 bytes/second.
TTS_BYTES_PER_SECOND = 24_000 * 2
LOCAL_OUTPUT_FRAME_MS = 20


@dataclass
class TurnLedger:
    """Per-turn record of what the bot tried to say vs. what was accepted.

    ``sentences_sent`` accumulates the text of each sentence dispatched
    to TTS in order. ``bytes_accepted`` tracks audio bytes for which
    ``transport.send_audio`` returned ``True``. At cancel time we combine
    them to estimate where, in the concatenated text, the user's ear
    fell silent.
    """

    sentences_sent: list[str] = field(default_factory=list)
    bytes_accepted: int = 0

    def heard_text(self) -> str:
        """Estimate the text prefix the user's ear actually reached.

        Audio bytes map directly to playback duration (OpenAI TTS
        emits a fixed-rate stream). Convert duration to characters
        via the expected full-text byte count; clamp to the real
        length so a complete turn returns the whole string.
        """
        if not self.sentences_sent:
            return ""
        full_text = " ".join(self.sentences_sent)
        expected = max(1, _expected_bytes(full_text))
        estimated_chars = int(len(full_text) * self.bytes_accepted / expected)
        estimated_chars = max(0, min(estimated_chars, len(full_text)))
        return full_text[:estimated_chars]


def _expected_bytes(text: str) -> int:
    """Very rough ~15 chars/s of speech at 48000 bytes/s of TTS audio."""
    chars_per_sec = 15
    seconds = len(text) / chars_per_sec
    return int(seconds * TTS_BYTES_PER_SECOND)


def _local_output_frames(chunk: AudioChunk) -> Iterator[AudioChunk]:
    """Split TTS audio into all-or-nothing LocalTransport queue writes."""
    frame_bytes = (
        chunk.format.sample_rate * chunk.format.frame_size * LOCAL_OUTPUT_FRAME_MS // 1000
    )
    for offset in range(0, len(chunk.data), frame_bytes):
        yield AudioChunk(
            data=chunk.data[offset : offset + frame_bytes],
            format=chunk.format,
            timestamp=chunk.timestamp,
        )


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


async def mic_producer(detector, transport, queue: asyncio.Queue) -> None:
    async for tag, chunk in detector.frames(transport.receive_audio()):
        await queue.put((tag, chunk))


async def run_agent(client, history, sentence_queue, cancel: CancelToken):
    stream = await client.chat.completions.create(
        model=MODEL,
        reasoning_effort="none",
        messages=history,
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


async def drain_to_speaker(tts, transport, sentence_queue, cancel, ledger, journal):
    while True:
        sentence = await sentence_queue.get()
        if sentence is None or cancel.is_cancelled:
            break
        ledger.sentences_sent.append(sentence)
        async for event in tts.synthesize(TTSInput(text=sentence)):
            if cancel.is_cancelled:
                await tts.cancel()
                break
            if event.type == TTSEventType.AUDIO and event.audio is not None:
                # LocalTransport reports False for a partial fit. Sending one
                # callback-sized frame at a time makes acceptance atomic, so
                # the ledger can still credit an accepted head accurately.
                for frame in _local_output_frames(event.audio):
                    if cancel.is_cancelled:
                        await tts.cancel()
                        break
                    if await transport.send_audio(frame):
                        ledger.bytes_accepted += len(frame.data)
                if cancel.is_cancelled:
                    break
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="stage.tts.execute",
            session_id=SESSION_ID,
            data={
                "stage": "tts",
                "text": sentence,
                "bytes_accepted_so_far": ledger.bytes_accepted,
                "cancelled": cancel.is_cancelled,
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


async def shutdown_coordinator(stt, bot_task, active_cancel, journal) -> None:
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
            await observe_bot_task(bot_task, journal)


async def route_barge_in(tag, bot_task, active_cancel, active_ledger, transport, journal, history):
    """Cancel output, rewrite history, and preserve speech_started for STT."""
    if bot_task is None:
        return bot_task, active_cancel, active_ledger, False
    if bot_task.done():
        await observe_bot_task(bot_task, journal)
        return None, None, None, False
    if tag != "speech_started" or active_cancel is None or active_ledger is None:
        return bot_task, active_cancel, active_ledger, True

    started_at = time.monotonic()
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="interruption.start",
        session_id=SESSION_ID,
        data={"stage": "vad", "t_ms": started_at * 1000},
    )
    active_cancel.cancel()
    await transport.clear_audio()
    clear_returned_at = time.monotonic()
    await observe_bot_task(bot_task, journal)
    bot_returned_at = time.monotonic()
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="interruption.cancel_complete",
        session_id=SESSION_ID,
        data={
            "stage": "interruption",
            "cancel_to_clear_audio_return_ms": (clear_returned_at - started_at) * 1000,
            "cancel_to_bot_task_return_ms": (bot_returned_at - started_at) * 1000,
            "t_ms": bot_returned_at * 1000,
        },
    )

    heard = active_ledger.heard_text()
    full = " ".join(active_ledger.sentences_sent)
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="interruption.estimate",
        session_id=SESSION_ID,
        data={
            "stage": "interruption",
            "full_text": full,
            "heard_text": heard,
            "bytes_accepted": active_ledger.bytes_accepted,
        },
    )
    history.append({"role": "assistant", "content": heard})
    print(f"  bot (cut): {heard!r}")
    return None, None, None, False


async def coordinator(mic_queue, stt_factory, client, tts, transport, journal):
    """Maintain a multi-turn history and rewrite it on cancel."""
    history: list[dict] = [
        {
            "role": "system",
            "content": (
                "You are a helpful voice assistant. "
                "Give a long-ish answer so the reader has something to interrupt."
            ),
        }
    ]
    stt = None
    bot_task: asyncio.Task | None = None
    active_cancel: CancelToken | None = None
    active_ledger: TurnLedger | None = None

    try:
        while True:
            tag, chunk = await mic_queue.get()

            # Cancel output, but fall through with speech_started intact.
            bot_task, active_cancel, active_ledger, consumed = await route_barge_in(
                tag,
                bot_task,
                active_cancel,
                active_ledger,
                transport,
                journal,
                history,
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
                history.append({"role": "user", "content": final_text})

                cancel = CancelToken()
                ledger = TurnLedger()
                active_cancel = cancel
                active_ledger = ledger

                async def _bot(hist=list(history), ct=cancel, led=ledger):
                    q: asyncio.Queue = asyncio.Queue()
                    await asyncio.gather(
                        run_agent(client, hist, q, ct),
                        drain_to_speaker(tts, transport, q, ct, led, journal),
                    )
                    if not ct.is_cancelled:
                        # Clean completion: record the full reply in history.
                        full = " ".join(led.sentences_sent)
                        history.append({"role": "assistant", "content": full})
                        print(f"  bot: {full!r}")

                bot_task = asyncio.create_task(_bot())
    finally:
        await shutdown_coordinator(stt, bot_task, active_cancel, journal)


async def main() -> None:
    if not (os.getenv("OPENAI_API_KEY") and os.getenv("DEEPGRAM_API_KEY")):
        raise SystemExit("Set OPENAI_API_KEY and DEEPGRAM_API_KEY.")

    journal = InMemoryRingBuffer(capacity=10_000)
    transport = LocalTransport(
        LocalTransportConfig(
            audio_format=PCM16_MONO_24K,
            frame_duration_ms=LOCAL_OUTPUT_FRAME_MS,
        )
    )

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

        print("Cancel + history rewrite. Interrupt freely. Ctrl-C to stop.\n")

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
