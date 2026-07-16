from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "06-streaming-agent"
TURN_GAP_SCRIPTS = [
    CHAPTER / "main.py",
    ROOT / "docs" / "teaching" / "07-tools" / "main.py",
    ROOT / "docs" / "teaching" / "07-tools" / "blocking_tool.py",
    ROOT / "docs" / "teaching" / "08-smart-turn" / "main.py",
]


def _load_chapter(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=object))
    spec = importlib.util.spec_from_file_location(
        "teaching_06_streaming_agent", CHAPTER / "main.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_streaming_turn_gap_ends_at_first_accepted_audio(monkeypatch, capsys) -> None:
    chapter = _load_chapter(monkeypatch)
    clock = {"now": 1.0}
    rows: list[dict] = []

    class FakeJournal:
        def append(self, **row) -> None:
            rows.append(row)

    class FakeSTT:
        async def events(self):
            yield types.SimpleNamespace(type=chapter.STTEventType.FINAL, text="hello")

    class FakeTTS:
        async def synthesize(self, _payload):
            clock["now"] = 3.0
            yield types.SimpleNamespace(type=chapter.TTSEventType.AUDIO, audio="chunk")
            clock["now"] = 4.0

    class FakeTransport:
        async def send_audio(self, _chunk) -> bool:
            clock["now"] = 3.2
            return True

    async def fake_agent(_client, _text: str, queue, _journal) -> None:
        await queue.put("sentence")
        await queue.put(None)

    monkeypatch.setattr(chapter.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(chapter, "stream_sentences_to_tts", fake_agent)

    await chapter.run_turn(FakeTransport(), FakeSTT(), None, FakeTTS(), FakeJournal())

    first_audio = next(row["data"] for row in rows if row["name"] == "tts.first_audio")
    gap = next(row["data"] for row in rows if row["name"] == "turn.gap")
    assert first_audio["t_ms"] == 3_200.0
    assert gap["total_gap_ms"] == 2_200.0
    assert gap["reply_enqueue_gap_ms"] == 3_000.0
    output = capsys.readouterr().out
    assert "STT final → first audio accepted" in output
    assert "bot done speaking" not in output


async def test_streaming_turn_gap_is_unavailable_without_accepted_audio(
    monkeypatch, capsys
) -> None:
    chapter = _load_chapter(monkeypatch)
    clock = {"now": 1.0}
    rows: list[dict] = []

    class FakeJournal:
        def append(self, **row) -> None:
            rows.append(row)

    class FakeSTT:
        async def events(self):
            yield types.SimpleNamespace(type=chapter.STTEventType.FINAL, text="hello")

    class FakeTTS:
        async def synthesize(self, _payload):
            clock["now"] = 3.0
            yield types.SimpleNamespace(type=chapter.TTSEventType.AUDIO, audio="chunk")
            clock["now"] = 4.0

    class FakeTransport:
        async def send_audio(self, _chunk) -> bool:
            clock["now"] = 3.2
            return False

    async def fake_agent(_client, _text: str, queue, _journal) -> None:
        await queue.put("sentence")
        await queue.put(None)

    monkeypatch.setattr(chapter.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(chapter, "stream_sentences_to_tts", fake_agent)

    await chapter.run_turn(FakeTransport(), FakeSTT(), None, FakeTTS(), FakeJournal())

    assert all(row["name"] != "tts.first_audio" for row in rows)
    gap = next(row["data"] for row in rows if row["name"] == "turn.gap")
    assert gap["total_gap_ms"] is None
    assert gap["reply_enqueue_gap_ms"] == 3_000.0
    assert "turn gap unavailable — TTS produced no accepted audio" in capsys.readouterr().out


def test_streaming_chapter_copies_keep_first_audio_turn_gap_contract() -> None:
    stale = []
    for path in TURN_GAP_SCRIPTS:
        source = path.read_text(encoding="utf-8")
        if (
            '"reply_enqueue_gap_ms"' not in source
            or "first_audio_t" not in source
            or "total_gap = (time.monotonic() - stt_final_t)" in source
            or "bot done speaking" in source
        ):
            stale.append(path.relative_to(ROOT).as_posix())

    assert not stale, "Streaming turn-gap copies drifted in: " + ", ".join(stale)
