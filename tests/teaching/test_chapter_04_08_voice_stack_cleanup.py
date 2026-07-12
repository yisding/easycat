"""Keep the early manual voice stack aligned with its ownership lesson."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
TEACHING = ROOT / "docs" / "teaching"
CHAPTER_6 = TEACHING / "06-streaming-agent"
TURN_SCRIPTS = [
    CHAPTER_6 / "main.py",
    TEACHING / "07-tools" / "main.py",
    TEACHING / "07-tools" / "blocking_tool.py",
    TEACHING / "08-smart-turn" / "main.py",
]


def load_script(path: Path):
    module_name = f"teaching_{path.parent.name}_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTransport:
    def receive_audio(self):
        return object()


class FakeDetector:
    def __init__(self, *, pause_after_start: bool) -> None:
        self.pause_after_start = pause_after_start

    async def frames(self, _audio):
        yield "speech_started", None
        if self.pause_after_start:
            await asyncio.Event().wait()
        yield "speech_ended", None


class FakeSTT:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.started = asyncio.Event()

    async def start_stream(self) -> None:
        self.events.append("start")
        self.started.set()

    async def end_stream(self) -> None:
        self.events.append("end")

    async def close(self) -> None:
        self.events.append("close")


def test_voice_stack_cleanup_probe_covers_normal_cancel_and_failure() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER_6 / "voice_stack_cleanup_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    expected_events = [
        "transport.connect",
        "stt.start",
        "stt.end",
        "stt.close",
        "tts.close",
        "client.close",
        "vad.close",
        "transport.disconnect",
    ]
    assert payload["normal_turn"] == {
        "error": None,
        "events": expected_events,
        "outcome": "completed",
    }
    assert payload["cancelled_turn"] == {
        "error": None,
        "events": expected_events,
        "outcome": "cancelled",
    }
    assert payload["cleanup_failure"] == {
        "error": "tts close failed",
        "events": expected_events,
        "outcome": "cleanup_error",
    }


def test_chapters_4_through_8_close_long_lived_resources() -> None:
    scripts = [
        TEACHING / "04-vad-preroll" / "main.py",
        TEACHING / "05-blocking-agent" / "main.py",
        TEACHING / "06-streaming-agent" / "main.py",
        TEACHING / "07-tools" / "main.py",
        TEACHING / "07-tools" / "blocking_tool.py",
        TEACHING / "08-smart-turn" / "main.py",
    ]
    for path in scripts:
        source = path.read_text(encoding="utf-8")
        assert "from contextlib import AsyncExitStack" in source
        assert "resources.push_async_callback(transport.disconnect)" in source
        assert "resources.push_async_callback(close_if_supported, vad)" in source

    for path in scripts[1:]:
        source = path.read_text(encoding="utf-8")
        assert "resources.push_async_callback(close_if_supported, client)" in source

    for path in scripts[2:]:
        source = path.read_text(encoding="utf-8")
        assert "resources.push_async_callback(close_if_supported, tts)" in source


def test_chapters_6_through_8_preserve_per_turn_stt_cleanup() -> None:
    for path in TURN_SCRIPTS:
        source = path.read_text(encoding="utf-8")
        assert "active_stt = stt" in source
        assert "await close_if_supported(active_stt)" in source
        assert "await close_if_supported(stt)" in source


@pytest.mark.parametrize("path", TURN_SCRIPTS, ids=lambda path: path.parent.name + "-" + path.stem)
@pytest.mark.asyncio
async def test_turn_collectors_close_stt_after_normal_turn(path: Path, monkeypatch) -> None:
    module = load_script(path)
    stt = FakeSTT()

    async def fake_run_turn(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(module, "run_turn", fake_run_turn)
    args = [FakeTransport(), FakeDetector(pause_after_start=False), lambda: stt, None, None, None]
    if path.parent.name == "08-smart-turn":
        args.append("session")

    await module.collect_turns(*args)
    assert stt.events == ["start", "end", "close"]


@pytest.mark.parametrize("path", TURN_SCRIPTS, ids=lambda path: path.parent.name + "-" + path.stem)
@pytest.mark.asyncio
async def test_turn_collectors_close_stt_when_cancelled(path: Path) -> None:
    module = load_script(path)
    stt = FakeSTT()
    args = [FakeTransport(), FakeDetector(pause_after_start=True), lambda: stt, None, None, None]
    if path.parent.name == "08-smart-turn":
        args.append("session")

    task = asyncio.create_task(module.collect_turns(*args))
    await stt.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stt.events == ["start", "end", "close"]


def test_chapter_6_teaches_both_ownership_scopes() -> None:
    readme = (CHAPTER_6 / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER_6 / "EXERCISES.md").read_text(encoding="utf-8")

    assert "Two ownership scopes" in readme
    assert "voice_stack_cleanup_probe.py" in readme
    assert "LIFO" in readme
    assert "every registered cleanup" in readme
    assert "voice_stack_cleanup_probe.py" in exercises
    assert "per-turn STT" in exercises
    assert "process-wide" in exercises
