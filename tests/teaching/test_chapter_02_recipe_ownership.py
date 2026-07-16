"""Keep Chapter 2's STT recipe ownership lesson executable."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "02-transcribe"


def test_transcribe_ownership_probe_is_provider_free_and_executable() -> None:
    result = subprocess.run(
        [sys.executable, str(CHAPTER / "transcribe_ownership_probe.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "owned_transcript": "provider-free transcript",
        "owned_stream_ended": True,
        "owned_provider_closed": True,
        "caller_transcript": "provider-free transcript",
        "caller_stream_ended": True,
        "caller_provider_closed": False,
    }


def test_chapter_names_logical_stream_and_provider_ownership() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")

    assert "Ending a stream is not the same as closing a provider" in readme
    assert "helper-created STT" in readme
    assert "caller-supplied STT" in readme
    assert "logical stream ends in both cases" in exercises
