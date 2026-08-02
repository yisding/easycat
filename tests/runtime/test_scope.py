from __future__ import annotations

import asyncio

import pytest

from easycat.runtime.scope import BackgroundTaskScope, JournalSink, RuntimeScope


def test_background_scope_closes_coroutine_when_task_creation_fails() -> None:
    scope = BackgroundTaskScope()

    async def work() -> None:
        pass

    coro = work()
    with pytest.raises(RuntimeError, match="no running event loop"):
        scope.create_task("timer", coro)

    assert coro.cr_frame is None


def test_background_scope_closes_coroutine_for_empty_name() -> None:
    scope = BackgroundTaskScope()

    async def work() -> None:
        pass

    coro = work()
    with pytest.raises(ValueError, match="must be non-empty"):
        scope.create_task("", coro)

    assert coro.cr_frame is None


@pytest.mark.asyncio
async def test_background_scope_closes_coroutine_for_duplicate_name() -> None:
    scope = BackgroundTaskScope()
    release = asyncio.Event()
    active = scope.create_task("timer", release.wait())

    async def rejected_work() -> None:
        pass

    rejected = rejected_work()
    with pytest.raises(RuntimeError, match="already active"):
        scope.create_task("timer", rejected)

    assert rejected.cr_frame is None
    release.set()
    await active


@pytest.mark.asyncio
async def test_background_scope_prunes_completed_task() -> None:
    scope = BackgroundTaskScope()

    task = scope.create_task("timer", asyncio.sleep(0))
    assert scope.active("timer")

    await task
    await asyncio.sleep(0)

    assert scope.empty


@pytest.mark.asyncio
async def test_background_scope_replaces_named_task() -> None:
    scope = BackgroundTaskScope()
    first_started = asyncio.Event()
    first_cleaned_up = asyncio.Event()
    release_second = asyncio.Event()

    async def first_timer() -> None:
        first_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            first_cleaned_up.set()

    first = scope.create_task("timer", first_timer())
    await first_started.wait()

    second = scope.create_task("timer", release_second.wait(), replace=True)
    await asyncio.sleep(0)

    assert first.cancelled()
    assert first_cleaned_up.is_set()
    assert scope.active("timer")
    assert scope.tasks() == (second,)

    release_second.set()
    await second
    await asyncio.sleep(0)

    assert scope.empty


@pytest.mark.asyncio
async def test_background_scope_cancel_detaches_name_immediately() -> None:
    scope = BackgroundTaskScope()
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def timer() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    task = scope.create_task("timer", timer())
    await started.wait()

    assert scope.cancel("timer") == (task,)
    assert not scope.active("timer")
    assert scope.empty

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned_up.is_set()


@pytest.mark.asyncio
async def test_background_scope_cancel_detaches_current_task_without_cancelling_it() -> None:
    scope = BackgroundTaskScope()
    completed = asyncio.Event()

    async def cancel_owner() -> None:
        current = asyncio.current_task()
        assert current is not None
        assert scope.cancel("timer") == (current,)
        await asyncio.sleep(0)
        completed.set()

    task = scope.create_task("timer", cancel_owner())
    await task

    assert not task.cancelled()
    assert completed.is_set()
    assert scope.empty


