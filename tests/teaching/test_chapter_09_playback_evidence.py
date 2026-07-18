"""Keep Chapter 9's transport playback evidence accurate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests._markdown_asserts import assert_prose_not_in

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "09-interruption"
PROBE = CHAPTER / "playback_evidence.py"


def test_playback_probe_distinguishes_delivery_callbacks_from_marks() -> None:
    result = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "human_ear_ground_truth": False,
        "local": {
            "progress_evidence": ["TransportAudioDelivered"],
            "reports_audio_delivery": True,
            "supports_playback_marks": False,
            "transport_class": "LocalTransport",
        },
        "twilio": {
            "progress_evidence": ["PlaybackMarkAck"],
            "reports_audio_delivery": False,
            "supports_playback_marks": True,
            "transport_class": "TwilioTransport",
        },
    }


def test_interruption_lesson_does_not_call_local_delivery_a_playback_mark() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    normalized_exercises = " ".join(exercises.split())

    assert "playback_evidence.py" in readme
    assert "playback_evidence.py" in exercises
    assert_prose_not_in("playback-ack\n   marks (from `LocalTransport`)", exercises)
    assert "`TransportAudioDelivered`" in readme
    assert "`PlaybackMarkAck`" in readme
    assert "none proves sound reached a human ear" in readme
    assert "human reaction is not an exact clock" in normalized_exercises
    assert "Interrupt exactly after one word" not in readme
    assert "repeat several times" in readme
