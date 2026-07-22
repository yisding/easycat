"""Keep Chapter 8's endpoint-wait decomposition executable."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "08-smart-turn"


def load_probe():
    spec = importlib.util.spec_from_file_location(
        "test_chapter_08_endpoint_wait_probe", CHAPTER / "endpoint_wait_probe.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_endpoint_wait_probe_decomposes_three_commit_paths() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "endpoint_wait_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "baseline_vad": {
            "classification_inference_ms": None,
            "components_match_total": True,
            "endpoint_wait_ms": 800.0,
            "pending_wait_ms": 0.0,
            "reason": "vad_timeout",
            "silence_wait_ms": 800,
            "speech_ended": True,
        },
        "smart_accept": {
            "classification_inference_ms": 40.0,
            "components_match_total": True,
            "endpoint_wait_ms": 240.0,
            "pending_wait_ms": 0.0,
            "reason": "smart_turn",
            "silence_wait_ms": 200,
            "speech_ended": True,
        },
        "smart_fallback": {
            "classification_inference_ms": 40.0,
            "components_match_total": True,
            "endpoint_wait_ms": 1040.0,
            "pending_wait_ms": 800.0,
            "reason": "fallback",
            "silence_wait_ms": 200,
            "speech_ended": True,
        },
    }


def test_fallback_deadline_tracks_a_changed_classifier_cost() -> None:
    probe = load_probe()

    result = asyncio.run(probe.run_case(mode="smart", probability=0.1, inference_ms=120))

    assert result["classification_inference_ms"] == 120.0
    assert result["pending_wait_ms"] == 800.0
    assert result["endpoint_wait_ms"] == 1120.0
    assert result["components_match_total"] is True
