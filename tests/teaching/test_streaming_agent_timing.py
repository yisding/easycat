from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "06-streaming-agent"


def _load_chapter(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=object))
    spec = importlib.util.spec_from_file_location(
        "teaching_06_streaming_agent", CHAPTER / "main.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_first_audio_is_recorded_after_transport_accepts_chunk(monkeypatch) -> None:
    chapter = _load_chapter(monkeypatch)
    clock = {"now": 1.0}
    rows: list[dict] = []
    order: list[str] = []

    class FakeJournal:
        def append(self, **row) -> None:
            rows.append(row)
            if row["name"] == "tts.first_audio":
                order.append("first_audio")

    class FakeTTS:
        async def synthesize(self, _input):
            yield types.SimpleNamespace(type=chapter.TTSEventType.AUDIO, audio=b"rejected")
            yield types.SimpleNamespace(type=chapter.TTSEventType.AUDIO, audio=b"accepted")
            yield types.SimpleNamespace(type=chapter.TTSEventType.AUDIO, audio=b"later")

    class FakeTransport:
        async def send_audio(self, audio) -> bool:
            if audio == b"rejected":
                clock["now"] = 2.0
                order.append("rejected")
                return False
            clock["now"] = 3.0 if audio == b"accepted" else 4.0
            order.append("accepted")
            return True

    sentence_queue: asyncio.Queue[str | None] = asyncio.Queue()
    await sentence_queue.put("hello")
    await sentence_queue.put(None)
    monkeypatch.setattr(chapter.time, "monotonic", lambda: clock["now"])

    await chapter.drain_sentences_to_speaker(
        FakeTTS(), FakeTransport(), sentence_queue, FakeJournal()
    )

    first_audio_rows = [row for row in rows if row["name"] == "tts.first_audio"]
    assert len(first_audio_rows) == 1
    assert first_audio_rows[0]["data"]["t_ms"] == 3_000.0
    assert order == ["rejected", "accepted", "first_audio", "accepted"]


async def test_first_audio_is_not_recorded_when_transport_rejects_every_chunk(
    monkeypatch,
) -> None:
    chapter = _load_chapter(monkeypatch)
    rows: list[dict] = []

    class FakeJournal:
        def append(self, **row) -> None:
            rows.append(row)

    class FakeTTS:
        async def synthesize(self, _input):
            yield types.SimpleNamespace(type=chapter.TTSEventType.AUDIO, audio=b"rejected")

    class FakeTransport:
        async def send_audio(self, _audio) -> bool:
            return False

    sentence_queue: asyncio.Queue[str | None] = asyncio.Queue()
    await sentence_queue.put("hello")
    await sentence_queue.put(None)

    await chapter.drain_sentences_to_speaker(
        FakeTTS(), FakeTransport(), sentence_queue, FakeJournal()
    )

    assert all(row["name"] != "tts.first_audio" for row in rows)
