from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "10-cleaning-signal"


def _load_chapter(monkeypatch) -> types.ModuleType:
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=object))
    module_name = "teaching_10_cleaning_signal_main"
    spec = importlib.util.spec_from_file_location(module_name, CHAPTER / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


async def test_aec_reference_only_receives_fully_accepted_audio(monkeypatch) -> None:
    chapter = _load_chapter(monkeypatch)
    rejected = chapter.AudioChunk(data=b"rejected", format=chapter.PCM16_MONO_24K)
    accepted = chapter.AudioChunk(data=b"accepted", format=chapter.PCM16_MONO_24K)

    class FakeTTS:
        async def synthesize(self, _payload):
            for audio in (rejected, accepted):
                yield types.SimpleNamespace(type=chapter.TTSEventType.AUDIO, audio=audio)

    class FakeTransport:
        def __init__(self) -> None:
            self.results = iter((False, True))

        async def send_audio(self, _chunk) -> bool:
            return next(self.results)

    class FakeAEC:
        def __init__(self) -> None:
            self.references = []

        def feed_reference(self, chunk) -> None:
            self.references.append(chunk)

    class FakeJournal:
        def append(self, **_row) -> None: ...

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put("hello world")
    await queue.put(None)
    aec = FakeAEC()

    await chapter.drain_to_speaker(
        FakeTTS(),
        FakeTransport(),
        aec,
        queue,
        types.SimpleNamespace(is_cancelled=False),
        "test-session",
        FakeJournal(),
    )

    assert aec.references == [accepted]
