"""Chapter 4 — VAD + pre-roll.

Replace chapter 3's fixed silence timeout with a real voice-activity
detector plus a pre-roll ring buffer. The same parrot loop, now gated
on VAD turn boundaries instead of "500 ms since the last STT event."

Run with ``--no-preroll`` to hear the start-of-utterance truncation
this chapter was designed to fix.

Dependencies:
    uv sync --extra quickstart --group dev
    export OPENAI_API_KEY=...      # OpenAI TTS
    export DEEPGRAM_API_KEY=...    # Streaming STT
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import time
import types
from pathlib import Path

from easycat import LocalTransportConfig
from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.debug.export import export_debug_bundle
from easycat.events import EventBus, STTEventType, VADStartSpeaking, VADStopSpeaking
from easycat.quick import speak
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
from easycat.stt.factory import STTProviderConfig, create_stt_provider
from easycat.transports.local import LocalTransport
from easycat.vad import VADConfig
from easycat.vad.factory import create_vad

PREROLL_FRAMES = 15  # 15 × 20 ms = 300 ms of audio *before* VAD fires
RUNS_DIR = Path(__file__).parent / "runs"


class MiniTurnDetector:
    """Tiny turn detector: VAD + pre-roll buffer.

    Consumes raw audio chunks, yields tagged events:

        ("speech_started", first_chunk)  - once per turn, at VAD-on.
                                           Emits pre-roll chunks too.
        ("frame",          chunk)         - while VAD says "speech."
        ("speech_ended",   None)          - once per turn, at VAD-off.

    About 40 lines of real logic. EasyCat's production ``TurnManager``
    (``src/easycat/turn_manager.py``) is a 5-state FSM with far more
    responsibilities (bot-speech overlap, cancellation, actions); read
    it once you understand why each extra state is there.
    """

    def __init__(self, vad, preroll_frames: int = PREROLL_FRAMES) -> None:
        self._vad = vad
        self._preroll: collections.deque[AudioChunk] = collections.deque(maxlen=preroll_frames)
        self._speaking = False

    async def frames(self, audio_iter):
        async for chunk in audio_iter:
            vad_events = [ev async for ev in self._vad.process(chunk)]

            for ev in vad_events:
                if isinstance(ev, VADStartSpeaking):
                    # Flush the pre-roll buffer so STT sees the sounds
                    # that arrived *before* the VAD decided to fire.
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


async def parrot(
    transport,
    stt_factory,
    detector: MiniTurnDetector,
    journal: InMemoryRingBuffer,
    session_id: str,
) -> None:
    """On each VAD turn, stream audio into STT, wait for final, speak it."""
    stt = None
    collected_final = ""

    async for tag, chunk in detector.frames(transport.receive_audio()):
        if tag == "speech_started":
            if stt is None:
                stt = stt_factory()
                await stt.start_stream()
                collected_final = ""
                journal.append(
                    kind=JournalRecordKind.EVENT,
                    name="turn.started",
                    session_id=session_id,
                    data={"stage": "turn", "t_ms": time.monotonic() * 1000},
                )
            await stt.send_audio(chunk)

        elif tag == "frame" and stt is not None:
            await stt.send_audio(chunk)

        elif tag == "speech_ended" and stt is not None:
            # Drain the event queue until the sentinel from end_stream().
            # A VADStop before STT saw any speech is harmless — we just
            # close an empty stream and get no FINAL back.
            await stt.end_stream()
            async for event in stt.events():
                if event.type == STTEventType.FINAL:
                    collected_final = event.text
            stt = None

            journal.append(
                kind=JournalRecordKind.EVENT,
                name="turn.ended",
                session_id=session_id,
                data={
                    "stage": "turn",
                    "t_ms": time.monotonic() * 1000,
                    "text": collected_final,
                },
            )

            if collected_final.strip():
                print(f"  → parrot: {collected_final!r}")
                await speak(transport, collected_final)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-preroll",
        action="store_true",
        help="Disable pre-roll; start-of-utterance will be clipped.",
    )
    args = parser.parse_args()

    oai_key = os.getenv("OPENAI_API_KEY")
    dg_key = os.getenv("DEEPGRAM_API_KEY")
    if not oai_key or not dg_key:
        raise SystemExit("Set OPENAI_API_KEY (TTS) and DEEPGRAM_API_KEY (STT).")

    preroll = 0 if args.no_preroll else PREROLL_FRAMES
    session_id = f"ch04-vad-{'nopreroll' if args.no_preroll else 'preroll'}-{int(time.time())}"
    print(f"Pre-roll: {preroll * 20} ms" if preroll else "Pre-roll: OFF")

    journal = InMemoryRingBuffer(capacity=10_000)
    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))
    vad = create_vad(VADConfig())
    detector = MiniTurnDetector(vad, preroll_frames=preroll)

    def stt_factory():
        return create_stt_provider(
            STTProviderConfig(
                provider="deepgram",
                api_key=dg_key,
                params={"sample_rate": 24000, "event_bus": EventBus()},
            )
        )

    await transport.connect()
    print("Speak. The bot parrots back after each VAD turn. Ctrl-C to stop.")

    try:
        await parrot(transport, stt_factory, detector, journal, session_id)
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
