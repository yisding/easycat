from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "04-vad-preroll"
TEACHING_DIR = ROOT / "docs" / "teaching"


def _load_chapter():
    spec = importlib.util.spec_from_file_location("teaching_04_vad_preroll", CHAPTER / "main.py")
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


def test_provider_free_probe_exposes_exact_preroll_frame_contract() -> None:
    result = subprocess.run(
        [sys.executable, str(CHAPTER / "preroll_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "input_frames": ["cached-1", "cached-2", "trigger", "live", "stop"],
        "with_preroll": [
            {"event": "speech_started", "frame": None},
            {"event": "frame", "frame": "cached-1"},
            {"event": "frame", "frame": "cached-2"},
            {"event": "frame", "frame": "trigger"},
            {"event": "frame", "frame": "live"},
            {"event": "speech_ended", "frame": None},
        ],
        "without_preroll": [
            {"event": "speech_started", "frame": None},
            {"event": "frame", "frame": "trigger"},
            {"event": "frame", "frame": "live"},
            {"event": "speech_ended", "frame": None},
        ],
    }


def test_preroll_lesson_separates_frame_contract_from_live_observations() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")

    assert "preroll_probe.py" in readme
    assert "preroll_probe.py" in exercises
    assert "chop the first ~100 ms" not in exercises
    assert "breaker now survives" not in readme
    assert "Pre-roll does not change the stop decision" in readme
    assert "Transcript and confidence changes are observations" in exercises
