"""Keep Chapter 0's raw-vs-resolved TTS format lesson executable."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "00-hello-audio"
PROBE = CHAPTER / "tts_alignment_probe.py"
PROVIDERS = {"cartesia", "deepgram", "elevenlabs", "openai"}


def test_tts_alignment_probe_uses_public_easyconfig_resolution() -> None:
    completed = script_runner.run(
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
