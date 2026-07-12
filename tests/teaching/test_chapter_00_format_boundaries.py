"""Keep Chapter 0's format table derived from actual runtime defaults."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "00-hello-audio"
PROBE = CHAPTER / "format_boundaries.py"


def test_format_boundary_probe_reports_runtime_defaults_without_io() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    rows = {row["name"]: row for row in json.loads(completed.stdout)}

    assert {name: row["sample_rate_hz"] for name, row in rows.items()} == {
        "cartesia_stt_target": 16_000,
        "cartesia_tts_output": 24_000,
        "deepgram_stt_target": 16_000,
        "deepgram_tts_output": 24_000,
        "elevenlabs_realtime_stt_target": 16_000,
        "elevenlabs_tts_output": 24_000,
        "local_pipeline": 24_000,
        "openai_realtime_stt_input": 24_000,
        "openai_tts_output": 24_000,
        "twilio_pipeline_target": 16_000,
        "twilio_wire": 8_000,
        "webrtc_media_frames": 48_000,
        "webrtc_pipeline_target": 16_000,
        "websocket_pipeline_target": 16_000,
    }
    assert rows["twilio_wire"]["encoding"] == "mulaw"
    assert rows["webrtc_media_frames"]["role"] == "media"
    assert rows["openai_realtime_stt_input"]["role"] == "provider_input"


def test_lesson_names_boundaries_instead_of_brand_wide_rates() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "format_boundaries.py" in readme
    assert "boundaries and defaults" in lesson
    assert "wire, capture, pipeline, provider input, or provider output" in lesson
    assert "cannot recreate spectrum" in lesson
    assert "Most STT providers (Deepgram, OpenAI Realtime, ElevenLabs)" not in lesson
    assert "WebRTC receives and sends 48 kHz media frames" in lesson
    assert "default pipeline target is 16 kHz" in lesson
