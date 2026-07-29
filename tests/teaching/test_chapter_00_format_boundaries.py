"""Keep Chapter 0's format table derived from actual runtime defaults."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "00-hello-audio"
PROBE = CHAPTER / "format_boundaries.py"


def test_format_boundary_probe_reports_runtime_defaults_without_io() -> None:
    completed = script_runner.run(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    rows = {row["name"]: row for row in json.loads(completed.stdout)}

    assert {name: row["sample_rate_hz"] for name, row in rows.items()} == {
        "cartesia_stt_target": 16_000,
        "cartesia_tts_config_default": 24_000,
        "deepgram_stt_target": 16_000,
        "deepgram_tts_config_default": 24_000,
        "elevenlabs_realtime_stt_target": 16_000,
        "elevenlabs_tts_config_default": 24_000,
        "local_pipeline": 24_000,
        "openai_realtime_stt_input": 24_000,
        "openai_tts_config_default": 24_000,
        "twilio_pipeline_target": 16_000,
        "twilio_wire": 8_000,
        "webrtc_media_frames": 48_000,
        "webrtc_pipeline_target": 16_000,
        "websocket_pipeline_target": 16_000,
    }
    assert rows["twilio_wire"]["encoding"] == "mulaw"
    assert rows["webrtc_media_frames"]["role"] == "media"
    assert rows["openai_realtime_stt_input"]["role"] == "provider_input"
    assert rows["openai_tts_config_default"]["role"] == "provider_config_default"