@pytest.mark.asyncio
async def test_background_scope_observes_task_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scope = BackgroundTaskScope()

    async def fail() -> None:
        raise RuntimeError("boom")

    scope.create_task("timer", fail())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert scope.empty
    assert "Background task 'timer' failed" in caplog.text


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
    never_released = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        try:
            await never_released.wait()
        finally:
            cleaned_up.set()

    task = scope.create_task("worker", wait_forever())
    await started.wait()

    await scope.cancel_and_drain("worker")

    assert task.cancelled()
    assert cleaned_up.is_set()
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_cancel_and_drain_ignores_preexisting_cancellation_count() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    owned = scope.create_task("worker", wait_forever())
    await started.wait()

    async def drain_after_caught_cancellation() -> int:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert current.cancelling() == 1

        await scope.cancel_and_drain("worker")
        return current.cancelling()

    caller = asyncio.create_task(drain_after_caught_cancellation())

    assert await caller == 1
    assert owned.cancelled()
    assert cleaned_up.is_set()
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_cancel_and_drain_preserves_cancellation_pending_at_entry() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        await asyncio.Event().wait()

    owned = scope.create_task("worker", wait_forever())
    await started.wait()

    async def cancel_before_drain() -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await scope.cancel_and_drain("worker")

    caller = asyncio.create_task(cancel_before_drain())

    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.gather(owned, return_exceptions=True)
    await scope.cancel_and_drain("worker")
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_cancel_and_drain_propagates_new_cancellation_count() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    awaiting_drain = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    owned = scope.create_task("worker", wait_forever())
    await started.wait()

    async def drain_after_caught_cancellation() -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert current.cancelling() == 1

        awaiting_drain.set()
        await scope.cancel_and_drain("worker")

    caller = asyncio.create_task(drain_after_caught_cancellation())
    await awaiting_drain.wait()
    await cleanup_started.wait()
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert caller.cancelling() == 2

    release_cleanup.set()
    await asyncio.gather(owned, return_exceptions=True)
    await scope.cancel_and_drain("worker")
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_cancel_and_drain_detaches_caller_and_cancels_siblings() -> None:
    scope = RuntimeScope()
    sibling_started = asyncio.Event()
    sibling_cleaned_up = asyncio.Event()
    caller_continued = asyncio.Event()

    async def sibling() -> None:
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cleaned_up.set()

    sibling_task = scope.create_task("sibling", sibling())
    await sibling_started.wait()

    async def cancel_from_owned_task() -> None:
        current = asyncio.current_task()
        assert current is not None
        scope.add_task("owner", current)

        await scope.cancel_and_drain()
        await asyncio.sleep(0)
        caller_continued.set()

    owner_task = asyncio.create_task(cancel_from_owned_task())
    await owner_task

    assert not owner_task.cancelled()
    assert caller_continued.is_set()
    assert sibling_task.cancelled()
    assert sibling_cleaned_up.is_set()
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_discard_detaches_without_cancelling_task() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def wait_for_release() -> str:
        started.set()
        await release.wait()
        completed.set()
        return "done"

    task = scope.create_task("worker", wait_for_release())
    await started.wait()

    scope.discard(task)

    assert scope.empty
    assert not task.cancelled()
    assert not task.done()

    release.set()
    assert await task == "done"
    assert completed.is_set()


@pytest.mark.asyncio
async def test_runtime_scope_discard_allows_current_task_to_detach_itself() -> None:
    scope = RuntimeScope()

    async def self_detach() -> None:
        task = asyncio.current_task()
        assert task is not None
        scope.add_task("self", task)
        assert scope.tasks("self") == (task,)

        scope.discard(task)

        assert scope.empty

    await self_detach()


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


@pytest.mark.asyncio
async def test_runtime_scope_cancel_and_drain_preserves_caller_cancellation() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_started = asyncio.Event()

    async def stubborn_cancel_cleanup() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    task = scope.create_task("worker", stubborn_cancel_cleanup())
    await started.wait()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(scope.cancel_and_drain("worker"), timeout=0.01)

    assert cleanup_started.is_set()
    assert not scope.empty

    release_cleanup.set()
    await scope.cancel_and_drain("worker")

    assert task.cancelled()
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_drain_preserves_caller_cancellation_without_cancelling_task() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_release() -> str:
        started.set()
        await release.wait()
        return "done"

    task = scope.create_task("worker", wait_for_release())
    await started.wait()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(scope.drain("worker"), timeout=0.01)

    assert not task.cancelled()
    assert not scope.empty

    release.set()
    await scope.drain("worker")

    assert task.result() == "done"
    assert scope.empty
