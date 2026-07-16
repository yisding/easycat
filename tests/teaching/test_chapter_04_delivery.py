"""Keep Chapter 4's output evidence aligned with Chapter 3."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "04-vad-preroll"


def test_vad_delivery_probe_uses_real_parrot_path() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "delivery_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "delivery": {
            "accepted_chunks": 2,
            "committed_text": "hello from vad",
            "rejected_chunks": 1,
            "stage": "parrot",
        },
        "provider_events": [
            "stt.start",
            "stt.send",
            "stt.end",
            "stt.close",
            "tts.speak:hello from vad",
        ],
        "record_names": ["turn.started", "turn.ended", "parrot.delivery"],
    }


def test_vad_parrot_preserves_speak_acceptance_counts() -> None:
    source = (CHAPTER / "main.py").read_text(encoding="utf-8")

    assert "accepted_chunks, rejected_chunks = await speak" in source
    assert 'name="parrot.delivery"' in source
    assert '"accepted_chunks": accepted_chunks' in source
    assert '"rejected_chunks": rejected_chunks' in source
    assert "Preserve transport acceptance without claiming speaker playback" in source


def test_lesson_keeps_input_and_output_evidence_separate() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "Better input does not prove output" in lesson
    assert "delivery_probe.py" in lesson
    assert "A rejection proves a drop" in lesson
    assert "scheduling, not rendering or audibility" in lesson
    assert "do not relabel accepted chunks as played audio" in lesson
