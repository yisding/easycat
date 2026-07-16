"""Keep the first manual STT chapters aligned with provider ownership."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEACHING = ROOT / "docs" / "teaching"
CHAPTER_4 = TEACHING / "04-vad-preroll"
CHAPTER_6 = TEACHING / "06-streaming-agent"


class _FakeSTT:
    def __init__(self) -> None:
        self.started = self.ended = self.closed = 0

    async def start_stream(self) -> None:
        self.started += 1

    async def send_audio(self, _chunk) -> None:
        pass

    async def end_stream(self) -> None:
        self.ended += 1

    async def close(self) -> None:
        self.closed += 1


class _FakeTransport:
    async def receive_audio(self):
        if False:
            yield None


class _FakeDetector:
    def __init__(self, *, cancelled: bool) -> None:
        self.cancelled = cancelled

    async def frames(self, _audio):
        yield "speech_started", None
        yield "frame", object()
        if self.cancelled:
            raise asyncio.CancelledError
        yield "speech_ended", None


def test_chapter_4_closes_stt_on_normal_and_cancelled_turns() -> None:
    result = subprocess.run(
        [sys.executable, str(CHAPTER_4 / "stt_cleanup_probe.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "normal_turn": {"started": 1, "ended": 1, "closed": 1},
        "cancelled_turn": {"started": 1, "ended": 1, "closed": 1},
    }


def test_chapters_2_through_6_close_manually_created_stt() -> None:
    scoped_cleanup_paths = (
        TEACHING / "02-transcribe" / "streaming.py",
        TEACHING / "03-parrot-naive" / "main.py",
    )
    for path in scoped_cleanup_paths:
        source = path.read_text(encoding="utf-8")
        assert "from easycat.runtime.capabilities import close_if_supported" in source
        assert "resources.push_async_callback(close_if_supported, stt)" in source

    direct_cleanup_paths = (
        TEACHING / "04-vad-preroll" / "main.py",
        TEACHING / "05-blocking-agent" / "main.py",
        CHAPTER_6 / "main.py",
    )

    for path in direct_cleanup_paths:
        source = path.read_text(encoding="utf-8")
        assert "from easycat.runtime.capabilities import close_if_supported" in source
        assert "await close_if_supported(" in source


def _load_chapter_6(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=object))
    spec = importlib.util.spec_from_file_location(
        "teaching_06_streaming_agent_cleanup", CHAPTER_6 / "main.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_chapter_6_closes_stt_on_normal_and_cancelled_turns(
    monkeypatch, cancelled: bool
) -> None:
    chapter = _load_chapter_6(monkeypatch)
    stt = _FakeSTT()

    async def fake_run_turn(*_args) -> None:
        pass

    monkeypatch.setattr(chapter, "run_turn", fake_run_turn)
    operation = chapter.collect_turns(
        _FakeTransport(), _FakeDetector(cancelled=cancelled), lambda: stt, None, None, None
    )

    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        await operation

    assert (stt.started, stt.ended, stt.closed) == (1, 1, 1)


def test_chapter_4_names_cleanup_as_distinct_from_stream_end() -> None:
    readme = (CHAPTER_4 / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER_4 / "EXERCISES.md").read_text(encoding="utf-8")

    assert "A VAD turn does not own the provider process" in readme
    assert "normal and cancelled paths" in readme
    assert "ends and closes exactly once" in exercises
