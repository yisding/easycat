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


def test_endpoint_commit_records_additive_wait_components() -> None:
    source = (CHAPTER / "main.py").read_text(encoding="utf-8")

    assert '"silence_wait_ms": self._silence_wait_ms' in source
    assert '"classification_inference_ms": self._last_inference_ms' in source
    assert '"pending_wait_ms": pending_wait_ms' in source
    assert '"endpoint_wait_ms": (committed_at - estimated_speech_end_t) * 1000' in source


def test_fallback_deadline_tracks_a_changed_classifier_cost() -> None:
    probe = load_probe()

    result = asyncio.run(probe.run_case(mode="smart", probability=0.1, inference_ms=120))

    assert result["classification_inference_ms"] == 120.0
    assert result["pending_wait_ms"] == 800.0
    assert result["endpoint_wait_ms"] == 1120.0
    assert result["components_match_total"] is True


def test_lesson_explains_fallback_timeout_is_not_total_wait() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "endpoint_wait_probe.py" in lesson
    assert "The fallback timeout is not the total wait" in lesson
    assert "200 + 40 + 800" in lesson
    assert "1,040 ms" in lesson
    assert "configured timeout alone" in lesson
