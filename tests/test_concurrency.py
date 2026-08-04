"""Adversarial contracts for the leaf concurrency ownership primitives."""

from __future__ import annotations

import ast
import asyncio
import gc
import inspect
import weakref
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast

import pytest

from easycat._concurrency import (
    HardTimeoutStatus,
    LifecycleLock,
    LifecycleLockHeldError,
    OwnerState,
    ReservationState,
    RuntimeSupervisor,
    SurvivorCapacityError,
    SurvivorRegistry,
    hard_timeout,
    reap,
    shielded_cleanup,
    start_owned,
    swallow_cancel,
)


def _registry(
    *,
    supervisor_capacity: int = 4,
    root_capacity: int = 2,
    root_id: str = "root-1",
    journal: Any = None,
) -> tuple[RuntimeSupervisor, SurvivorRegistry]:
    supervisor = RuntimeSupervisor(capacity=supervisor_capacity, journal=journal)
    registry = SurvivorRegistry(
        supervisor=supervisor,
        root_id=root_id,
        capacity=root_capacity,
    )
    return supervisor, registry


async def _release_after_cancel(
    started: asyncio.Event,
    cancel_seen: asyncio.Event,
    release: asyncio.Event,
) -> str:
    started.set()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        cancel_seen.set()
        await release.wait()
    return "released"


