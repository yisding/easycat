"""RuntimeTaskScope attachment and standalone-root ownership contracts."""

from __future__ import annotations

import asyncio
import logging

import pytest

from easycat.runtime._event_tasks import RuntimeTaskScope
from easycat.runtime.scope import RuntimeScope, RuntimeScopeState, RuntimeSupervisor


def _task_scope() -> RuntimeTaskScope:
    return RuntimeTaskScope(
        owner_label="test-owner",
        member_name="test_member",
        cohort="test-cohort",
        logger=logging.getLogger(__name__),
        failure_message="test task failed",
        drop_if_closed=False,
    )


def _root(name: str) -> RuntimeScope:
    return RuntimeScope.create_root(
        name=name,
        root_id=f"test-root:{name}",
        supervisor=RuntimeSupervisor(capacity=1),
        survivor_capacity=1,
    )


def test_off_loop_attach_does_not_register_child_before_promotion_fails() -> None:
    loop = asyncio.new_event_loop()
    tasks = _task_scope()
    release: asyncio.Event

    async def start() -> tuple[asyncio.Event, asyncio.Task[None]]:
        blocker = asyncio.Event()
        task = tasks.create_task(blocker.wait(), task_name="standalone-work")
        assert task is not None
        return blocker, task

    release, task = loop.run_until_complete(start())
    parent = _root("off-loop-parent")
    try:
        with pytest.raises(RuntimeError, match="no running event loop"):
            tasks.attach(parent, name="attached-child")

        assert parent.children() == ()
        assert tasks.scope is not None
        assert tasks.scope.tasks("test_member") == (task,)
    finally:
        loop.call_soon_threadsafe(release.set)
        loop.run_until_complete(task)
        loop.run_until_complete(tasks.release_standalone_if_empty())
        loop.close()


@pytest.mark.asyncio
async def test_bind_promotes_active_standalone_task_and_retires_old_root() -> None:
    tasks = _task_scope()
    release = asyncio.Event()
    task = tasks.create_task(release.wait(), task_name="standalone-work")
    assert task is not None
    standalone = tasks.scope
    assert standalone is not None
    attached = _root("attached")

    tasks.bind(attached)

    assert standalone.tasks("test_member") == ()
    assert attached.tasks("test_member") == (task,)
    assert tasks.owns_root is False

    release.set()
    await task
    await tasks.release_standalone_if_empty()

    assert attached.tasks("test_member") == ()
    assert standalone.state is RuntimeScopeState.CLOSED


@pytest.mark.asyncio
async def test_attach_promotes_active_standalone_task_into_named_child() -> None:
    tasks = _task_scope()
    release = asyncio.Event()
    task = tasks.create_task(release.wait(), task_name="standalone-work")
    assert task is not None
    parent = _root("parent")

    tasks.attach(parent, name="attached-child")

    scope = tasks.scope
    assert scope is not None
    assert scope.parent is parent
    assert parent.tasks("test_member") == (task,)

    release.set()
    await task
    await tasks.release_standalone_if_empty()


@pytest.mark.asyncio
async def test_rebind_still_rejects_work_owned_by_an_external_scope() -> None:
    tasks = _task_scope()
    first = _root("first")
    second = _root("second")
    tasks.bind(first)
    release = asyncio.Event()
    task = tasks.create_task(release.wait(), task_name="attached-work")
    assert task is not None

    with pytest.raises(RuntimeError, match="Cannot rebind.*while emissions are active"):
        tasks.bind(second)

    assert first.tasks("test_member") == (task,)
    assert second.tasks("test_member") == ()

    release.set()
    await task


@pytest.mark.asyncio
async def test_failed_promotion_keeps_standalone_ownership_intact() -> None:
    tasks = _task_scope()
    release = asyncio.Event()
    task = tasks.create_task(release.wait(), task_name="standalone-work")
    assert task is not None
    standalone = tasks.scope
    assert standalone is not None
    closed = _root("closed")
    await closed.close()

    with pytest.raises(RuntimeError, match="RuntimeScope .* is closed"):
        tasks.bind(closed)

    assert tasks.scope is standalone
    assert tasks.owns_root is True
    assert standalone.tasks("test_member") == (task,)

    release.set()
    await task
    await tasks.release_standalone_if_empty()
