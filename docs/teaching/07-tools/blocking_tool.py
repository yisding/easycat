"""Chapter 7 — wrong-version-first warm-up.

The same agent + the same tool as `main.py`, but **without** the
filler heuristic. The tool runs synchronously inside the agent
loop; while it's running (~1.5s for get_weather), the user hears
absolute silence. The technical pipeline works perfectly — the
audio plays cleanly before and after — but the UX is broken in a
way that no production voice bot ships.

Run this first. Notice the awkward silence in the middle of the
turn. Then run `main.py` and notice the filler phrase covering
that same gap.

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
import json
import os
import random
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
SESSION_ID = f"ch07-blocking-tool-{int(time.time())}"


async def get_weather(city: str) -> str:
    await asyncio.sleep(1.5)
    return f"The weather in {city} is {random.choice(['sunny', 'cloudy', 'rainy'])} and 17°C."


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
]

TOOL_IMPLS = {"get_weather": get_weather}


class MiniTurnDetector:
    """Same VAD + pre-roll as chapter 4."""

    def __init__(self, vad, preroll_frames: int = PREROLL_FRAMES) -> None:
        self._vad = vad
        self._preroll: collections.deque[AudioChunk] = collections.deque(maxlen=preroll_frames)
        self._speaking = False

    async def frames(self, audio_iter):
        async for chunk in audio_iter:
            for ev in [e async for e in self._vad.process(chunk)]:
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


async def run_agent_blocking(
    client: AsyncOpenAI,
    user_text: str,
    sentence_queue: asyncio.Queue,
    journal: InMemoryRingBuffer,
) -> None:
    """Tool calls run synchronously. **No filler is played.**

    Compare this body to `main.py`'s `run_agent_streaming`: the
    only difference is the missing `if should_play_filler(name):
    await sentence_queue.put(("filler", FILLER_PHRASES[name]))`.
    That one missing branch is the entire UX gap.
    """
    messages = [
        {"role": "system", "content": "You are a helpful voice assistant. Keep replies brief."},
        {"role": "user", "content": user_text},
    ]

    for _ in range(2):
        stream = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            stream=True,
        )

        buffer = ""
        tool_calls: dict[int, dict] = {}

        async for chunk in stream:
            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                buffer += delta.content
                ready, buffer = split_at_sentence_boundaries(buffer)
                if ready.strip():
                    spoken = strip_markdown(ready).strip()
                    if spoken:
                        await sentence_queue.put(spoken)

            for tc in delta.tool_calls or []:
                entry = tool_calls.setdefault(tc.index, {"id": None, "name": None, "args": ""})
                if tc.id:
                    entry["id"] = tc.id
                if tc.function and tc.function.name:
                    entry["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    entry["args"] += tc.function.arguments

            if choice.finish_reason == "stop":
                if buffer.strip():
                    spoken = strip_markdown(buffer).strip()
                    if spoken:
                        await sentence_queue.put(spoken)
                await sentence_queue.put(None)
                return

            if choice.finish_reason == "tool_calls":
                break

        if not tool_calls:
            await sentence_queue.put(None)
            return

        messages.append(
            {
                "role": "assistant",
                "content": buffer or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["args"]},
                    }
                    for tc in tool_calls.values()
                ],
            }
        )

        for tc in tool_calls.values():
            name = tc["name"]
            args = json.loads(tc["args"] or "{}")

            # NO FILLER. The user hears silence for however long this takes.
            journal.append(
                kind=JournalRecordKind.EVENT,
                name="tool.call.started",
                session_id=SESSION_ID,
                data={"stage": "tool", "name": name, "args": args, "filler_played": False},
            )
            t0 = time.monotonic()
            result = await TOOL_IMPLS[name](**args)
            journal.append(
                kind=JournalRecordKind.EVENT,
                name="tool.call.result",
                session_id=SESSION_ID,
                data={
                    "stage": "tool",
                    "name": name,
                    "elapsed_ms": (time.monotonic() - t0) * 1000,
                    "result": result,
                },
            )
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)})

    await sentence_queue.put(None)


async def drain_to_speaker(
    tts, transport, sentence_queue, journal
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
                            session_id=SESSION_ID,
                            data={
                                "stage": "tts",
                                "kind": "reply",
                                "t_ms": first_audio_t * 1000,
                            },
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
    final_text = ""
    stt_final_t = None
    async for event in stt.events():
        if event.type == STTEventType.FINAL:
            final_text = event.text
            stt_final_t = time.monotonic()

    if not final_text.strip() or stt_final_t is None:
        return

    print(f"  user: {final_text!r}")
    sentence_queue: asyncio.Queue = asyncio.Queue()
    _, delivery = await asyncio.gather(
        run_agent_blocking(client, final_text, sentence_queue, journal),
        drain_to_speaker(tts, transport, sentence_queue, journal),
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

        print('Ask: "What is the weather in Tokyo?"')
        print("Listen for the ~1.5s silence in the middle of the turn — that gap")
        print("is what main.py's filler heuristic is built to mask.\n")

        try:
            await collect_turns(transport, detector, stt_factory, client, tts, journal)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    RUNS_DIR.mkdir(exist_ok=True)
    bundle_path = RUNS_DIR / f"{SESSION_ID}.bundle"
    session_stub = types.SimpleNamespace(journal=journal)
    export_debug_bundle(session_stub, bundle_path, overwrite=True)
    print(f"\nWrote bundle → {bundle_path.relative_to(Path.cwd())}")
    print("Compare the `tool.call.*` records here vs main.py's:")
    print("  - This bundle: `filler_played: False`, no `stage.tts.execute kind=filler`.")
    print("  - main.py:     `filler_played: True`, a `kind=filler` TTS span")
    print("                 between tool.started/result.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