def test_concurrency_module_is_a_package_leaf() -> None:
    source_path = Path(__file__).parents[1] / "src" / "easycat" / "_concurrency.py"
    tree = ast.parse(source_path.read_text())
    package_imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("easycat")
    ]
    assert package_imports == []


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: RuntimeSupervisor(capacity=0), "capacity must be positive"),
        (
            lambda: SurvivorRegistry(
                supervisor=RuntimeSupervisor(capacity=1),
                root_id="",
                capacity=1,
            ),
            "root_id must be non-empty",
        ),
        (
            lambda: SurvivorRegistry(
                supervisor=RuntimeSupervisor(capacity=1),
                root_id="root",
                capacity=0,
            ),
            "capacity must be positive",
        ),
    ],
)
def test_capacity_configuration_is_validated(factory: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


@pytest.mark.asyncio
async def test_normal_settlement_records_reserved_active_released() -> None:
    records: list[tuple[str, dict[str, object]]] = []
    supervisor, registry = _registry(
        journal=lambda event, data: records.append((event, dict(data)))
    )

    owned = await start_owned(
        lambda: asyncio.sleep(0, result="ok"),
        registry=registry,
        owner_id="session-1",
        task_name="worker",
    )

    assert owned.state is ReservationState.ACTIVE
    assert await owned.task == "ok"
    await asyncio.sleep(0)

    assert owned.state is ReservationState.RELEASED
    assert registry.drained
    assert supervisor.active_count == 0
    transitions = [data["state"] for event, data in records if event == "owned_task_transition"]
    assert transitions == ["reserved", "active", "released"]


@pytest.mark.asyncio
async def test_exceptional_settlement_releases_both_quotas() -> None:
    supervisor, registry = _registry()

    async def fail() -> None:
        raise RuntimeError("boom")

    owned = await start_owned(
        fail,
        registry=registry,
        owner_id="session-1",
        task_name="worker",
    )
    with pytest.raises(RuntimeError, match="boom"):
        await owned.task
    await asyncio.sleep(0)

    assert owned.state is ReservationState.RELEASED
    assert supervisor.active_count == registry.active_count == 0


@pytest.mark.asyncio
async def test_factory_failure_and_cancellation_release_reserved_capacity() -> None:
    supervisor, registry = _registry()

    def fail() -> Coroutine[Any, Any, None]:
        raise RuntimeError("factory boom")

    with pytest.raises(RuntimeError, match="factory boom"):
        await start_owned(
            fail,
            registry=registry,
            owner_id="session-1",
            task_name="failure",
        )

    def cancel() -> Coroutine[Any, Any, None]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await start_owned(
            cancel,
            registry=registry,
            owner_id="session-1",
            task_name="cancel",
        )

    assert supervisor.active_count == registry.active_count == 0


@pytest.mark.asyncio
async def test_cancellation_recorded_at_reservation_prevents_factory_invocation() -> None:
    caller = asyncio.current_task()
    assert caller is not None
    invoked = False

    def journal(event: str, data: Any) -> None:
        if event == "owned_task_transition" and data["state"] == "reserved":
            caller.cancel()

    supervisor, registry = _registry(journal=journal)

    async def work() -> None:
        nonlocal invoked
        invoked = True

    with pytest.raises(asyncio.CancelledError):
        await start_owned(
            work,
            registry=registry,
            owner_id="session-1",
            task_name="worker",
        )

    assert not invoked
    assert supervisor.active_count == registry.active_count == 0


@pytest.mark.asyncio
async def test_factory_requested_cancellation_parks_created_task_before_reraise() -> None:
    caller = asyncio.current_task()
    assert caller is not None
    records: list[tuple[str, dict[str, object]]] = []
    supervisor, registry = _registry(
        journal=lambda event, data: records.append((event, dict(data)))
    )

    async def work() -> None:
        await asyncio.Event().wait()

    def factory() -> Coroutine[Any, Any, None]:
        caller.cancel()
        return work()

    with pytest.raises(asyncio.CancelledError):
        await start_owned(
            factory,
            registry=registry,
            owner_id="session-1",
            task_name="worker",
        )
    await asyncio.wait(supervisor.tasks())
    await asyncio.sleep(0)

    assert any(
        event == "owned_task_transition" and data["state"] == "parked" for event, data in records
    )
    assert registry.owner_state("session-1") is OwnerState.CLOSED
    assert supervisor.active_count == 0


@pytest.mark.asyncio
async def test_root_capacity_rejects_before_factory_invocation() -> None:
    supervisor, registry = _registry(supervisor_capacity=3, root_capacity=1)
    release = asyncio.Event()
    first = await start_owned(
        release.wait,
        registry=registry,
        owner_id="session-1",
        task_name="first",
    )
    invoked = False

    async def rejected() -> None:
        nonlocal invoked
        invoked = True

    with pytest.raises(SurvivorCapacityError) as exc_info:
        await start_owned(
            rejected,
            registry=registry,
            owner_id="session-2",
            task_name="second",
        )

    assert exc_info.value.quota == "root"
    assert not invoked
    assert supervisor.active_count == registry.active_count == 1
    release.set()
    await first.task
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_runtime_capacity_is_aggregate_across_roots() -> None:
    supervisor = RuntimeSupervisor(capacity=1)
    first_root = SurvivorRegistry(supervisor=supervisor, root_id="root-1", capacity=2)
    second_root = SurvivorRegistry(supervisor=supervisor, root_id="root-2", capacity=2)
    release = asyncio.Event()
    first = await start_owned(
        release.wait,
        registry=first_root,
        owner_id="session-1",
        task_name="first",
    )
    invoked = False

    async def rejected() -> None:
        nonlocal invoked
        invoked = True

    with pytest.raises(SurvivorCapacityError) as exc_info:
        await start_owned(
            rejected,
            registry=second_root,
            owner_id="session-2",
            task_name="second",
        )

    assert exc_info.value.quota == "runtime"
    assert not invoked
    assert second_root.drained
    release.set()
    await first.task
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_child_scopes_share_one_root_quota() -> None:
    _supervisor, registry = _registry(root_capacity=1)
    child_registry = registry.for_child()
    assert child_registry is registry
    release = asyncio.Event()
    first = await start_owned(
        release.wait,
        registry=child_registry,
        owner_id="child-1",
        task_name="first",
    )

    async def rejected() -> None:
        raise AssertionError("capacity rejection must precede factory invocation")

    with pytest.raises(SurvivorCapacityError) as exc_info:
        await start_owned(
            rejected,
            registry=registry,
            owner_id="child-2",
            task_name="second",
        )
    assert exc_info.value.quota == "root"

    release.set()
    await first.task
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_bare_coroutine_and_running_task_adoption_are_rejected() -> None:
    _supervisor, registry = _registry()

    async def work() -> None:
        await asyncio.sleep(0)

    coroutine = work()
    with pytest.raises(TypeError, match="factory, not a bare coroutine"):
        await start_owned(
            coroutine,  # type: ignore[arg-type]
            registry=registry,
            owner_id="session-1",
            task_name="bare",
        )
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED

    release = asyncio.Event()
    running = asyncio.create_task(release.wait())
    with pytest.raises(TypeError, match="already-running"):
        await start_owned(
            lambda: cast(Any, running),
            registry=registry,
            owner_id="session-1",
            task_name="adopt",
        )
    assert registry.drained
    assert not running.cancelled()
    release.set()
    await running


@pytest.mark.asyncio
async def test_duplicate_parking_is_idempotent_and_final_settlement_releases() -> None:
    supervisor, registry = _registry()
    release = asyncio.Event()
    owned = await start_owned(
        release.wait,
        registry=registry,
        owner_id="session-1",
        task_name="worker",
    )

    assert owned.park()
    assert not owned.park()
    assert owned.state is ReservationState.PARKED
    assert supervisor.active_count == registry.active_count == 1
    assert supervisor.survivor_count == 1
    assert registry.owner_state("session-1") is OwnerState.CLOSED_WITH_SURVIVORS

    async def rejected() -> None:
        raise AssertionError("closed-owner rejection must precede factory invocation")

    with pytest.raises(RuntimeError, match="is closed"):
        await start_owned(
            rejected,
            registry=registry,
            owner_id="session-1",
            task_name="rejected",
        )

    release.set()
    await owned.task
    await asyncio.sleep(0)

    assert owned.state is ReservationState.RELEASED
    assert supervisor.active_count == registry.active_count == 0
    assert registry.owner_state("session-1") is OwnerState.CLOSED


@pytest.mark.asyncio
async def test_clean_owner_close_rejects_new_work() -> None:
    _supervisor, registry = _registry()

    assert registry.close_owner("session-1") is OwnerState.CLOSED

    async def rejected() -> None:
        raise AssertionError("closed-owner rejection must precede factory invocation")

    with pytest.raises(RuntimeError, match="is closed"):
        await start_owned(
            rejected,
            registry=registry,
            owner_id="session-1",
            task_name="rejected",
        )


@pytest.mark.asyncio
async def test_supervisor_anchors_survivor_after_lifecycle_owner_drop() -> None:
    class LifecycleOwner:
        pass

    supervisor, registry = _registry()
    registry_ref = weakref.ref(registry)
    lifecycle_owner = LifecycleOwner()
    owner_ref = weakref.ref(lifecycle_owner)
    release = asyncio.Event()
    owned = await start_owned(
        release.wait,
        registry=registry,
        owner_id="session-1",
        task_name="worker",
    )
    owned.park()

    del lifecycle_owner
    del owned
    del registry
    gc.collect()

    assert owner_ref() is None
    assert registry_ref() is not None
    assert len(supervisor.tasks()) == 1
    assert supervisor.survivors()[0].owner_id == "session-1"

    task = supervisor.tasks()[0]
    release.set()
    await task
    await asyncio.sleep(0)
    assert supervisor.active_count == 0
    gc.collect()
    assert registry_ref() is None


@pytest.mark.asyncio
async def test_reap_returns_finished_failure_and_cancelled_child_errors() -> None:
    _supervisor, registry = _registry()

    async def fail() -> None:
        raise RuntimeError("boom")

    failed = await start_owned(
        fail,
        registry=registry,
        owner_id="session-1",
        task_name="failed",
    )
    await asyncio.wait({failed.task})
    error = await reap(failed)
    assert isinstance(error, RuntimeError)
    assert str(error) == "boom"

    cancelled = await start_owned(
        lambda: asyncio.Event().wait(),
        registry=registry,
        owner_id="session-1",
        task_name="cancelled",
    )
    error = await reap(cancelled)
    assert isinstance(error, asyncio.CancelledError)


@pytest.mark.asyncio
async def test_reap_timeout_parks_cancellation_resistant_child() -> None:
    supervisor, registry = _registry()
    started = asyncio.Event()
    cancel_seen = asyncio.Event()
    release = asyncio.Event()
    owned = await start_owned(
        lambda: _release_after_cancel(started, cancel_seen, release),
        registry=registry,
        owner_id="session-1",
        task_name="worker",
    )
    await started.wait()

    error = await reap(owned, timeout=0)
    assert isinstance(error, TimeoutError)
    assert owned.state is ReservationState.PARKED
    assert registry.owner_state("session-1") is OwnerState.CLOSED_WITH_SURVIVORS
    await cancel_seen.wait()

    release.set()
    await owned.task
    await asyncio.sleep(0)
    assert owned.state is ReservationState.RELEASED
    assert supervisor.active_count == 0


@pytest.mark.asyncio
async def test_reap_caller_cancellation_parks_pending_child_and_reraises() -> None:
    _supervisor, registry = _registry()
    started = asyncio.Event()
    cancel_seen = asyncio.Event()
    release = asyncio.Event()
    owned = await start_owned(
        lambda: _release_after_cancel(started, cancel_seen, release),
        registry=registry,
        owner_id="session-1",
        task_name="worker",
    )
    await started.wait()
    reaper = asyncio.create_task(reap(owned))
    await cancel_seen.wait()

    reaper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reaper

    assert owned.state is ReservationState.PARKED
    release.set()
    await owned.task
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_reap_pending_entry_cancellation_parks_before_reraise() -> None:
    _supervisor, registry = _registry()
    release = asyncio.Event()
    owned = await start_owned(
        release.wait,
        registry=registry,
        owner_id="session-1",
        task_name="worker",
    )
    caller = asyncio.current_task()
    assert caller is not None
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await reap(owned)
    assert owned.state is ReservationState.PARKED

    await asyncio.wait({owned.task})
    await asyncio.sleep(0)
    assert owned.state is ReservationState.RELEASED


@pytest.mark.asyncio
async def test_reap_trailing_caller_cancellation_releases_settled_child() -> None:
    _supervisor, registry = _registry()

    async def run() -> None:
        caller = asyncio.current_task()
        assert caller is not None
        owned = await start_owned(
            lambda: asyncio.sleep(0),
            registry=registry,
            owner_id="session-1",
            task_name="worker",
        )
        owned.task.add_done_callback(lambda _task: caller.cancel())
        with pytest.raises(asyncio.CancelledError):
            await reap(owned)
        assert owned.state is ReservationState.RELEASED

    await run()


@pytest.mark.asyncio
async def test_hard_timeout_returns_completed_result_or_error() -> None:
    _supervisor, registry = _registry()
    loop = asyncio.get_running_loop()
    succeeded = await start_owned(
        lambda: asyncio.sleep(0),
        registry=registry,
        owner_id="session-1",
        task_name="success",
    )
    outcome = await hard_timeout(succeeded, loop.time() + 1)
    assert outcome.status is HardTimeoutStatus.COMPLETED
    assert outcome.error is None

    async def fail() -> None:
        raise ValueError("bad")

    failed = await start_owned(
        fail,
        registry=registry,
        owner_id="session-1",
        task_name="failure",
    )
    outcome = await hard_timeout(failed, loop.time() + 1)
    assert outcome.status is HardTimeoutStatus.COMPLETED
    assert isinstance(outcome.error, ValueError)


@pytest.mark.asyncio
async def test_hard_timeout_uses_absolute_deadline_and_parks_survivor() -> None:
    supervisor, registry = _registry()
    started = asyncio.Event()
    cancel_seen = asyncio.Event()
    release = asyncio.Event()
    owned = await start_owned(
        lambda: _release_after_cancel(started, cancel_seen, release),
        registry=registry,
        owner_id="session-1",
        task_name="worker",
    )
    await started.wait()

    outcome = await hard_timeout(owned, asyncio.get_running_loop().time())
    assert outcome.status is HardTimeoutStatus.TIMED_OUT_PARKED
    assert owned.state is ReservationState.PARKED
    assert supervisor.survivors() == registry.survivors()
    await cancel_seen.wait()

    release.set()
    await owned.task
    await asyncio.sleep(0)
    assert owned.state is ReservationState.RELEASED


@pytest.mark.asyncio
async def test_hard_timeout_caller_cancellation_parks_before_reraise() -> None:
    _supervisor, registry = _registry()
    started = asyncio.Event()
    cancel_seen = asyncio.Event()
    release = asyncio.Event()
    owned = await start_owned(
        lambda: _release_after_cancel(started, cancel_seen, release),
        registry=registry,
        owner_id="session-1",
        task_name="worker",
    )
    await started.wait()
    waiter = asyncio.create_task(hard_timeout(owned, asyncio.get_running_loop().time() + 60))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert owned.state is ReservationState.PARKED
    await cancel_seen.wait()

    release.set()
    await owned.task
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_hard_timeout_pending_entry_cancellation_parks_before_reraise() -> None:
    _supervisor, registry = _registry()
    owned = await start_owned(
        lambda: asyncio.Event().wait(),
        registry=registry,
        owner_id="session-1",
        task_name="worker",
    )
    caller = asyncio.current_task()
    assert caller is not None
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await hard_timeout(owned, asyncio.get_running_loop().time() + 60)
    assert owned.state is ReservationState.PARKED

    await asyncio.wait({owned.task})
    await asyncio.sleep(0)
    assert owned.state is ReservationState.RELEASED


@pytest.mark.asyncio
async def test_hard_timeout_rejects_parking_lifecycle_lock_holder() -> None:
    _supervisor, registry = _registry()
    lock = LifecycleLock(registry.supervisor)
    started = asyncio.Event()
    cancel_seen = asyncio.Event()
    release = asyncio.Event()

    async def work() -> None:
        async with lock:
            await _release_after_cancel(started, cancel_seen, release)

    owned = await start_owned(
        work,
        registry=registry,
        owner_id="session-1",
        task_name="worker",
    )
    await started.wait()

    outcome = await hard_timeout(owned, asyncio.get_running_loop().time())
    assert outcome.status is HardTimeoutStatus.PARK_REJECTED_LOCK_HELD
    assert isinstance(outcome.error, LifecycleLockHeldError)
    assert owned.state is ReservationState.ACTIVE
    assert registry.owner_state("session-1") is OwnerState.CLOSED_WITH_SURVIVORS
    await cancel_seen.wait()

    release.set()
    await owned.task
    await asyncio.sleep(0)
    assert not lock.locked()
    assert owned.state is ReservationState.RELEASED
    assert registry.owner_state("session-1") is OwnerState.CLOSED


@pytest.mark.asyncio
async def test_shielded_cleanup_returns_success_and_failure() -> None:
    success = await shielded_cleanup(lambda: asyncio.sleep(0, result="ok"))
    assert success.succeeded
    assert success.result == "ok"
    assert success.cancellation_requests == 0

    async def fail() -> None:
        raise RuntimeError("cleanup failed")

    failure = await shielded_cleanup(fail)
    assert isinstance(failure.error, RuntimeError)
    assert failure.result is None
    assert failure.cancellation_requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_shielded_cleanup_records_caller_cancellation(
    fails: bool,
    cancel_count: int,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup() -> str:
        started.set()
        await release.wait()
        if fails:
            raise RuntimeError("cleanup failed")
        return "ok"

    caller = asyncio.create_task(shielded_cleanup(cleanup))
    await started.wait()
    for _ in range(cancel_count):
        caller.cancel()
        await asyncio.sleep(0)
    release.set()
    settlement = await caller

    assert settlement.cancellation_requests == cancel_count
    assert not caller.cancelled()
    if fails:
        assert isinstance(settlement.error, RuntimeError)
    else:
        assert settlement.result == "ok"
        assert settlement.error is None


@pytest.mark.asyncio
async def test_shielded_cleanup_distinguishes_child_cancellation() -> None:
    async def cancel_self() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)

    settlement = await shielded_cleanup(cancel_self)
    assert isinstance(settlement.error, asyncio.CancelledError)
    assert settlement.cancellation_requests == 0


@pytest.mark.asyncio
async def test_swallow_cancel_suppresses_child_cancellation_and_journals() -> None:
    records: list[tuple[str, dict[str, object]]] = []
    child = asyncio.create_task(asyncio.sleep(60))
    child.cancel()

    async with swallow_cancel(journal=lambda event, data: records.append((event, dict(data)))):
        await child

    assert records == [("child_cancellation_swallowed", {"cancellation_requests": 0})]


@pytest.mark.asyncio
async def test_swallow_cancel_does_not_consume_caller_cancellation() -> None:
    entered = asyncio.Event()

    async def wait() -> None:
        async with swallow_cancel():
            entered.set()
            await asyncio.Event().wait()

    caller = asyncio.create_task(wait())
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller


@pytest.mark.asyncio
async def test_swallow_cancel_delivers_pre_entry_pending_cancellation() -> None:
    entered = False

    async def run() -> None:
        nonlocal entered
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        async with swallow_cancel():
            entered = True

    with pytest.raises(asyncio.CancelledError):
        await run()
    assert not entered


@pytest.mark.asyncio
async def test_swallow_cancel_ignores_stale_caught_request_for_child_cancel() -> None:
    task = asyncio.current_task()
    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.sleep(0)
    baseline = task.cancelling()

    child = asyncio.create_task(asyncio.sleep(60))
    child.cancel()
    async with swallow_cancel():
        await child

    assert task.cancelling() == baseline


@pytest.mark.asyncio
async def test_swallow_cancel_composes_with_asyncio_timeout() -> None:
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            async with swallow_cancel():
                await asyncio.Event().wait()
