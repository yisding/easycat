"""Chapter 5 — The blocking agent.

Same pipeline as chapter 4, but instead of parroting the transcript
back, we send it to an LLM and wait for the complete response before
handing it to TTS. The bot falls silent for 2-4 seconds per turn.
That silence is the whole point of this chapter.

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
from pathlib import Path

from openai import AsyncOpenAI

from easycat import (
    LocalTransport,
    LocalTransportConfig,
    create_stt_provider,
    create_vad,
    speak,
)
from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.debug.export import export_debug_bundle
from easycat.events import (
    EventBus,
    STTEventType,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
from easycat.stt.factory import STTProviderConfig
from easycat.vad import VADConfig

PREROLL_FRAMES = 15
MODEL = "gpt-4o-mini"
RUNS_DIR = Path(__file__).parent / "runs"
SESSION_ID = f"ch05-blocking-{int(time.time())}"


class MiniTurnDetector:
    """Same as chapter 4."""

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


def span(journal: InMemoryRingBuffer, name: str, t0: float, **extra) -> None:
    """Record a closed span with start→end wall time in ms."""
    elapsed_ms = (time.monotonic() - t0) * 1000
    journal.append(
        kind=JournalRecordKind.EVENT,
        name=name,
        session_id=SESSION_ID,
        data={"stage": name.split(".")[1], "elapsed_ms": elapsed_ms, **extra},
    )


async def blocking_agent(client: AsyncOpenAI, user_text: str) -> str:
    """One LLM call. Wait for the full response. Return the string."""
    resp = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "You are a helpful voice assistant. Keep it brief."},
            {"role": "user", "content": user_text},
        ],
    )
    return resp.choices[0].message.content or ""


async def run_turn(transport, stt, client, journal) -> None:
    """Finalize the current STT stream, run the LLM, speak the reply.

    The STT stream has been receiving chunks from the parent caller's
    VAD loop already — we just close it here and drain the FINAL.
    """
    final_text = ""
    stt_final_t = None
    async for event in stt.events():
        if event.type == STTEventType.FINAL:
            final_text = event.text
            stt_final_t = time.monotonic()

    if not final_text.strip() or stt_final_t is None:
        return

    print(f"  user: {final_text!r}")

    # Sub-gap 1: STT final → we start the LLM call. Just our own
    # dispatch overhead; should be under a millisecond.
    agent_dispatch = time.monotonic()
    span(
        journal,
        "stage.stt_to_agent",
        stt_final_t,
        at_ms=(agent_dispatch - stt_final_t) * 1000,
    )

    # Sub-gap 2: the LLM call itself. The biggest sub-gap — usually
    # 1-3 seconds of silence on a small model, more on a large one.
    agent_start = time.monotonic()
    reply = await blocking_agent(client, final_text)
    agent_end = time.monotonic()
    span(
        journal,
        "stage.agent.execute",
        agent_start,
        prompt=final_text,
        reply=reply,
    )

    # Sub-gap 3: agent response → first TTS audio reaches the speaker.
    # The OpenAI TTS provider streams PCM back, so ``speak`` blocks
    # until the whole file is enqueued on the transport. For teaching
    # purposes we report the full synth-and-enqueue duration.
    tts_start = time.monotonic()
    print(f"  bot:  {reply!r}")
    await speak(transport, reply)
    span(journal, "stage.tts.execute", tts_start, text=reply)

    total_gap = (time.monotonic() - stt_final_t) * 1000
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="turn.gap",
        session_id=SESSION_ID,
        data={
            "stage": "turn",
            "total_gap_ms": total_gap,
            "stt_to_agent_ms": (agent_dispatch - stt_final_t) * 1000,
            "agent_ms": (agent_end - agent_start) * 1000,
            "tts_ms": (time.monotonic() - tts_start) * 1000,
            "text": reply,
        },
    )
    print(f"  (turn gap: {total_gap:.0f} ms — STT final → bot done speaking)")


async def main() -> None:
    if not (os.getenv("OPENAI_API_KEY") and os.getenv("DEEPGRAM_API_KEY")):
        raise SystemExit("Set OPENAI_API_KEY and DEEPGRAM_API_KEY.")

    journal = InMemoryRingBuffer(capacity=10_000)
    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))
    vad = create_vad(VADConfig())
    detector = MiniTurnDetector(vad)
    client = AsyncOpenAI()

    def stt_factory():
        return create_stt_provider(
            STTProviderConfig(
                provider="deepgram",
                api_key=os.environ["DEEPGRAM_API_KEY"],
                params={"sample_rate": 24000, "event_bus": EventBus()},
            )
        )

    await transport.connect()
    print("Talk. Each turn will feel slow. That is the lesson.\n")

    async def collect_turns():
        """Same shape as chapter 4: stream live into STT per turn."""
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
                await run_turn(transport, stt, client, journal)
                stt = None

    try:
        await collect_turns()
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
