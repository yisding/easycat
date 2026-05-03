from __future__ import annotations

import asyncio

import pytest

from easycat.runtime.scope import RuntimeScope


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
