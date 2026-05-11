"""Chapter 8 — Smart-turn.

Replace the "wait 800 ms of silence to be sure they're done" rule with
an ONNX endpoint classifier. When the model is confident the user is
done, we commit the turn immediately.

Two modes:

    --backend vad           # baseline: long silence timeout, no model
    --backend smart         # short timeout + smart-turn confirmation

Run with each and compare the bundle timings.

Dependencies:
    uv sync --extra quickstart --group dev     # includes smart-turn
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

from easycat import LocalTransportConfig, SmartTurnConfig
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
from easycat.smart_turn import create_smart_turn
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

# Baseline: VAD waits a long silence before calling the turn over.
# Smart: VAD fires early (short silence), then smart-turn *gates* the
# commit. If the model says "not done," we stay in the turn and let
# a hard fallback timeout catch actual long silences.
VAD_BASELINE_SILENCE_MS = 800
SMART_EARLY_SILENCE_MS = 200
SMART_FALLBACK_MS = 800  # if smart-turn keeps saying "not done"
SMART_THRESHOLD = 0.5

# ── MiniTurnDetector with optional smart-turn ─────────────────────


class MiniTurnDetector:
    """VAD + optional smart-turn gating.

    Without smart-turn, every ``VADStopSpeaking`` becomes
    ``speech_ended``. With smart-turn we gate the commit:

    - VAD fires after a *short* silence.
    - Classifier looks at the turn so far. Above threshold → commit
      now. Below → enter a ``pending`` state where a new
      ``VADStartSpeaking`` resumes the turn (the user was just
      thinking). A hard ``fallback_ms`` fallback commits if neither
      happens.
    """

    def __init__(
        self,
        vad,
        *,
        smart_turn=None,
        threshold: float = SMART_THRESHOLD,
        fallback_ms: int = SMART_FALLBACK_MS,
        journal: InMemoryRingBuffer | None = None,
        session_id: str = "",
        preroll_frames: int = PREROLL_FRAMES,
    ) -> None:
        self._vad = vad
        self._smart = smart_turn
        self._threshold = threshold
        self._fallback_ms = fallback_ms
        self._journal = journal
        self._session_id = session_id
        self._preroll: collections.deque[AudioChunk] = collections.deque(maxlen=preroll_frames)
        self._state: str = "idle"  # idle | speaking | pending
        self._pending_since: float | None = None
        self._turn_audio: list[AudioChunk] = []

    async def frames(self, audio_iter):
        async for chunk in audio_iter:
            vad_events = [ev async for ev in self._vad.process(chunk)]

            for ev in vad_events:
                if isinstance(ev, VADStartSpeaking):
                    if self._state == "pending":
                        # The user was just thinking — resume without a
                        # new speech_started boundary.
                        self._state = "speaking"
                        self._pending_since = None
                    else:
                        while self._preroll:
                            buf = self._preroll.popleft()
                            self._turn_audio.append(buf)
                            yield "speech_started", buf
                        self._state = "speaking"
                elif isinstance(ev, VADStopSpeaking) and self._state == "speaking":
                    confirmed = await self._classify()
                    if self._smart is None or confirmed:
                        self._state = "idle"
                        self._turn_audio = []
                        yield "speech_ended", None
                    else:
                        self._state = "pending"
                        self._pending_since = time.monotonic()

            # Fallback commit — smart-turn kept saying "not done" but no
            # new speech arrived. Force the turn over.
            if (
                self._state == "pending"
                and self._pending_since is not None
                and (time.monotonic() - self._pending_since) * 1000 >= self._fallback_ms
            ):
                self._state = "idle"
                self._pending_since = None
                self._turn_audio = []
                yield "speech_ended", None

            if self._state == "speaking":
                self._turn_audio.append(chunk)
                yield "frame", chunk
            elif self._state == "pending":
                self._turn_audio.append(chunk)
            else:
                self._preroll.append(chunk)

    async def _classify(self) -> bool:
        """Return True if smart-turn confirms the turn is over."""
        if self._smart is None or not self._turn_audio:
            return True
        t0 = time.monotonic()
        result = await self._smart.detect(self._turn_audio)
        inference_ms = (time.monotonic() - t0) * 1000
        confirmed = result.probability >= self._threshold
        if self._journal is not None:
            self._journal.append(
                kind=JournalRecordKind.EVENT,
                name="smart_turn.classify",
                session_id=self._session_id,
                data={
                    "stage": "turn",
                    "probability": result.probability,
                    "prediction": result.prediction,
                    "confirmed": confirmed,
                    "inference_ms": inference_ms,
                },
            )
        return confirmed


# ── Streaming agent + TTS (same shape as chapter 6) ───────────────


async def run_agent_streaming(client, user_text, sentence_queue):
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


async def drain_sentences_to_speaker(tts, transport, sentence_queue, journal, session_id):
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
            session_id=session_id,
            data={
                "stage": "tts",
                "elapsed_ms": (time.monotonic() - synth_start) * 1000,
                "text": sentence,
            },
        )


async def run_turn(transport, stt, client, tts, journal, session_id):
    final_text = ""
    stt_final_t = None
    async for event in stt.events():
        if event.type == STTEventType.FINAL:
            final_text = event.text
            stt_final_t = time.monotonic()
    if not final_text.strip() or stt_final_t is None:
        return

    print(f"  user: {final_text!r}")
    q: asyncio.Queue = asyncio.Queue()
    await asyncio.gather(
        run_agent_streaming(client, final_text, q),
        drain_sentences_to_speaker(tts, transport, q, journal, session_id),
    )
    total_gap = (time.monotonic() - stt_final_t) * 1000
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="turn.gap",
        session_id=session_id,
        data={"stage": "turn", "total_gap_ms": total_gap, "text": final_text},
    )
    print(f"  (turn gap: {total_gap:.0f} ms)")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--backend",
        choices=("vad", "smart"),
        default="smart",
        help="vad: long silence timeout. smart: short timeout + smart-turn confirmation.",
    )
    args = ap.parse_args()

    if not (os.getenv("OPENAI_API_KEY") and os.getenv("DEEPGRAM_API_KEY")):
        raise SystemExit("Set OPENAI_API_KEY and DEEPGRAM_API_KEY.")

    session_id = f"ch08-{args.backend}-{int(time.time())}"
    silence_ms = SMART_EARLY_SILENCE_MS if args.backend == "smart" else VAD_BASELINE_SILENCE_MS
    print(
        f"Backend: {args.backend}  "
        f"VAD min_silence_duration={silence_ms} ms  "
        f"smart-turn={'on' if args.backend == 'smart' else 'off'}"
    )

    journal = InMemoryRingBuffer(capacity=10_000)
    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))
    vad = create_vad(VADConfig(min_silence_duration_ms=silence_ms))
    smart_turn = None
    if args.backend == "smart":
        smart_turn = create_smart_turn(SmartTurnConfig(enabled=True, threshold=SMART_THRESHOLD))
    detector = MiniTurnDetector(
        vad,
        smart_turn=smart_turn,
        threshold=SMART_THRESHOLD,
        journal=journal,
        session_id=session_id,
    )
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

    async def collect_turns():
        stt = None
        async for tag, chunk in detector.frames(transport.receive_audio()):
            if tag == "speech_started":
                if stt is None:
                    stt = stt_factory()
                    await stt.start_stream()
                await stt.send_audio(chunk)
            elif tag == "frame" and stt is not None:
                await stt.send_audio(chunk)
            elif tag == "speech_ended" and stt is not None:
                await stt.end_stream()
                await run_turn(transport, stt, client, tts, journal, session_id)
                stt = None

    try:
        await collect_turns()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await transport.disconnect()

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
