"""Keep Chapter 6's streamed TTS delivery evidence executable."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "06-streaming-agent"
STREAMING_SCRIPTS = [
    CHAPTER / "main.py",
    ROOT / "docs" / "teaching" / "07-tools" / "main.py",
    ROOT / "docs" / "teaching" / "07-tools" / "blocking_tool.py",
    ROOT / "docs" / "teaching" / "08-smart-turn" / "main.py",
]


def test_streamed_tts_probe_preserves_sentence_and_turn_counts() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "tts_delivery_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "all_rejected": {
            "accepted_chunks": 0,
            "gap_available": False,
            "outcome": "all_chunks_rejected",
            "rejected_chunks": 2,
            "sentence_counts": [[0, 1], [0, 1]],
            "turn_counts_match": True,
        },
        "mixed_across_sentences": {
            "accepted_chunks": 1,
            "gap_available": True,
            "outcome": "first_audio_accepted",
            "rejected_chunks": 1,
            "sentence_counts": [[0, 1], [1, 0]],
            "turn_counts_match": True,
        },
        "no_audio": {
            "accepted_chunks": 0,
            "gap_available": False,
            "outcome": "no_chunks_produced",
            "rejected_chunks": 0,
            "sentence_counts": [[0, 0]],
            "turn_counts_match": True,
        },
    }


def test_streaming_chapter_copies_preserve_delivery_evidence() -> None:
    stale = []
    for path in STREAMING_SCRIPTS:
        source = path.read_text(encoding="utf-8")
        if (
            '"accepted_chunks": sentence_accepted' not in source
            or '"rejected_chunks": sentence_rejected' not in source
            or '"tts_accepted_chunks": accepted_chunks' not in source
            or '"tts_rejected_chunks": rejected_chunks' not in source
            or "transport rejected all" not in source
            or "TTS produced no audio" not in source
        ):
            stale.append(path.relative_to(ROOT).as_posix())

    assert not stale, "Streaming TTS delivery evidence drifted in: " + ", ".join(stale)


def test_lesson_distinguishes_empty_tts_from_stream_rejection() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "tts_delivery_probe.py" in lesson
    assert "Per-sentence records explain where" in lesson
    assert "all_chunks_rejected" in lesson
    assert "no_chunks_produced" in lesson
    assert "first accepted chunk may arrive in a later sentence" in lesson
