from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "05-blocking-agent"


def _load_chapter(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=object))
    spec = importlib.util.spec_from_file_location(
        "teaching_05_blocking_agent", CHAPTER / "main.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_turn_gap_ends_at_first_audio_not_full_enqueue(monkeypatch, capsys) -> None:
    chapter = _load_chapter(monkeypatch)
    clock = {"now": 1.0}
    rows: list[dict] = []

    class FakeJournal:
        def append(self, **row) -> None:
            rows.append(row)

    class FakeSTT:
        async def events(self):
            yield types.SimpleNamespace(type=chapter.STTEventType.FINAL, text="hello")

    class FakeTransport:
        async def send_audio(self, _chunk) -> bool:
            return True

    async def fake_agent(_client, _text: str) -> str:
        clock["now"] = 3.0
        return "reply"

    async def fake_speak(transport, _text: str) -> None:
        clock["now"] = 3.5
        await transport.send_audio("first chunk")
        clock["now"] = 4.0

    monkeypatch.setattr(chapter.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(chapter, "blocking_agent", fake_agent)
    monkeypatch.setattr(chapter, "speak", fake_speak)

    await chapter.run_turn(FakeTransport(), FakeSTT(), None, FakeJournal())

    gap = next(row["data"] for row in rows if row["name"] == "turn.gap")
    assert gap["tts_ms"] == 500.0
    assert gap["tts_enqueue_ms"] == 1_000.0
    assert gap["total_gap_ms"] == 2_500.0
    output = capsys.readouterr().out
    assert "STT final → first audio enqueued" in output
    assert "bot done speaking" not in output
