from __future__ import annotations

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
