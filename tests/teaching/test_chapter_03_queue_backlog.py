"""Keep Chapter 3 honest about STT ingress and blocked consumption."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from easycat.events import STTEvent, STTEventType
from easycat.runtime import InMemoryRingBuffer

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "03-parrot-naive"


def _load_main():
    path = CHAPTER / "main.py"
    spec = importlib.util.spec_from_file_location("teaching_ch03_backlog", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ScriptedSTT:
    async def events(self):
        yield STTEvent(type=STTEventType.PARTIAL, text="The capital is")
        yield STTEvent(type=STTEventType.FINAL, text="The capital is Paris")


@pytest.mark.asyncio
async def test_listener_records_ingress_before_queue_consumption() -> None:
    chapter = _load_main()
    journal = InMemoryRingBuffer(capacity=10)
    queue: asyncio.Queue = asyncio.Queue()
    start = chapter.time.monotonic()

    await chapter.listen_stt(ScriptedSTT(), queue, journal, start)

    first = await queue.get()
    second = await queue.get()
    assert first[0] == 1
    assert first[1].type == STTEventType.PARTIAL
    assert first[1].text == "The capital is"
    assert second[0] == 2
    assert second[1].type == STTEventType.FINAL
    assert second[1].text == "The capital is Paris"
    assert await queue.get() is None

    records = journal.read()
    assert [record.name for record in records] == ["stt.received", "stt.received"]
    assert [record.data["event_id"] for record in records] == [1, 2]
    assert [record.data["queue_depth_before_put"] for record in records] == [0, 1]


@pytest.mark.asyncio
async def test_partial_is_queued_during_speak_then_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chapter = _load_main()
    monkeypatch.setattr(chapter, "SILENCE_TIMEOUT_S", 0.001)
    journal = InMemoryRingBuffer(capacity=20)
    queue: asyncio.Queue = asyncio.Queue()
    start = chapter.time.monotonic()
    speaking = asyncio.Event()
    release_speech = asyncio.Event()

    async def blocked_speak(_transport, _journal, _text, _start) -> None:
        speaking.set()
        await release_speech.wait()

    monkeypatch.setattr(chapter, "speak_and_record", blocked_speak)
    await queue.put(
        (
            1,
            STTEvent(type=STTEventType.PARTIAL, text="The capital is"),
            0.0,
        )
    )
    task = asyncio.create_task(chapter.parrot_events(object(), queue, journal, start))
    await asyncio.wait_for(speaking.wait(), timeout=1)

    paris_received_ms = (chapter.time.monotonic() - start) * 1000
    await queue.put(
        (
            2,
            STTEvent(type=STTEventType.PARTIAL, text="Paris"),
            paris_received_ms,
        )
    )
    await queue.put(None)

    partials_before_release = [record for record in journal.read() if record.name == "stt.partial"]
    assert [record.data["text"] for record in partials_before_release] == ["The capital is"]
    assert queue.qsize() == 2

    release_speech.set()
    await asyncio.wait_for(task, timeout=1)

    partials = [record for record in journal.read() if record.name == "stt.partial"]
    assert [record.data["text"] for record in partials] == ["The capital is", "Paris"]
    assert partials[1].data["event_id"] == 2
    assert partials[1].data["received_offset_ms"] == paris_received_ms
    assert partials[1].data["consumer_lag_ms"] >= 0
    assert partials[1].data["queue_depth_after_get"] == 1
