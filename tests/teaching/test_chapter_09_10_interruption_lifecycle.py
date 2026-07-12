"""Keep interruption turn continuity and ownership executable."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
TEACHING = ROOT / "docs" / "teaching"
CHAPTER_9 = TEACHING / "09-interruption"
CANCEL_SCRIPTS = [
    CHAPTER_9 / "cancel.py",
    CHAPTER_9 / "estimate.py",
    TEACHING / "10-cleaning-signal" / "main.py",
    TEACHING / "10-cleaning-signal" / "wrong_order.py",
]
ALL_SCRIPTS = [CHAPTER_9 / "ignore.py", *CANCEL_SCRIPTS]


def load_script(path: Path):
    module_name = f"teaching_lifecycle_{path.parent.name}_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeJournal:
    def __init__(self) -> None:
        self.names: list[str] = []

    def append(self, *, name: str, **_kwargs) -> None:
        self.names.append(name)


class FakeTransport:
    def __init__(self) -> None:
        self.clear_calls = 0

    async def clear_audio(self) -> None:
        self.clear_calls += 1


class FakeCancel:
    def __init__(self) -> None:
        self._cancelled = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    async def wait(self) -> None:
        await self._cancelled.wait()


class FakeSTT:
    def __init__(self, final_text: str, final_type) -> None:
        self.final_text = final_text
        self.final_type = final_type
        self.calls: list[str] = []
        self.started = asyncio.Event()

    async def start_stream(self) -> None:
        self.calls.append("start")
        self.started.set()

    async def send_audio(self, chunk) -> None:
        self.calls.append(f"audio:{chunk}")

    async def end_stream(self) -> None:
        self.calls.append("end")

    async def events(self):
        yield types.SimpleNamespace(type=self.final_type, text=self.final_text)

    async def close(self) -> None:
        self.calls.append("close")


def coordinator_args(path, queue, factory, transport, journal):
    args = [queue, factory, None, None, transport]
    if path.parent.name == "09-interruption":
        return [*args, journal]
    args.extend((None, "session", journal))
    if path.stem == "wrong_order":
        args.append("aec-no-reference")
    return args


def sentence_queue_from_drain_args(path: Path, args) -> asyncio.Queue:
    return args[2] if path.parent.name == "09-interruption" else args[3]


@pytest.mark.parametrize(
    "path", CANCEL_SCRIPTS, ids=lambda path: path.parent.name + "-" + path.stem
)
@pytest.mark.asyncio
async def test_completed_bot_does_not_consume_next_speech_start(path: Path) -> None:
    module = load_script(path)
    journal = FakeJournal()
    transport = FakeTransport()
    cancel = FakeCancel()
    bot_task = asyncio.create_task(asyncio.sleep(0))
    await bot_task

    if path.stem == "estimate":
        bot_task, cancel, _ledger, consumed = await module.route_barge_in(
            "speech_started",
            bot_task,
            cancel,
            module.TurnLedger(),
            transport,
            journal,
            [],
        )
    elif path.parent.name == "10-cleaning-signal":
        bot_task, cancel, consumed = await module.route_barge_in(
            "speech_started", bot_task, cancel, transport, journal, "session"
        )
    else:
        bot_task, cancel, consumed = await module.route_barge_in(
            "speech_started", bot_task, cancel, transport, journal
        )

    assert bot_task is None
    assert cancel is None
    assert consumed is False
    assert transport.clear_calls == 0
    assert "interruption.start" not in journal.names


@pytest.mark.asyncio
async def test_ignore_router_does_not_drop_event_after_bot_finishes() -> None:
    module = load_script(CHAPTER_9 / "ignore.py")
    journal = FakeJournal()
    bot_task = asyncio.create_task(asyncio.sleep(0))
    await bot_task

    bot_task, consumed = await module.route_ignored_event("speech_started", bot_task, journal)
    assert bot_task is None
    assert consumed is False
    assert "user.barge_in.ignored" not in journal.names


@pytest.mark.parametrize(
    "path", CANCEL_SCRIPTS, ids=lambda path: path.parent.name + "-" + path.stem
)
@pytest.mark.asyncio
async def test_barge_in_speech_started_becomes_next_stt_turn(path: Path, monkeypatch) -> None:
    module = load_script(path)
    journal = FakeJournal()
    transport = FakeTransport()
    stts = [
        FakeSTT("first question", module.STTEventType.FINAL),
        FakeSTT("interrupting question", module.STTEventType.FINAL),
    ]
    bot_starts: asyncio.Queue[int] = asyncio.Queue()
    active_agents = 0

    async def fake_run_agent(*args) -> None:
        nonlocal active_agents
        sentence_queue = args[2]
        cancel = args[3]
        active_agents += 1
        bot_starts.put_nowait(active_agents)
        try:
            await cancel.wait()
            await sentence_queue.put(None)
        finally:
            active_agents -= 1

    async def fake_drain(*args) -> None:
        sentence_queue = sentence_queue_from_drain_args(path, args)
        while await sentence_queue.get() is not None:
            pass

    monkeypatch.setattr(module, "run_agent", fake_run_agent)
    monkeypatch.setattr(module, "drain_to_speaker", fake_drain)
    monkeypatch.setattr(module, "CancelToken", FakeCancel)

    queue: asyncio.Queue = asyncio.Queue()
    iterator = iter(stts)
    task = asyncio.create_task(
        module.coordinator(
            *coordinator_args(path, queue, lambda: next(iterator), transport, journal)
        )
    )

    await queue.put(("speech_started", None))
    await queue.put(("frame", "first"))
    await queue.put(("speech_ended", None))
    await asyncio.wait_for(bot_starts.get(), timeout=1)

    await queue.put(("speech_started", None))
    await queue.put(("frame", "interrupt"))
    await queue.put(("speech_ended", None))
    await asyncio.wait_for(bot_starts.get(), timeout=1)

    assert stts[0].calls == ["start", "audio:first", "end", "close"]
    assert stts[1].calls == ["start", "audio:interrupt", "end", "close"]
    assert transport.clear_calls == 1
    assert "interruption.start" in journal.names

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active_agents == 0


@pytest.mark.parametrize("path", ALL_SCRIPTS, ids=lambda path: path.parent.name + "-" + path.stem)
@pytest.mark.asyncio
async def test_coordinator_shutdown_closes_incomplete_stt(path: Path) -> None:
    module = load_script(path)
    journal = FakeJournal()
    transport = FakeTransport()
    stt = FakeSTT("unused", module.STTEventType.FINAL)
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        module.coordinator(*coordinator_args(path, queue, lambda: stt, transport, journal))
    )

    await queue.put(("speech_started", None))
    await stt.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stt.calls == ["start", "end", "close"]


def test_barge_in_probe_uses_real_router() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER_9 / "barge_in_turn_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "bot_task_cleared": True,
        "cancel_token_cleared": True,
        "event_consumed": False,
        "events": [
            "bot.started",
            "transport.clear_audio",
            "bot.stopped",
            "stt.start",
            "stt.frame",
            "stt.end",
            "stt.close",
        ],
        "journal": ["interruption.start", "interruption.cancel_complete"],
    }


def test_chapters_9_and_10_close_shared_resources() -> None:
    for path in ALL_SCRIPTS:
        source = path.read_text(encoding="utf-8")
        assert "from contextlib import AsyncExitStack" in source
        assert "resources.push_async_callback(transport.disconnect)" in source
        assert "resources.push_async_callback(close_if_supported, vad)" in source
        assert "resources.push_async_callback(close_if_supported, client)" in source
        assert "resources.push_async_callback(close_if_supported, tts)" in source

    for path in ALL_SCRIPTS[-2:]:
        source = path.read_text(encoding="utf-8")
        assert "resources.push_async_callback(close_if_supported, nr)" in source
        assert "resources.push_async_callback(close_if_supported, aec)" in source


@pytest.mark.asyncio
async def test_chapter_10_replay_closes_offline_audio_stages(monkeypatch) -> None:
    path = TEACHING / "10-cleaning-signal" / "replay.py"
    module = load_script(path)
    closed: list[str] = []

    class FakeFilter:
        def __init__(self, name: str) -> None:
            self.name = name

        def version_info(self):
            return {"provider": self.name}

        def feed_reference(self, _chunk) -> None:
            return None

        async def process(self, chunk):
            return chunk

        async def close(self) -> None:
            closed.append(f"{self.name}.close")

    class FakeVAD:
        async def process(self, _chunk):
            if False:
                yield None

        async def close(self) -> None:
            closed.append("vad.close")

    audio_format = module.AudioFormat(sample_rate=16_000, channels=1, sample_width=2)
    frame = b"\0" * 640
    monkeypatch.setattr(module, "_read_wav", lambda _path: (frame, audio_format))
    monkeypatch.setattr(module, "create_noise_reducer", lambda _config: FakeFilter("nr"))
    monkeypatch.setattr(module, "create_echo_canceller", lambda _config: FakeFilter("aec"))
    monkeypatch.setattr(module, "create_vad", lambda _config: FakeVAD())
    monkeypatch.setattr(module, "export_debug_bundle", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "RUNS_DIR", ROOT)

    await module.run(Path("mic.wav"), None, "on", "on")
    assert closed == ["vad.close", "aec.close", "nr.close"]


def test_chapter_9_teaches_triggering_turn_continuity() -> None:
    readme = (CHAPTER_9 / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER_9 / "EXERCISES.md").read_text(encoding="utf-8")

    assert "Preserving the triggering utterance" in readme
    assert "barge_in_turn_probe.py" in readme
    assert "same `speech_started` event" in readme
    assert "barge_in_turn_probe.py" in exercises
    assert "must not be consumed" in exercises
