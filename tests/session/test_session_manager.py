from __future__ import annotations

import asyncio

import pytest

from easycat import session_manager as session_manager_module
from easycat.session import Session
from easycat.session_manager import (
    SessionManager,
    SessionStopAbandonReport,
    SessionStopFailure,
    SessionStopReport,
)
from tests.session._session_core_helpers import FakeTransport, _full_config


class _DummySession:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


@pytest.mark.asyncio
async def test_await_owned_stop_ignores_preexisting_cancellation_count() -> None:
    owned = asyncio.create_task(asyncio.Event().wait())
    owned.cancel()
    await asyncio.gather(owned, return_exceptions=True)

    async def reap_after_caught_cancellation() -> tuple[bool, int]:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert current.cancelling() == 1

        completed = await session_manager_module._await_owned_stop(owned)
        return completed, current.cancelling()

    caller = asyncio.create_task(reap_after_caught_cancellation())

    assert await caller == (False, 1)
    assert owned.cancelled()


@pytest.mark.asyncio
async def test_await_owned_stop_preserves_cancellation_pending_at_entry() -> None:
    owned = asyncio.create_task(asyncio.Event().wait())

    async def cancel_before_await() -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await session_manager_module._await_owned_stop(owned)

    caller = asyncio.create_task(cancel_before_await())

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not owned.done()
    owned.cancel()
    await asyncio.gather(owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_await_owned_stop_prioritizes_new_caller_cancellation() -> None:
    owned_started = asyncio.Event()
    awaiting_owned = asyncio.Event()
    continued = asyncio.Event()

    async def wait_forever() -> None:
        owned_started.set()
        await asyncio.Event().wait()

    owned = asyncio.create_task(wait_forever())
    await owned_started.wait()

    async def reap_after_caught_cancellation() -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert current.cancelling() == 1

        awaiting_owned.set()
        await session_manager_module._await_owned_stop(owned)
        continued.set()

    caller = asyncio.create_task(reap_after_caught_cancellation())

    def cancel_caller(_task: asyncio.Task[None]) -> None:
        caller.cancel()

    # Register before _await_owned_stop() creates shield's child callback so
    # both cancellations are visible when its CancelledError handler runs.
    owned.add_done_callback(cancel_caller)
    await awaiting_owned.wait()
    owned.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert caller.cancelling() == 2
    assert owned.cancelled()
    assert not continued.is_set()


@pytest.mark.asyncio
async def test_session_manager_add_remove() -> None:
    manager: SessionManager[str] = SessionManager()
    session = _DummySession()

    await manager.add("a", session)  # type: ignore[arg-type]

    assert manager.get("a") is session
    assert session.started == 1

    await manager.remove("a")
    assert manager.get("a") is None
    assert session.stopped == 1


@pytest.mark.asyncio
async def test_session_manager_releases_key_when_start_fails() -> None:
    manager: SessionManager[str] = SessionManager()

    class FailingSession(_DummySession):
        async def start(self) -> None:
            self.started += 1
            raise RuntimeError("start failed")

    failed = FailingSession()
    with pytest.raises(RuntimeError, match="start failed"):
        await manager.add("reusable", failed)  # type: ignore[arg-type]

    assert manager.get("reusable") is None
    assert failed.started == 1
    assert failed.stopped == 0

    replacement = _DummySession()
    await manager.add("reusable", replacement)  # type: ignore[arg-type]
    assert manager.get("reusable") is replacement
    await manager.remove("reusable")
    assert replacement.stopped == 1


@pytest.mark.asyncio
async def test_session_manager_releases_key_when_start_is_cancelled() -> None:
    manager: SessionManager[str] = SessionManager()
    start_entered = asyncio.Event()

    class BlockingSession(_DummySession):
        async def start(self) -> None:
            self.started += 1
            start_entered.set()
            await asyncio.Event().wait()

    cancelled = BlockingSession()
    add_task = asyncio.create_task(manager.add("reusable", cancelled))  # type: ignore[arg-type]
    await start_entered.wait()
    add_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await add_task

    assert manager.get("reusable") is None
    assert cancelled.started == 1
    assert cancelled.stopped == 0

    replacement = _DummySession()
    await manager.add("reusable", replacement)  # type: ignore[arg-type]
    assert manager.get("reusable") is replacement
    await manager.remove("reusable")
    assert replacement.stopped == 1


@pytest.mark.parametrize("release_method", ["remove", "stop_all"])
@pytest.mark.asyncio
async def test_cancelled_add_does_not_remove_replacement_session(
    release_method: str,
) -> None:
    manager: SessionManager[str] = SessionManager()
    start_entered = asyncio.Event()

    class BlockingSession(_DummySession):
        async def start(self) -> None:
            self.started += 1
            start_entered.set()
            await asyncio.Event().wait()

    original = BlockingSession()
    add_task = asyncio.create_task(manager.add("reused", original))  # type: ignore[arg-type]
    await start_entered.wait()

    if release_method == "remove":
        await manager.remove("reused")
    else:
        await manager.stop_all()

    replacement = _DummySession()
    await manager.add("reused", replacement)  # type: ignore[arg-type]
    add_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await add_task

    assert manager.get("reused") is replacement
    assert original.stopped == 1
    await manager.remove("reused")
    assert replacement.stopped == 1


@pytest.mark.asyncio
async def test_cancelled_real_session_start_rolls_back_before_manager_untracks() -> None:
    manager: SessionManager[str] = SessionManager()

    class BlockingWarmupTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.warmup_entered = asyncio.Event()
            self.disconnect_entered = asyncio.Event()
            self.allow_disconnect = asyncio.Event()

        async def warmup(self) -> None:
            self.warmup_entered.set()
            await asyncio.Event().wait()

        async def disconnect(self) -> None:
            self.disconnect_entered.set()
            await self.allow_disconnect.wait()
            await super().disconnect()

    transport = BlockingWarmupTransport()
    session = Session(_full_config(transport=transport))
    add_task = asyncio.create_task(manager.add("cancelled", session))

    await transport.warmup_entered.wait()
    assert transport.connected
    add_task.cancel()

    await transport.disconnect_entered.wait()
    assert manager.get("cancelled") is session

    # Repeated cancellation must not interrupt the rollback already in
    # progress or let the manager untrack the session before disconnect.
    add_task.cancel()
    await asyncio.sleep(0)
    assert not add_task.done()
    assert manager.get("cancelled") is session
    transport.allow_disconnect.set()

    with pytest.raises(asyncio.CancelledError):
        await add_task

    assert manager.get("cancelled") is None
    assert not session.is_running
    assert transport.disconnected
    assert session._health_checkers == []
    assert session._audio_router.pipeline_task is None
    assert session._audio_router.outbound_task is None


@pytest.mark.asyncio
async def test_session_manager_stop_all() -> None:
    manager: SessionManager[str] = SessionManager()
    a = _DummySession()
    b = _DummySession()

    await manager.add("a", a)  # type: ignore[arg-type]
    await manager.add("b", b)  # type: ignore[arg-type]

    report = await manager.stop_all()

    assert manager.get("a") is None
    assert manager.get("b") is None
    assert a.stopped == 1
    assert b.stopped == 1
    assert isinstance(report, SessionStopReport)
    assert report.ok
    assert report.attempted_keys == ("a", "b")
    assert report.stopped_keys == ("a", "b")
    assert report.failed_keys == ()
    assert report.failures == ()


@pytest.mark.asyncio
async def test_session_manager_stop_all_reports_failures_without_short_circuiting() -> None:
    manager: SessionManager[str] = SessionManager()

    class RetryableFailure(_DummySession):
        def __init__(self) -> None:
            super().__init__()
            self.should_fail = True

        async def stop(self) -> None:
            self.stopped += 1
            if self.should_fail:
                raise RuntimeError("teardown failed")

    failing = RetryableFailure()
    healthy = _DummySession()
    await manager.add("failing", failing)  # type: ignore[arg-type]
    await manager.add("healthy", healthy)  # type: ignore[arg-type]

    report = await manager.stop_all()

    assert not report.ok
    assert report.attempted_keys == ("failing", "healthy")
    assert report.stopped_keys == ("healthy",)
    assert report.failed_keys == ("failing",)
    assert len(report.failures) == 1
    failure = report.failures[0]
    assert isinstance(failure, SessionStopFailure)
    assert failure.key == "failing"
    assert isinstance(failure.exception, RuntimeError)
    assert str(failure.exception) == "teardown failed"
    assert failing.stopped == 1
    assert healthy.stopped == 1
    assert manager.get("failing") is failing
    assert manager.get("healthy") is None

    failing.should_fail = False
    retry = await manager.stop_all()

    assert retry.ok
    assert retry.attempted_keys == ("failing",)
    assert retry.stopped_keys == ("failing",)
    assert manager.active_keys() == ()


@pytest.mark.asyncio
async def test_session_manager_stop_all_empty_report_is_successful() -> None:
    report = await SessionManager[str]().stop_all()

    assert report.ok
    assert report.attempted_keys == ()
    assert report.stopped_keys == ()
    assert report.failures == ()


@pytest.mark.asyncio
async def test_abandon_pending_stops_reaps_task_but_retains_session() -> None:
    manager: SessionManager[str] = SessionManager()
    force_started = asyncio.Event()
    release = asyncio.Event()

    class BlockingSession(_DummySession):
        async def stop(self, *, force: bool = False) -> None:
            assert force is True
            force_started.set()
            await release.wait()

    session = BlockingSession()
    await manager.add("call", session)  # type: ignore[arg-type]
    remove_task = asyncio.create_task(manager.remove("call", force=True))
    await force_started.wait()
    remove_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await remove_task

    report = await manager.abandon_pending_stops()

    assert isinstance(report, SessionStopAbandonReport)
    assert not report.ok
    assert report.attempted_keys == ("call",)
    assert report.cancelled_keys == ("call",)
    assert report.retained_keys == ("call",)
    assert report.failures == ()
    assert manager.get("call") is session
    assert manager._stop_task_scope.tasks() == ()

    release.set()
    retry = await manager.stop_all(force=True)
    assert retry.ok
    assert manager.active_keys() == ()


@pytest.mark.asyncio
async def test_abandon_pending_stops_retains_cancellation_resistant_task() -> None:
    manager: SessionManager[str] = SessionManager()
    force_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()

    class ResistantSession(_DummySession):
        async def stop(self, *, force: bool = False) -> None:
            assert force is True
            force_started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()

    session = ResistantSession()
    await manager.add("call", session)  # type: ignore[arg-type]
    remove_task = asyncio.create_task(manager.remove("call", force=True))
    await force_started.wait()

    report = await manager.abandon_pending_stops()

    assert not report.ok
    assert report.attempted_keys == ("call",)
    assert report.cancelled_keys == ()
    assert report.retained_keys == ("call",)
    assert report.failures == ()
    assert cancellation_seen.is_set()
    assert manager.get("call") is session
    assert len(manager._stop_task_scope.tasks()) == 1

    release.set()
    await remove_task
    assert manager.active_keys() == ()
    assert manager._stop_task_scope.tasks() == ()


def test_session_manager_releases_stop_scope_before_cross_loop_reuse() -> None:
    manager: SessionManager[str] = SessionManager()

    async def run_once(key: str) -> None:
        session = _DummySession()
        await manager.add(key, session)  # type: ignore[arg-type]
        await manager.remove(key)

    asyncio.run(run_once("first"))
    asyncio.run(run_once("second"))

    assert manager.active_keys() == ()


@pytest.mark.asyncio
async def test_cancelled_remove_retains_session_for_force_sweep() -> None:
    manager: SessionManager[str] = SessionManager()

    class BlockingSession:
        def __init__(self) -> None:
            self.started = 0
            self.graceful_started = asyncio.Event()
            self.forced = False

        async def start(self) -> None:
            self.started += 1

        async def stop(self, *, force: bool = False) -> None:
            if force:
                self.forced = True
                return
            self.graceful_started.set()
            await asyncio.Event().wait()

    session = BlockingSession()
    await manager.add("call", session)  # type: ignore[arg-type]
    remove_task = asyncio.create_task(manager.remove("call"))
    await session.graceful_started.wait()
    stop_task = manager._stop_tasks["call"][1]

    assert manager._stop_task_scope.tasks() == (stop_task,)

    remove_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await remove_task

    assert manager.get("call") is session
    assert stop_task.done() is False
    assert manager._stop_task_scope.tasks() == (stop_task,)
    await manager.stop_all(force=True)
    assert session.forced is True
    assert manager.get("call") is None
    assert manager._stop_task_scope.tasks() == ()


@pytest.mark.asyncio
async def test_force_sweep_runs_when_graceful_cancellation_raises() -> None:
    manager: SessionManager[str] = SessionManager()

    class FailingCancellationSession:
        def __init__(self) -> None:
            self.graceful_started = asyncio.Event()
            self.forced = False

        async def start(self) -> None:
            return

        async def stop(self, *, force: bool = False) -> None:
            if force:
                self.forced = True
                return
            self.graceful_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                raise RuntimeError("graceful cancellation failed") from exc

    session = FailingCancellationSession()
    await manager.add("call", session)  # type: ignore[arg-type]
    graceful = asyncio.create_task(manager.remove("call"))
    await session.graceful_started.wait()

    await manager.stop_all(force=True)
    await asyncio.gather(graceful, return_exceptions=True)

    assert session.forced is True
    assert manager.get("call") is None


@pytest.mark.asyncio
async def test_session_manager_connection_can_attach_runtime_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager: SessionManager[str] = SessionManager()
    session = _DummySession()
    attached: list[object] = []

    monkeypatch.setattr("easycat.helpers.attach_runtime_feedback", attached.append)

    async with manager.connection("a", session, runtime_feedback=True):
        assert manager.get("a") is session
        assert session.started == 1

    assert attached == [session]
    assert manager.get("a") is None
    assert session.stopped == 1


@pytest.mark.asyncio
async def test_session_manager_connection_leaves_feedback_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager: SessionManager[str] = SessionManager()
    session = _DummySession()
    attached: list[object] = []

    monkeypatch.setattr("easycat.helpers.attach_runtime_feedback", attached.append)

    async with manager.connection("a", session):
        assert manager.get("a") is session

    assert attached == []
