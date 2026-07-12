"""Distinguish Chapter 5's first-audio failure outcomes without providers.

Run with::

    uv run python docs/teaching/05-blocking-agent/tts_outcome_probe.py
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout

import main as chapter

from easycat.events import STTEvent, STTEventType


class ProbeJournal:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, **row) -> None:
        self.rows.append(row)


class ScriptedSTT:
    async def events(self):
        yield STTEvent(type=STTEventType.FINAL, text="hello")


class ScriptedTransport:
    def __init__(self, decisions: list[bool]) -> None:
        self.decisions = iter(decisions)

    async def send_audio(self, _chunk) -> bool:
        return next(self.decisions)


def _outcome(output: str) -> str:
    if "first audio enqueued" in output:
        return "first_audio_accepted"
    if "transport rejected all" in output:
        return "all_chunks_rejected"
    if "TTS produced no audio" in output:
        return "no_chunks_produced"
    if "accepted TTS audio had no timestamp" in output:
        return "acceptance_timestamp_missing"
    raise AssertionError(f"missing turn-gap outcome in output: {output!r}")


async def run_case(decisions: list[bool]) -> dict[str, object]:
    journal = ProbeJournal()

    async def scripted_agent(_client, _text: str) -> str:
        return "reply"

    async def scripted_speak(transport, _text: str) -> tuple[int, int]:
        accepted = rejected = 0
        for _ in decisions:
            if await transport.send_audio(object()):
                accepted += 1
            else:
                rejected += 1
        return accepted, rejected

    chapter.blocking_agent = scripted_agent
    chapter.speak = scripted_speak
    output = io.StringIO()
    with redirect_stdout(output):
        await chapter.run_turn(
            ScriptedTransport(decisions),
            ScriptedSTT(),
            None,
            journal,
        )

    tts = next(row["data"] for row in journal.rows if row["name"] == "stage.tts.execute")
    gap = next(row["data"] for row in journal.rows if row["name"] == "turn.gap")
    return {
        "accepted_chunks": tts["accepted_chunks"],
        "gap_available": gap["total_gap_ms"] is not None,
        "outcome": _outcome(output.getvalue()),
        "rejected_chunks": tts["rejected_chunks"],
        "turn_counts_match": (
            gap["tts_accepted_chunks"] == tts["accepted_chunks"]
            and gap["tts_rejected_chunks"] == tts["rejected_chunks"]
        ),
    }


async def probe() -> dict[str, dict[str, object]]:
    return {
        "all_rejected": await run_case([False, False]),
        "mixed": await run_case([True, False]),
        "no_audio": await run_case([]),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
