from __future__ import annotations

import asyncio

import pytest

from easycat.runtime.scope import JournalSink, RuntimeScope


def test_create_journaled_task_records_lifecycle_via_structural_sink() -> None:
    class RecordingSink:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

        def current_turn_id(self, turn_id: str | None = None) -> str | None:
            return turn_id or "turn-1"

        def append_record(
            self,
            *,
            name: str,
            turn_id: str | None = None,
            data: dict[str, object] | None = None,
        ) -> None:
            self.records.append({"name": name, "turn_id": turn_id, "data": data})

    sink = RecordingSink()
    # A duck-typed sink satisfies the runtime-owned protocol structurally,
    # so the runtime layer needs no dependency on the session package.
    assert isinstance(sink, JournalSink)

    scope = RuntimeScope()

    async def work() -> str:
        return "ok"

    async def run() -> None:
        task = scope.create_journaled_task(work(), name="job", journal_sink=sink)
        await scope.drain("job")
        assert task.result() == "ok"

    asyncio.run(run())

    names = [r["name"] for r in sink.records]
    assert names == ["task_scheduled", "task_completed"]
    assert all(r["turn_id"] == "turn-1" for r in sink.records)


@pytest.mark.asyncio
async def test_runtime_scope_drains_completed_task() -> None:
    scope = RuntimeScope()

    async def complete() -> str:
        await asyncio.sleep(0)
        return "done"

    task = scope.create_task("worker", complete())

    await scope.drain("worker")

    assert task.done()
    assert task.result() == "done"
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_cancel_and_drain_cancels_task() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleaned_up.set()

    task = scope.create_task("worker", wait_forever())
    await started.wait()

    await scope.cancel_and_drain("worker")

    assert task.cancelled()
    assert cleaned_up.is_set()
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_drain_propagates_task_exceptions() -> None:
    scope = RuntimeScope()

    async def fail() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    scope.create_task("worker", fail())

    with pytest.raises(RuntimeError, match="boom"):
        await scope.drain("worker")

    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_drain_observes_completed_task_exceptions() -> None:
    scope = RuntimeScope()

    async def fail() -> None:
        raise RuntimeError("boom")

    task = scope.create_task("worker", fail())
    while not task.done():
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="boom"):
        await scope.drain("worker")

    assert scope.empty
