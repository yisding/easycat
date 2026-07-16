from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "08-smart-turn"


def _load_chapter(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=object))
    spec = importlib.util.spec_from_file_location("teaching_08_smart_turn", CHAPTER / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeJournal:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, **row) -> None:
        self.rows.append(row)


async def _audio(*chunks):
    for chunk in chunks:
        yield chunk


async def test_vad_baseline_reports_configured_endpoint_wait(monkeypatch) -> None:
    chapter = _load_chapter(monkeypatch)
    clock = {"now": 0.0}
    journal = FakeJournal()

    class FakeVAD:
        def __init__(self) -> None:
            self.calls = 0

        async def process(self, _chunk):
            self.calls += 1
            if self.calls == 1:
                yield chapter.VADStartSpeaking()
            else:
                clock["now"] = 1.8
                yield chapter.VADStopSpeaking()

    monkeypatch.setattr(chapter.time, "monotonic", lambda: clock["now"])
    detector = chapter.MiniTurnDetector(
        FakeVAD(), silence_wait_ms=800, journal=journal, session_id="vad"
    )

    events = [event async for event in detector.frames(_audio("speech", "silence"))]

    assert events[-1][0] == "speech_ended"
    assert events[-1][1] == pytest.approx(1.0)
    endpoint = next(row["data"] for row in journal.rows if row["name"] == "turn.endpoint_commit")
    assert endpoint["mode"] == "vad"
    assert endpoint["reason"] == "vad_timeout"
    assert endpoint["endpoint_wait_ms"] == pytest.approx(800.0)


async def test_smart_turn_endpoint_wait_includes_classifier_time(monkeypatch) -> None:
    chapter = _load_chapter(monkeypatch)
    clock = {"now": 0.0}
    journal = FakeJournal()

    class FakeVAD:
        def __init__(self) -> None:
            self.calls = 0

        async def process(self, _chunk):
            self.calls += 1
            if self.calls == 1:
                yield chapter.VADStartSpeaking()
            else:
                clock["now"] = 1.2
                yield chapter.VADStopSpeaking()

    class FakeSmartTurn:
        async def detect(self, _audio):
            clock["now"] = 1.24
            return types.SimpleNamespace(probability=0.9, prediction="complete")

    monkeypatch.setattr(chapter.time, "monotonic", lambda: clock["now"])
    detector = chapter.MiniTurnDetector(
        FakeVAD(),
        smart_turn=FakeSmartTurn(),
        silence_wait_ms=200,
        journal=journal,
        session_id="smart",
    )

    events = [event async for event in detector.frames(_audio("speech", "silence"))]

    assert events[-1][1] == pytest.approx(1.0)
    endpoint = next(row["data"] for row in journal.rows if row["name"] == "turn.endpoint_commit")
    assert endpoint["mode"] == "smart"
    assert endpoint["reason"] == "smart_turn"
    assert endpoint["endpoint_wait_ms"] == pytest.approx(240.0)


async def test_smart_turn_chapter_uses_runtime_strict_threshold(monkeypatch) -> None:
    chapter = _load_chapter(monkeypatch)
    journal = FakeJournal()

    class FakeSmartTurn:
        async def detect(self, _audio):
            return types.SimpleNamespace(probability=0.5, prediction="complete")

    detector = chapter.MiniTurnDetector(
        object(),
        smart_turn=FakeSmartTurn(),
        threshold=0.5,
        journal=journal,
        session_id="boundary",
    )
    detector._turn_audio.append("frame")

    assert await detector._classify() is False
    classified = next(row["data"] for row in journal.rows if row["name"] == "smart_turn.classify")
    assert classified["confirmed"] is False


async def test_turn_gap_keeps_post_stt_and_endpoint_intervals_separate(
    monkeypatch, capsys
) -> None:
    chapter = _load_chapter(monkeypatch)
    clock = {"now": 1.0}
    journal = FakeJournal()

    class FakeSTT:
        async def events(self):
            clock["now"] = 1.4
            yield types.SimpleNamespace(type=chapter.STTEventType.FINAL, text="hello")

    async def fake_agent(_client, _text: str, _queue) -> None:
        return None

    async def fake_drain(_tts, _transport, _queue, _journal, _session_id) -> float:
        clock["now"] = 2.5
        return 2.0

    monkeypatch.setattr(chapter.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(chapter, "run_agent_streaming", fake_agent)
    monkeypatch.setattr(chapter, "drain_sentences_to_speaker", fake_drain)

    await chapter.run_turn(None, FakeSTT(), None, None, journal, "session", 1.0)

    gap = next(row["data"] for row in journal.rows if row["name"] == "turn.gap")
    assert gap["total_gap_ms"] == pytest.approx(600.0)
    assert gap["estimated_speech_end_to_stt_final_ms"] == pytest.approx(400.0)
    assert gap["estimated_speech_end_to_first_audio_ms"] == pytest.approx(1_000.0)
    assert gap["reply_enqueue_gap_ms"] == pytest.approx(1_100.0)
    assert "estimated user speech end → first audio: 1000 ms" in capsys.readouterr().out


async def test_turn_gap_keeps_nullable_audio_intervals_when_no_audio_is_accepted(
    monkeypatch, capsys
) -> None:
    chapter = _load_chapter(monkeypatch)
    clock = {"now": 1.0}
    journal = FakeJournal()

    class FakeSTT:
        async def events(self):
            clock["now"] = 1.4
            yield types.SimpleNamespace(type=chapter.STTEventType.FINAL, text="hello")

    async def fake_agent(_client, _text: str, _queue) -> None:
        return None

    async def fake_drain(_tts, _transport, _queue, _journal, _session_id) -> None:
        clock["now"] = 2.5
        return None

    monkeypatch.setattr(chapter.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(chapter, "run_agent_streaming", fake_agent)
    monkeypatch.setattr(chapter, "drain_sentences_to_speaker", fake_drain)

    await chapter.run_turn(None, FakeSTT(), None, None, journal, "session", 1.0)

    gap = next(row["data"] for row in journal.rows if row["name"] == "turn.gap")
    assert gap["total_gap_ms"] is None
    assert gap["estimated_speech_end_to_first_audio_ms"] is None
    assert gap["estimated_speech_end_to_stt_final_ms"] == pytest.approx(400.0)
    assert gap["reply_enqueue_gap_ms"] == pytest.approx(1_100.0)
    assert "turn gap unavailable — TTS produced no accepted audio" in capsys.readouterr().out
