"""Decompose the blocking agent's provider-free first-audio gap.

Run with::

    uv run python docs/teaching/05-blocking-agent/gap_decomposition_probe.py
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace

import main as chapter


class ProbeJournal:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def append(self, **row: object) -> None:
        self.rows.append(row)


class ScriptedSTT:
    async def events(self):
        yield SimpleNamespace(type=chapter.STTEventType.FINAL, text="hello")


class AcceptingTransport:
    async def send_audio(self, _chunk: object) -> bool:
        return True


async def probe() -> dict[str, object]:
    clock = {"now": 0.0}
    journal = ProbeJournal()

    async def scripted_agent(_client: object, _text: str) -> str:
        clock["now"] = 1.2
        return "reply"

    async def scripted_speak(transport: object, _text: str) -> tuple[int, int]:
        clock["now"] = 1.65
        await transport.send_audio(object())
        clock["now"] = 2.0
        return 1, 0

    real_monotonic = chapter.time.monotonic
    real_blocking_agent = chapter.blocking_agent
    real_speak = chapter.speak
    try:
        chapter.time.monotonic = lambda: clock["now"]
        chapter.blocking_agent = scripted_agent
        chapter.speak = scripted_speak
        with redirect_stdout(io.StringIO()):
            await chapter.run_turn(AcceptingTransport(), ScriptedSTT(), None, journal)
    finally:
        chapter.time.monotonic = real_monotonic
        chapter.blocking_agent = real_blocking_agent
        chapter.speak = real_speak

    gap = next(row["data"] for row in journal.rows if row["name"] == "turn.gap")
    stt_to_agent_ms = round(float(gap["stt_to_agent_ms"]), 3)
    agent_ms = round(float(gap["agent_ms"]), 3)
    tts_ms = round(float(gap["tts_ms"]), 3)
    total_gap_ms = round(float(gap["total_gap_ms"]), 3)
    enqueue_ms = round(float(gap["tts_enqueue_ms"]), 3)
    return {
        "stt_to_agent_ms": stt_to_agent_ms,
        "agent_ms": agent_ms,
        "tts_to_first_audio_ms": tts_ms,
        "tts_enqueue_ms": enqueue_ms,
        "total_gap_ms": total_gap_ms,
        "components_match_total": stt_to_agent_ms + agent_ms + tts_ms == total_gap_ms,
        "first_audio_precedes_enqueue_end": tts_ms < enqueue_ms,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
