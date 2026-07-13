from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "docs" / "teaching" / "05-blocking-agent" / "gap_decomposition_probe.py"


def test_gap_decomposition_probe_accounts_for_first_audio_latency() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload == {
        "agent_ms": 1200.0,
        "components_match_total": True,
        "first_audio_precedes_enqueue_end": True,
        "stt_to_agent_ms": 0.0,
        "total_gap_ms": 1650.0,
        "tts_enqueue_ms": 800.0,
        "tts_to_first_audio_ms": 450.0,
    }
