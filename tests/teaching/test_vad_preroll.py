from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "04-vad-preroll"
SMART_CHAPTER = ROOT / "docs" / "teaching" / "08-smart-turn"
TEACHING_DIR = ROOT / "docs" / "teaching"


def _load_chapter():
    spec = importlib.util.spec_from_file_location("teaching_04_vad_preroll", CHAPTER / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_smart_chapter(monkeypatch):
    openai = types.ModuleType("openai")
    openai.AsyncOpenAI = object
    monkeypatch.setitem(sys.modules, "openai", openai)
    spec = importlib.util.spec_from_file_location(
        "teaching_08_smart_turn", SMART_CHAPTER / "main.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeVAD:
    def __init__(self, events_by_frame: list[list[object]]) -> None:
        self._events = iter(events_by_frame)

    async def process(self, _chunk):
        for event in next(self._events):
            yield event


class FakeSmartTurn:
    def __init__(self, probabilities: list[float]) -> None:
        self._probabilities = iter(probabilities)

    async def detect(self, _chunks):
        probability = next(self._probabilities)
        return types.SimpleNamespace(probability=probability, prediction=int(probability >= 0.5))


async def _audio(*chunks):
    for chunk in chunks:
        yield chunk


async def test_no_preroll_still_starts_turn_before_first_live_frame() -> None:
    chapter = _load_chapter()
    vad = FakeVAD(
        [
            [chapter.VADStartSpeaking()],
            [],
            [chapter.VADStopSpeaking()],
        ]
    )
    detector = chapter.MiniTurnDetector(vad, preroll_frames=0)

    events = [event async for event in detector.frames(_audio("first", "middle", "last"))]

    assert events == [
        ("speech_started", None),
        ("frame", "first"),
        ("frame", "middle"),
        ("speech_ended", None),
    ]


async def test_preroll_frames_follow_single_start_event_in_order() -> None:
    chapter = _load_chapter()
    vad = FakeVAD(
        [
            [],
            [chapter.VADStartSpeaking()],
            [chapter.VADStopSpeaking()],
        ]
    )
    detector = chapter.MiniTurnDetector(vad, preroll_frames=2)

    events = [event async for event in detector.frames(_audio("cached", "trigger", "last"))]

    assert events == [
        ("speech_started", None),
        ("frame", "cached"),
        ("frame", "trigger"),
        ("speech_ended", None),
    ]


async def test_smart_turn_pending_frames_stay_on_open_stt_stream(monkeypatch) -> None:
    chapter = _load_smart_chapter(monkeypatch)
    vad = FakeVAD(
        [
            [chapter.VADStartSpeaking()],
            [chapter.VADStopSpeaking()],
            [],
            [chapter.VADStartSpeaking()],
            [chapter.VADStopSpeaking()],
        ]
    )
    detector = chapter.MiniTurnDetector(
        vad,
        smart_turn=FakeSmartTurn([0.1, 0.9]),
        preroll_frames=0,
    )

    events = [
        event
        async for event in detector.frames(
            _audio("first", "pause", "resuming", "continued", "last")
        )
    ]

    assert events[:-1] == [
        ("speech_started", None),
        ("frame", "first"),
        ("frame", "pause"),
        ("frame", "resuming"),
        ("frame", "continued"),
    ]
    assert events[-1][0] == "speech_ended"
    assert isinstance(events[-1][1], float)


def test_copied_turn_detectors_keep_audio_out_of_start_events() -> None:
    detector_sources = [
        path
        for path in sorted(TEACHING_DIR.glob("*/*.py"))
        if "class MiniTurnDetector" in path.read_text(encoding="utf-8")
    ]

    assert detector_sources
    stale = []
    for path in detector_sources:
        source = path.read_text(encoding="utf-8")
        start_payloads = [
            line.strip().removeprefix('yield "speech_started", ')
            for line in source.splitlines()
            if line.strip().startswith('yield "speech_started", ')
        ]
        if not start_payloads or any(payload != "None" for payload in start_payloads):
            stale.append(path.relative_to(ROOT).as_posix())

    assert not stale, "speech_started must be one state-only event in: " + ", ".join(stale)
