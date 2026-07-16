"""Keep Chapter 0's raw-vs-resolved TTS format lesson executable."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "00-hello-audio"
PROBE = CHAPTER / "tts_alignment_probe.py"
PROVIDERS = {"cartesia", "deepgram", "elevenlabs", "openai"}


def test_tts_alignment_probe_uses_public_easyconfig_resolution() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert set(payload["raw_defaults"]) == PROVIDERS
    assert {
        provider: rates["transport_output_rate_hz"]
        for provider, rates in payload["raw_defaults"].items()
    } == dict.fromkeys(PROVIDERS, 24_000)

    expected_transport_output = {
        "local": 24_000,
        "webrtc": 16_000,
        "websocket": 16_000,
        "twilio": 8_000,
    }
    assert {
        transport: {
            provider: rates["transport_output_rate_hz"]
            for provider, rates in provider_rows.items()
        }
        for transport, provider_rows in payload["resolved"].items()
    } == {
        transport: dict.fromkeys(PROVIDERS, rate)
        for transport, rate in expected_transport_output.items()
    }
    assert {
        provider: rates["provider_request_rate_hz"]
        for provider, rates in payload["resolved"]["twilio"].items()
    } == {
        "cartesia": 8_000,
        "deepgram": 8_000,
        "elevenlabs": 16_000,
        "openai": 24_000,
    }
    assert payload["controls"] == {
        "twilio_auto_align_disabled": {
            "provider_request_rate_hz": 24_000,
            "transport_output_rate_hz": 24_000,
        },
        "twilio_explicit_16k_preserved": {
            "provider_request_rate_hz": 24_000,
            "transport_output_rate_hz": 16_000,
        },
    }


def test_lesson_distinguishes_config_default_from_resolved_output() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "tts_alignment_probe.py" in readme
    assert "Raw TTS default vs. resolved session output" in readme
    assert "provider request rate" in lesson
    assert "transport-output rate" in lesson
    assert "not proof that a human heard" in lesson
    assert "explicit caller intent wins" in lesson
    assert "auto_align_tts_output_to_transport=False" in lesson
    assert "24 kHz TTS rows" in lesson and "config defaults" in lesson
    assert "OpenAI returns fixed 24 kHz PCM" in lesson
    assert "provider-native 24 kHz" in lesson
    assert "resolved TTS output 16 kHz" in lesson
    assert "WebRTC media 48 kHz" in lesson
    assert "OpenAI TTS 24 kHz ──resample──► WebRTC media 48 kHz" not in lesson
