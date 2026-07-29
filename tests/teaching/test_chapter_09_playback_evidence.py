"""Keep Chapter 9's transport playback evidence accurate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "09-interruption"
PROBE = CHAPTER / "playback_evidence.py"


def test_playback_probe_distinguishes_delivery_callbacks_from_marks() -> None:
    result = script_runner.run(
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
