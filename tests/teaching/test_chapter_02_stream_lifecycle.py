"""Keep Chapter 2's task and resource lifetimes inside one safe scope."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "02-transcribe"


def test_stream_lifecycle_probe_covers_acquisition_and_sibling_failures() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "stream_lifecycle_probe.py")],
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
            "error": "stt send failed",
            "events": [
                "transport.connect",
                "stt.start",
                "transport.receive",
                "stt.send",
                "stt.events.start",
                "stt.events.cancelled",
                "stt.end",
                "stt.close",
                "transport.disconnect",
            ],
        },
        "normal": {
            "error": None,
            "events": [
                "transport.connect",
                "stt.start",
                "transport.receive",
                "stt.send",
                "stt.events.start",
                "stt.end",
                "stt.event.final",
                "stt.close",
                "transport.disconnect",
            ],
        },
        "partial_connect_failure": {
            "error": "transport output start failed",
            "events": [
                "transport.connect",
                "transport.input.start",
                "stt.close",
                "transport.input.stop",
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


def test_streaming_source_uses_distinct_task_and_resource_scopes() -> None:
    source = (CHAPTER / "streaming.py").read_text(encoding="utf-8")

    assert "async with AsyncExitStack() as resources" in source
    assert "async with asyncio.TaskGroup() as streams" in source
    assert "resources.push_async_callback(transport.disconnect)" in source
    assert "resources.push_async_callback(close_if_supported, stt)" in source
    assert "resources.push_async_callback(stt.end_stream)" in source
    assert "asyncio.gather(feed_audio(), consume_events())" not in source
