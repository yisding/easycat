from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "docs" / "teaching" / "13-swap-providers-and-transports" / "matrix_probe.py"


def test_matrix_probe_builds_both_orthogonal_axes_without_credentials() -> None:
    completed = script_runner.run(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["provider_mixes"] == {
        "deepgram-eleven": {"stt": "deepgram/nova-2", "tts": "elevenlabs"},
        "openai": {"stt": "openai", "tts": "openai"},
    }
    assert payload["transports"] == {
        "local": "LocalTransportConfig",
        "twilio": "TwilioTransportConfig",
        "webrtc": "WebRTCTransportConfig",
    }
    assert payload["cell_count"] == 6
    assert {cell["tag"] for cell in payload["cells"]} == {
        "openai-local",
        "openai-webrtc",
        "openai-twilio",
        "deepgram-eleven-local",
        "deepgram-eleven-webrtc",
        "deepgram-eleven-twilio",
    }
    assert payload["provider_axis_reused_across_transports"] is True
    assert payload["transport_axis_reused_across_provider_mixes"] is True
