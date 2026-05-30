"""Chapter 9c — cancel + estimate what the user actually heard.

Same as ``cancel.py`` plus: we track bytes of TTS audio that made
it onto the speaker before the cancel fires, compute the character
position in the bot's *text* reply that corresponds to those bytes,
and rewrite the assistant turn in the conversation history to end
there. Next turn, the LLM has the same picture of reality the
user does.

The byte-to-char estimate is deliberately simple: bytes heard ÷
total bytes × total chars. The production
`easycat.session.interruption` estimator is a lot more careful
about silence, SSML, markdown, and playback-ack fudge factors —
read it after you understand the toy.

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
SESSION_ID = f"ch09c-estimate-{int(time.time())}"

# OpenAI TTS emits PCM16 mono at 24 kHz = 48,000 bytes/second.
TTS_BYTES_PER_SECOND = 24_000 * 2


@dataclass
class TurnLedger:
    """Per-turn record of what the bot tried to say vs. what played.

    ``sentences_sent`` accumulates the text of each sentence dispatched
    to TTS in order. ``bytes_sent`` tracks audio bytes that actually
    reached ``transport.send_audio``. At cancel time we combine them
    to estimate where, in the concatenated text, the user's ear fell
    silent.
    """

    sentences_sent: list[str] = field(default_factory=list)
    bytes_sent: int = 0

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
        estimated_chars = int(len(full_text) * self.bytes_sent / expected)
        estimated_chars = max(0, min(estimated_chars, len(full_text)))
        return full_text[:estimated_chars]


def _expected_bytes(text: str) -> int:
    """Very rough ~15 chars/s of speech at 48000 bytes/s of TTS audio."""
    chars_per_sec = 15
    seconds = len(text) / chars_per_sec
    return int(seconds * TTS_BYTES_PER_SECOND)


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


async def mic_producer(detector, transport, queue: asyncio.Queue) -> None:
    async for tag, chunk in detector.frames(transport.receive_audio()):
        await queue.put((tag, chunk))


async def run_agent(client, history, sentence_queue, cancel: CancelToken):
    stream = await client.chat.completions.create(model=MODEL, messages=history, stream=True)
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
                await transport.send_audio(event.audio)
                ledger.bytes_sent += len(event.audio.data)
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="stage.tts.execute",
            session_id=SESSION_ID,
            data={
                "stage": "tts",
                "text": sentence,
                "bytes_sent_so_far": ledger.bytes_sent,
                "cancelled": cancel.is_cancelled,
            },
        )


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

    while True:
        tag, chunk = await mic_queue.get()

        # Barge-in during bot speech → cancel AND rewrite history.
        if bot_task is not None and not bot_task.done():
            if tag == "speech_started" and active_cancel is not None and active_ledger is not None:
                journal.append(
                    kind=JournalRecordKind.EVENT,
                    name="interruption.start",
                    session_id=SESSION_ID,
                    data={"stage": "vad", "t_ms": time.monotonic() * 1000},
                )
                active_cancel.cancel()
                await transport.clear_audio()

                # Let the bot task unwind so the ledger is final. A
                # transient agent/TTS error here shouldn't take the
                # whole session down mid-barge-in; log it and move on.
                try:
                    await bot_task
                except Exception as exc:
                    journal.append(
                        kind=JournalRecordKind.EVENT,
                        name="bot_task.error",
                        session_id=SESSION_ID,
                        data={"stage": "coordinator", "error": repr(exc)},
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
                        "bytes_heard": active_ledger.bytes_sent,
                    },
                )
                # Rewrite history: the bot said *only* what the user heard.
                history.append({"role": "assistant", "content": heard})
                print(f"  bot (cut): {heard!r}")
                active_cancel = None
                active_ledger = None
                bot_task = None
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


async def main() -> None:
    if not (os.getenv("OPENAI_API_KEY") and os.getenv("DEEPGRAM_API_KEY")):
        raise SystemExit("Set OPENAI_API_KEY and DEEPGRAM_API_KEY.")

    journal = InMemoryRingBuffer(capacity=10_000)
    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))
    vad = create_vad(VADConfig())
    detector = MiniTurnDetector(vad)
    client = AsyncOpenAI()
    tts = create_tts_provider(
        TTSProviderConfig(provider="openai", api_key=os.environ["OPENAI_API_KEY"])
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
    print("Cancel + history rewrite. Interrupt freely. Ctrl-C to stop.\n")

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
