"""Keep Chapter 3's deliberate timeout bug inside a safe runtime scope."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "03-parrot-naive"


def test_parrot_lifecycle_probe_covers_normal_and_failure_paths() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "parrot_lifecycle_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "connect_failure": {
            "error": "transport connect failed",
            "events": [
                "transport.connect",
                "stt.close",
                "transport.disconnect",
            ],
        },
        "feed_failure": {
            "error": "microphone receive failed",
            "events": [
                "transport.connect",
                "stt.start",
                "transport.receive",
                "stt.events.start",
                "transport.receive.failed",
                "stt.events.cancelled",
                "stt.end",
                "stt.close",
                "transport.disconnect",
            ],
        },
        "normal_event_end": {
            "error": None,
            "events": [
                "transport.connect",
                "stt.start",
                "transport.receive",
                "stt.events.start",
                "stt.events.end",
                "transport.receive.cancelled",
                "stt.end",
                "stt.close",
                "transport.disconnect",
            ],
        },
        "start_failure": {
            "error": "stt start failed",
            "events": [
                "transport.connect",
                "stt.start",
                "stt.close",
                "transport.disconnect",
            ],
        },
    }


def test_parrot_carries_chapter_2_lifetime_scopes_forward() -> None:
    source = (CHAPTER / "main.py").read_text(encoding="utf-8")

    assert "async with AsyncExitStack() as resources" in source
    assert "async with asyncio.TaskGroup() as streams" in source
    assert "resources.push_async_callback(transport.disconnect)" in source
    assert "resources.push_async_callback(close_if_supported, stt)" in source
    assert "resources.push_async_callback(stt.end_stream)" in source
    assert "except* ParrotEventStreamEndedError" in source
    assert "asyncio.gather(" not in source


def test_lesson_identifies_only_timeout_as_deliberately_broken() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "Keep the intended bug isolated" in lesson
    assert "normal_event_end" in lesson
    assert "ParrotEventStreamEndedError" in lesson
    assert "silence-timeout policy" in lesson
    assert "cleanup and cancellation are not" in lesson
    assert "only deliberate failure introduced by this chapter" in lesson
