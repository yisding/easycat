"""Distinguish streamed TTS delivery outcomes without providers.

Run with::

    uv run python docs/teaching/06-streaming-agent/tts_delivery_probe.py
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import sys
import types
from contextlib import redirect_stdout
from types import SimpleNamespace

from easycat.events import STTEvent, STTEventType

# Exercise the real drain/run-turn logic without requiring the optional SDK
# that the live chapter uses only to construct its client.
sys.modules.setdefault("openai", types.SimpleNamespace(AsyncOpenAI=object))
chapter = importlib.import_module("main")


class ProbeJournal:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, **row) -> None:
        self.rows.append(row)


class ScriptedSTT:
    async def events(self):
        yield STTEvent(type=STTEventType.FINAL, text="hello")


class ScriptedTTS:
    def __init__(self, chunks_per_sentence: list[int]) -> None:
        self._chunks_per_sentence = iter(chunks_per_sentence)

    async def synthesize(self, _payload):
        for _ in range(next(self._chunks_per_sentence)):
            yield SimpleNamespace(type=chapter.TTSEventType.AUDIO, audio=object())


class ScriptedTransport:
    def __init__(self, decisions: list[bool]) -> None:
        self._decisions = iter(decisions)

    async def send_audio(self, _chunk) -> bool:
        return next(self._decisions)


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


async def run_case(chunks_per_sentence: list[int], decisions: list[bool]) -> dict[str, object]:
    journal = ProbeJournal()

    async def scripted_agent(_client, _text: str, queue, _journal) -> None:
        for index in range(len(chunks_per_sentence)):
            await queue.put(f"sentence {index + 1}")
        await queue.put(None)

    chapter.stream_sentences_to_tts = scripted_agent
    output = io.StringIO()
    with redirect_stdout(output):
        await chapter.run_turn(
            ScriptedTransport(decisions),
            ScriptedSTT(),
            None,
            ScriptedTTS(chunks_per_sentence),
            journal,
        )

    tts_rows = [row["data"] for row in journal.rows if row["name"] == "stage.tts.execute"]
    gap = next(row["data"] for row in journal.rows if row["name"] == "turn.gap")
    accepted_chunks = sum(row["accepted_chunks"] for row in tts_rows)
    rejected_chunks = sum(row["rejected_chunks"] for row in tts_rows)
    return {
        "accepted_chunks": accepted_chunks,
        "gap_available": gap["total_gap_ms"] is not None,
        "outcome": _outcome(output.getvalue()),
        "rejected_chunks": rejected_chunks,
        "sentence_counts": [[row["accepted_chunks"], row["rejected_chunks"]] for row in tts_rows],
        "turn_counts_match": (
            gap["tts_accepted_chunks"] == accepted_chunks
            and gap["tts_rejected_chunks"] == rejected_chunks
        ),
    }


async def probe() -> dict[str, dict[str, object]]:
    return {
        "all_rejected": await run_case([1, 1], [False, False]),
        "mixed_across_sentences": await run_case([1, 1], [False, True]),
        "no_audio": await run_case([0], []),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
