"""Show that enqueueing a tool filler is not proof of delivery.

Run with::

    uv run python docs/teaching/07-tools/filler_delivery_probe.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from types import SimpleNamespace

# Exercise the chapter's real streaming/tool helpers without requiring the
# optional SDK that its live ``main()`` uses only to construct a client.
sys.modules.setdefault("openai", types.SimpleNamespace(AsyncOpenAI=object))
chapter = importlib.import_module("main")


class ProbeJournal:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, **row) -> None:
        self.rows.append(row)


class AsyncChunks:
    def __init__(self, chunks: list[object]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self._chunks:
            yield chunk


def _chunk(*, content=None, tool_calls=None, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls or []),
                finish_reason=finish_reason,
            )
        ]
    )


class ScriptedCompletions:
    def __init__(self, tool_name: str, args: dict[str, object]) -> None:
        call_id = f"call-{tool_name}"
        tool_delta = SimpleNamespace(
            index=0,
            id=call_id,
            function=SimpleNamespace(name=tool_name, arguments=json.dumps(args)),
        )
        self._streams = iter(
            [
                AsyncChunks([_chunk(tool_calls=[tool_delta], finish_reason="tool_calls")]),
                AsyncChunks([_chunk(content="Done.", finish_reason="stop")]),
            ]
        )

    async def create(self, **_kwargs):
        return next(self._streams)


class ScriptedTTS:
    async def synthesize(self, _payload):
        yield SimpleNamespace(type=chapter.TTSEventType.AUDIO, audio=object())


class ScriptedTransport:
    def __init__(self, decisions: list[bool]) -> None:
        self._decisions = iter(decisions)

    async def send_audio(self, _chunk) -> bool:
        return next(self._decisions)


async def run_case(
    tool_name: str, args: dict[str, object], decisions: list[bool]
) -> dict[str, object]:
    async def scripted_tool(**_kwargs) -> str:
        return "ok"

    chapter.TOOL_IMPLS[tool_name] = scripted_tool
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=ScriptedCompletions(tool_name, args))
    )
    journal = ProbeJournal()
    queue: asyncio.Queue[chapter.SentenceItem | None] = asyncio.Queue()

    await chapter.run_agent_streaming(client, "help", queue, journal)
    await chapter.drain_sentences_to_speaker(
        ScriptedTTS(), ScriptedTransport(decisions), queue, journal
    )

    started = next(row["data"] for row in journal.rows if row["name"] == "tool.call.started")
    result = next(row["data"] for row in journal.rows if row["name"] == "tool.call.result")
    tts_rows = [row["data"] for row in journal.rows if row["name"] == "stage.tts.execute"]
    first_audio = next(row["data"] for row in journal.rows if row["name"] == "tts.first_audio")
    filler = next((row for row in tts_rows if row["kind"] == "filler"), None)
    return {
        "filler_enqueued": started["filler_enqueued"],
        "filler_tts": (
            None
            if filler is None
            else {
                "accepted_chunks": filler["accepted_chunks"],
                "rejected_chunks": filler["rejected_chunks"],
                "tool_call_id": filler["tool_call_id"],
            }
        ),
        "first_audio_kind": first_audio["kind"],
        "tool_call_ids_match": started["tool_call_id"] == result["tool_call_id"],
    }


async def probe() -> dict[str, dict[str, object]]:
    return {
        "fast_tool": await run_case("set_timer", {"minutes": 5}, [True]),
        "slow_filler_rejected": await run_case("get_weather", {"city": "Tokyo"}, [False, True]),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
