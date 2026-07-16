"""Keep Chapter 7's filler request and delivery evidence distinct."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "07-tools"


def test_filler_delivery_probe_distinguishes_enqueue_from_acceptance() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "filler_delivery_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "fast_tool": {
            "filler_enqueued": False,
            "filler_tts": None,
            "first_audio_kind": "reply",
            "tool_call_ids_match": True,
        },
        "slow_filler_rejected": {
            "filler_enqueued": True,
            "filler_tts": {
                "accepted_chunks": 0,
                "rejected_chunks": 1,
                "tool_call_id": "call-get_weather",
            },
            "first_audio_kind": "reply",
            "tool_call_ids_match": True,
        },
    }


def test_tool_and_filler_records_share_attribution_fields() -> None:
    main = (CHAPTER / "main.py").read_text(encoding="utf-8")
    blocking = (CHAPTER / "blocking_tool.py").read_text(encoding="utf-8")

    assert '"filler_enqueued": filler_enqueued' in main
    assert '"filler_enqueued": False' in blocking
    assert '"tool_call_id": tc["id"]' in main
    assert '"tool_call_id": tool_call_id' in main
    assert "filler_played" not in main + blocking


def test_lesson_names_the_enqueue_delivery_boundary() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "filler_delivery_probe.py" in lesson
    assert "Enqueued is not delivered" in lesson
    assert "filler_enqueued" in lesson
    assert "scheduled for delivery" in lesson
    assert "first accepted reply chunk" in lesson
    assert "five action dataclasses" not in lesson
    assert '("reply" | "filler", text)' not in lesson
