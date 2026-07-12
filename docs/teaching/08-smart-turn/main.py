"""Chapter 8 — Smart-turn.

Replace the "wait 800 ms of silence to be sure they're done" rule with
an ONNX endpoint classifier. When the model is confident the user is
done, we commit the turn immediately.

Two modes:

    --backend vad           # baseline: long silence timeout, no model
    --backend smart         # short timeout + smart-turn confirmation

Run with each and compare the bundle timings.

Dependencies:
    uv sync --extra quickstart --extra deepgram --group dev     # includes smart-turn
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
from easycat.runtime.capabilities import close_if_supported
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

    ``speech_ended`` carries the estimated monotonic time of the
    user's last speech frame so downstream timing includes endpoint
    detection instead of starting only after STT finalises.
    """

    def __init__(
        self,
        vad,
        *,
        smart_turn=None,
        threshold: float = SMART_THRESHOLD,
        fallback_ms: int = SMART_FALLBACK_MS,
        silence_wait_ms: int = VAD_BASELINE_SILENCE_MS,
        journal: InMemoryRingBuffer | None = None,
        session_id: str = "",
        preroll_frames: int = PREROLL_FRAMES,
    ) -> None:
        self._vad = vad
        self._smart = smart_turn
        self._threshold = threshold
        self._fallback_ms = fallback_ms
        self._silence_wait_ms = silence_wait_ms
        self._journal = journal
        self._session_id = session_id
        self._preroll: collections.deque[AudioChunk] = collections.deque(maxlen=preroll_frames)
        self._state: str = "idle"  # idle | speaking | pending
        self._pending_since: float | None = None
        self._candidate_speech_end_t: float | None = None
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
                        self._candidate_speech_end_t = None
                    else:
                        self._candidate_speech_end_t = None
                        yield "speech_started", None
                        while self._preroll:
                            buf = self._preroll.popleft()
                            self._turn_audio.append(buf)
                            yield "frame", buf
                        self._state = "speaking"
                elif isinstance(ev, VADStopSpeaking) and self._state == "speaking":
                    detected_at = time.monotonic()
                    # VADStop arrives only after the configured silence
                    # window. Subtract that known wait to estimate when the
                    # user's last speech frame ended.
                    self._candidate_speech_end_t = detected_at - self._silence_wait_ms / 1000
                    confirmed = await self._classify()
                    if self._smart is None or confirmed:
                        reason = "vad_timeout" if self._smart is None else "smart_turn"
                        speech_end_t = self._commit_endpoint(reason)
                        self._state = "idle"
                        self._turn_audio = []
                        yield "speech_ended", speech_end_t
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
                speech_end_t = self._commit_endpoint("fallback")
                self._state = "idle"
                self._pending_since = None
                self._turn_audio = []
                yield "speech_ended", speech_end_t

            if self._state == "speaking":
                self._turn_audio.append(chunk)
                yield "frame", chunk
            elif self._state == "pending":
                self._turn_audio.append(chunk)
            else:
                self._preroll.append(chunk)

    def _commit_endpoint(self, reason: str) -> float:
        committed_at = time.monotonic()
        estimated_speech_end_t = self._candidate_speech_end_t
        if estimated_speech_end_t is None:
            estimated_speech_end_t = committed_at - self._silence_wait_ms / 1000
        self._candidate_speech_end_t = None

        if self._journal is not None:
            self._journal.append(
                kind=JournalRecordKind.EVENT,
                name="turn.endpoint_commit",
                session_id=self._session_id,
                data={
                    "stage": "turn",
                    "mode": "smart" if self._smart is not None else "vad",
                    "reason": reason,
                    "silence_wait_ms": self._silence_wait_ms,
                    "estimated_speech_end_ms": estimated_speech_end_t * 1000,
                    "committed_at_ms": committed_at * 1000,
                    "endpoint_wait_ms": (committed_at - estimated_speech_end_t) * 1000,
                },
            )
        return estimated_speech_end_t

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


async def drain_sentences_to_speaker(
    tts, transport, sentence_queue, journal, session_id
) -> tuple[float | None, int, int]:
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
                            session_id=session_id,
                            data={"stage": "tts", "t_ms": first_audio_t * 1000},
                        )
                else:
                    rejected_chunks += 1
                    sentence_rejected += 1
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="stage.tts.execute",
            session_id=session_id,
            data={
                "stage": "tts",
                "elapsed_ms": (time.monotonic() - synth_start) * 1000,
                "accepted_chunks": sentence_accepted,
                "rejected_chunks": sentence_rejected,
                "text": sentence,
            },
        )
    return first_audio_t, accepted_chunks, rejected_chunks


async def run_turn(transport, stt, client, tts, journal, session_id, estimated_speech_end_t=None):
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
    _, delivery = await asyncio.gather(
        run_agent_streaming(client, final_text, q),
        drain_sentences_to_speaker(tts, transport, q, journal, session_id),
    )
    first_audio_t, accepted_chunks, rejected_chunks = delivery
    reply_enqueue_gap = (time.monotonic() - stt_final_t) * 1000
    total_gap = None if first_audio_t is None else (first_audio_t - stt_final_t) * 1000
    speech_end_to_first_audio = (
        None
        if first_audio_t is None or estimated_speech_end_t is None
        else (first_audio_t - estimated_speech_end_t) * 1000
    )
    endpoint_to_stt_final = (
        None if estimated_speech_end_t is None else (stt_final_t - estimated_speech_end_t) * 1000
    )
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="turn.gap",
        session_id=session_id,
        data={
            "stage": "turn",
            "total_gap_ms": total_gap,
            "estimated_speech_end_to_first_audio_ms": speech_end_to_first_audio,
            "endpoint_to_stt_final_ms": endpoint_to_stt_final,
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
        if speech_end_to_first_audio is not None:
            print(
                f"  (estimated user speech end → first audio: {speech_end_to_first_audio:.0f} ms)"
            )


async def collect_turns(
    transport, detector, stt_factory, client, tts, journal, session_id
) -> None:
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
                    await run_turn(
                        transport,
                        active_stt,
                        client,
                        tts,
                        journal,
                        session_id,
                        estimated_speech_end_t=chunk,
                    )
                finally:
                    await close_if_supported(active_stt)
    finally:
        if stt is not None:
            try:
                await stt.end_stream()
            finally:
                await close_if_supported(stt)


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

        vad = create_vad(VADConfig(min_silence_duration_ms=silence_ms))
        resources.push_async_callback(close_if_supported, vad)
        smart_turn = None
        if args.backend == "smart":
            smart_turn = create_smart_turn(
                SmartTurnConfig(enabled=True, threshold=SMART_THRESHOLD)
            )
        detector = MiniTurnDetector(
            vad,
            smart_turn=smart_turn,
            threshold=SMART_THRESHOLD,
            silence_wait_ms=silence_ms,
            journal=journal,
            session_id=session_id,
        )

        client = AsyncOpenAI()
        resources.push_async_callback(close_if_supported, client)
        tts = create_tts_provider(
            TTSProviderConfig(provider="openai", api_key=os.environ["OPENAI_API_KEY"])
        )
        resources.push_async_callback(close_if_supported, tts)

        print("Talk. Ctrl-C to stop.\n")

        try:
            await collect_turns(transport, detector, stt_factory, client, tts, journal, session_id)
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
