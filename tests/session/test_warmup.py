from __future__ import annotations

import asyncio
from typing import Any

import pytest

from easycat.runtime.records import JournalRecordKind
from easycat.session._warmup import WarmupRunner


class _JournalSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append_record(
        self,
        *,
        name: str,
        kind: JournalRecordKind = JournalRecordKind.EVENT,
        turn_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.records.append(
            {
                "name": name,
                "kind": kind,
                "turn_id": turn_id,
                "data": data,
            }
        )


class _AsyncWarmup:
    def __init__(self, calls: list[str], name: str) -> None:
        self._calls = calls
        self._name = name

    async def warmup(self) -> None:
        self._calls.append(self._name)


class _SyncWarmup:
    def __init__(self, calls: list[str], name: str) -> None:
        self._calls = calls
        self._name = name

    def warmup(self) -> None:
        self._calls.append(self._name)


class _FailingWarmup:
    async def warmup(self) -> None:
        raise RuntimeError("boom")


class _BlockingWarmup:
    def __init__(
        self,
        *,
        name: str,
        started: list[str],
        all_started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._name = name
        self._started = started
        self._all_started = all_started
        self._release = release

    async def warmup(self) -> None:
        self._started.append(self._name)
        if len(self._started) == 2:
            self._all_started.set()
        await self._release.wait()


@pytest.mark.asyncio
async def test_warmup_runner_calls_supported_components_and_records_completion() -> None:
    calls: list[str] = []
    sink = _JournalSink()
    runner = WarmupRunner(
        enabled=True,
        journal_sink=sink,
        components=(
            ("stt", _AsyncWarmup(calls, "stt")),
            ("unsupported", object()),
            ("tts", _SyncWarmup(calls, "tts")),
        ),
    )

    await runner.run()

    assert calls == ["stt", "tts"]
    assert [record["name"] for record in sink.records] == ["warmup_completed"]
    record = sink.records[0]
    assert record["kind"] == JournalRecordKind.EVENT
    assert record["data"]["elapsed_ms"] >= 0
    assert [component["component"] for component in record["data"]["components"]] == [
        "stt",
        "tts",
    ]


@pytest.mark.asyncio
async def test_warmup_runner_runs_supported_components_concurrently() -> None:
    started: list[str] = []
    all_started = asyncio.Event()
    release = asyncio.Event()
    sink = _JournalSink()
    runner = WarmupRunner(
        enabled=True,
        journal_sink=sink,
        components=(
            (
                "stt",
                _BlockingWarmup(
                    name="stt",
                    started=started,
                    all_started=all_started,
                    release=release,
                ),
            ),
            (
                "agent",
                _BlockingWarmup(
                    name="agent",
                    started=started,
                    all_started=all_started,
                    release=release,
                ),
            ),
        ),
    )

    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(all_started.wait(), timeout=1.0)

    assert started == ["stt", "agent"]
    release.set()
    await task
    assert [record["name"] for record in sink.records] == ["warmup_completed"]
    assert [c["component"] for c in sink.records[0]["data"]["components"]] == ["stt", "agent"]


@pytest.mark.asyncio
async def test_warmup_runner_noops_when_disabled() -> None:
    calls: list[str] = []
    sink = _JournalSink()
    runner = WarmupRunner(
        enabled=False,
        journal_sink=sink,
        components=(("stt", _AsyncWarmup(calls, "stt")),),
    )

    await runner.run()

    assert calls == []
    assert sink.records == []


@pytest.mark.asyncio
async def test_warmup_runner_records_failure_and_reraises() -> None:
    sink = _JournalSink()
    runner = WarmupRunner(
        enabled=True,
        journal_sink=sink,
        components=(("stt", _FailingWarmup()),),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await runner.run()

    assert [record["name"] for record in sink.records] == ["warmup_failed"]
    record = sink.records[0]
    assert record["kind"] == JournalRecordKind.CONTROL
    assert record["data"]["component"] == "stt"
    assert record["data"]["elapsed_ms"] >= 0
    assert record["data"]["exc_type"] == "RuntimeError"
