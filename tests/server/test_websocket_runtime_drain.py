"""Shutdown-order tests for the shared raw-WebSocket session runtime."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import weakref

import pytest

from easycat._concurrency import (
    OwnerState,
    RuntimeSupervisor,
    SurvivorCapacityError,
    SurvivorRegistry,
    reap,
    start_owned,
)
from easycat.server.transports import WebSocketSessionRuntime, cancel_handler_tasks
from easycat.session_manager import SessionManager


class _Server:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.close_calls: list[bool] = []

    def close(self, close_connections: bool = True) -> None:
        self.close_calls.append(close_connections)
        self.events.append(f"listener_close:{close_connections}")

    async def wait_closed(self) -> None:
        self.events.append("listener_wait_closed")


class _Connection:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.waiting = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls: list[tuple[int, str]] = []

    async def wait_closed(self) -> None:
        self.waiting.set()
        await self.closed.wait()
        self.events.append("connection_wait_closed")

    async def close(self, *, code: int, reason: str) -> None:
        self.close_calls.append((code, reason))
        self.events.append("connection_close")
        self.closed.set()


class _Session:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.graceful_started = asyncio.Event()
        self.allow_graceful = asyncio.Event()
        self.closed = False

    async def start(self) -> None:
        self.events.append("session_start")

    async def stop(self, *, force: bool = False) -> None:
        if self.closed:
            return
        if force:
            self.events.append("session_force")
            self.closed = True
            self.allow_graceful.set()
            return
        self.events.append("session_graceful_start")
        self.graceful_started.set()
        await self.allow_graceful.wait()
        self.closed = True
        self.events.append("session_graceful_done")


class _Manager:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.sessions: dict[int, _Session] = {}

    async def add(self, key: int, session: _Session) -> None:
        self.sessions[key] = session
        await session.start()

    async def remove(self, key: int) -> None:
        session = self.sessions.pop(key, None)
        if session is not None:
            await session.stop()

    async def stop_all(self) -> None:
        sessions = list(self.sessions.values())
        self.sessions.clear()
        for session in sessions:
            await session.stop()
        self.events.append("manager_stop_all")


async def test_runtime_keeps_connection_open_until_graceful_session_drain() -> None:
    events: list[str] = []
    manager = _Manager(events)
    server = _Server(events)
    connection = _Connection(events)
    session = _Session(events)
    runtime = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="test-websocket-runtime",
        session_factory=lambda _connection: session,
    )

    handler = asyncio.create_task(runtime.handle(connection))
    await asyncio.wait_for(connection.waiting.wait(), timeout=1)

    drain = asyncio.create_task(
        runtime.drain(
            server,
            drain_timeout_s=1.0,
            force_timeout_s=1.0,
        )
    )
    await asyncio.wait_for(session.graceful_started.wait(), timeout=1)

    # Shutdown has stopped admission, but the established media connection is
    # still live while the session flushes its in-flight work.
    assert server.close_calls == [False]
    assert connection.close_calls == []
    assert connection.closed.is_set() is False

    session.allow_graceful.set()
    await asyncio.wait_for(drain, timeout=2)
    await asyncio.wait_for(handler, timeout=1)

    assert connection.close_calls == [(1001, "Server shutdown after drain")]
    assert events.index("listener_close:False") < events.index("session_graceful_start")
    assert events.index("session_graceful_done") < events.index("connection_close")
    assert "session_force" not in events
    assert runtime.listener_cleanup_state is OwnerState.CLOSED


async def test_runtime_rejects_new_connection_after_drain_starts() -> None:
    events: list[str] = []
    manager = _Manager(events)
    server = _Server(events)
    runtime = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="test-websocket-runtime",
        session_factory=lambda _connection: _Session(events),
    )
    runtime.start_draining(server)
    connection = _Connection(events)

    await runtime.handle(connection)

    assert connection.close_calls == [(1013, "Server is draining")]
    assert manager.sessions == {}
    assert server.close_calls == [False]


async def test_runtime_drains_after_listener_close_failure() -> None:
    """A listener-close failure must not skip the established-session drain."""

    events: list[str] = []

    class _FailingCloseServer(_Server):
        def close(self, close_connections: bool = True) -> None:
            super().close(close_connections)
            raise RuntimeError("listener close failed")

    manager = _Manager(events)
    server = _FailingCloseServer(events)
    connection = _Connection(events)
    session = _Session(events)
    runtime = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="test-websocket-runtime",
        session_factory=lambda _connection: session,
    )
    handler = asyncio.create_task(runtime.handle(connection))
    await asyncio.wait_for(connection.waiting.wait(), timeout=1)

    with pytest.raises(RuntimeError, match="listener close failed"):
        await asyncio.wait_for(
            runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=1.0), timeout=2
        )

    assert session.closed is True
    assert connection.close_calls == [(1001, "Server shutdown after drain")]
    assert "manager_stop_all" in events
    await asyncio.wait_for(handler, timeout=1)


async def test_runtime_surfaces_failed_manager_sweep_and_retains_ledgers_for_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class RetryableSession:
        def __init__(self) -> None:
            self.fail_stop = True

        async def start(self) -> None:
            pass

        async def stop(self, *, force: bool = False) -> None:
            if self.fail_stop:
                raise RuntimeError("retryable session failure")

    events: list[str] = []
    manager = SessionManager[int]()
    server = _Server(events)
    connection = _Connection(events)
    session = RetryableSession()
    key = id(connection)
    await manager.add(key, session)  # type: ignore[arg-type]
    runtime = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="test-websocket-runtime",
        session_factory=lambda _connection: session,
    )
    runtime._sessions[key] = session
    runtime._connections[key] = connection

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(
            RuntimeError,
            match="WebSocket session shutdown retained 1 session",
        ),
    ):
        await runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=1.0)

    assert "WebSocket session shutdown failed to stop 1 of 1 session" in caplog.text
    assert "retryable session failure" in caplog.text
    assert manager.get(key) is session
    assert runtime._sessions == {key: session}
    assert runtime._connections == {key: connection}

    session.fail_stop = False
    await runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=1.0)

    assert manager.get(key) is None
    assert runtime._sessions == {}
    assert runtime._connections == {}


async def test_runtime_surfaces_failed_connection_close_and_retains_ledger_for_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class RetryableConnection(_Connection):
        def __init__(self, events: list[str]) -> None:
            super().__init__(events)
            self.fail_close = True

        async def close(self, *, code: int, reason: str) -> None:
            if self.fail_close:
                raise RuntimeError("retryable connection failure")
            await super().close(code=code, reason=reason)

    events: list[str] = []
    manager = SessionManager[int]()
    server = _Server(events)
    connection = RetryableConnection(events)
    key = id(connection)
    runtime = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="test-websocket-runtime",
        session_factory=lambda _connection: None,
    )
    runtime._connections[key] = connection

    with (
        caplog.at_level(logging.ERROR, logger="easycat.server.transports"),
        pytest.raises(
            RuntimeError,
            match="WebSocket connection shutdown retained 1 connection",
        ),
    ):
        await runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=1.0)

    assert "WebSocket connection close task" in caplog.text
    assert "retryable connection failure" in caplog.text
    assert runtime._connections == {key: connection}

    connection.fail_close = False
    await runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=1.0)

    assert runtime._connections == {}


async def test_cancel_handler_tasks_reports_cleanup_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = asyncio.Event()

    async def handler() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            raise RuntimeError("handler cleanup failed") from exc

    task = asyncio.create_task(handler(), name="easycat-test-websocket-handler")
    await started.wait()

    with caplog.at_level(logging.ERROR, logger="easycat.server.transports"):
        await cancel_handler_tasks([task], timeout_s=1.0)

    assert "WebSocket handler task easycat-test-websocket-handler failed" in caplog.text
    assert "handler cleanup failed" in caplog.text


async def test_cancelled_drain_preserves_connection_bookkeeping_for_retry() -> None:
    events: list[str] = []
    manager = _Manager(events)
    server = _Server(events)
    connection = _Connection(events)
    session = _Session(events)
    runtime = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="test-websocket-runtime",
        session_factory=lambda _connection: session,
    )
    key = id(connection)
    runtime._sessions[key] = session
    runtime._connections[key] = connection
    manager.sessions[key] = session
    drain_started = asyncio.Event()

    async def block_gate_drain(*_args: object, **_kwargs: object) -> float:
        drain_started.set()
        await asyncio.Event().wait()
        return 0.0

    runtime.gate.drain = block_gate_drain  # type: ignore[method-assign]
    draining = asyncio.create_task(runtime.drain(server, drain_timeout_s=1.0, force_timeout_s=1.0))
    await drain_started.wait()
    draining.cancel()
    with pytest.raises(asyncio.CancelledError):
        await draining

    assert runtime._sessions == {key: session}
    assert runtime._connections == {key: connection}

    async def finish_gate_drain(*_args: object, **_kwargs: object) -> float:
        return asyncio.get_running_loop().time() + 1.0

    runtime.gate.drain = finish_gate_drain  # type: ignore[method-assign]
    session.allow_graceful.set()
    await runtime.drain(server, drain_timeout_s=1.0, force_timeout_s=1.0)

    assert connection.close_calls == [(1001, "Server shutdown after drain")]
    assert runtime._sessions == {}
    assert runtime._connections == {}


async def test_runtime_allows_async_preflight_to_reject_before_session_creation() -> None:
    events: list[str] = []
    manager = _Manager(events)
    connection = _Connection(events)

    async def reject_after_preflight(_connection: object) -> None:
        events.append("preflight_rejected")

    runtime = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="test-websocket-runtime",
        session_factory=reject_after_preflight,
    )

    await runtime.handle(connection)

    assert events == ["preflight_rejected"]
    assert manager.sessions == {}
    assert runtime.gate.reserved_count == 0


async def test_drain_closes_connection_while_async_preflight_is_pending() -> None:
    """A preflight handler cancellation must not leave an accepted socket open."""

    events: list[str] = []
    manager = _Manager(events)
    server = _Server(events)
    connection = _Connection(events)
    preflight_started = asyncio.Event()
    never_finish = asyncio.Event()

    async def slow_preflight(_connection: object) -> _Session:
        preflight_started.set()
        await never_finish.wait()
        return _Session(events)

    runtime = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="test-websocket-runtime",
        session_factory=slow_preflight,
    )
    handler = asyncio.create_task(runtime.handle(connection))
    await asyncio.wait_for(preflight_started.wait(), timeout=1)

    await asyncio.wait_for(
        runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=1.0), timeout=2
    )
    with contextlib.suppress(asyncio.CancelledError):
        await handler

    assert connection.close_calls == [(1001, "Server shutdown after drain")]
    assert runtime.gate.reserved_count == 0


async def test_drain_cancels_startup_before_session_becomes_active() -> None:
    events: list[str] = []
    manager = SessionManager[int]()
    server = _Server(events)
    connection = _Connection(events)

    class _SlowStartingSession:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.rollback_started = asyncio.Event()
            self.allow_rollback = asyncio.Event()
            self.starting = False
            self.stop_during_start = False

        async def start(self) -> None:
            self.starting = True
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.rollback_started.set()
                await self.allow_rollback.wait()
                self.starting = False
                raise

        async def stop(self, *, force: bool = False) -> None:
            self.stop_during_start = self.stop_during_start or self.starting

    session = _SlowStartingSession()
    runtime = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="test-websocket-runtime",
        session_factory=lambda _connection: session,
    )
    handler = asyncio.create_task(runtime.handle(connection))
    await asyncio.wait_for(session.started.wait(), timeout=1)

    drain = asyncio.create_task(runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=1.0))
    await asyncio.wait_for(session.rollback_started.wait(), timeout=1)

    assert runtime.gate.active_count == 0
    assert session.stop_during_start is False

    session.allow_rollback.set()
    await asyncio.wait_for(drain, timeout=2)
    with contextlib.suppress(asyncio.CancelledError):
        await handler

    assert manager.get(id(connection)) is None
    assert runtime.gate.reserved_count == 0


async def test_bounded_cleanup_keeps_hard_deadline_for_cancellation_resistant_work() -> None:
    release = asyncio.Event()
    finished = asyncio.Event()

    async def _resist_cancellation() -> None:
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        finally:
            finished.set()

    loop = asyncio.get_running_loop()
    started = loop.time()
    runtime = WebSocketSessionRuntime(
        manager=_Manager([]),
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="bounded-cleanup-runtime",
        session_factory=lambda _connection: None,
    )

    await runtime._bounded_cleanup(
        _resist_cancellation(),
        timeout_s=0.01,
        label="test cleanup",
    )

    assert loop.time() - started < 0.2
    assert not finished.is_set()
    owned = runtime._cleanup_task_scope.tasks()
    assert len(owned) == 1
    assert owned[0].get_name() == "easycat-websocket-runtime-cleanup"
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    await asyncio.gather(*runtime._cleanup_task_scope.tasks())
    await asyncio.sleep(0)
    await runtime._cleanup_task_scope.release_standalone_if_empty()
    assert runtime._cleanup_task_scope.tasks() == ()


async def test_force_timeout_is_shared_across_all_runtime_cleanup_steps() -> None:
    release = asyncio.Event()

    async def resist_cancellation() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    class _StuckServer:
        def close(self, close_connections: bool = True) -> None:
            pass

        async def wait_closed(self) -> None:
            await resist_cancellation()

    class _StuckConnection:
        async def close(self, *, code: int, reason: str) -> None:
            await resist_cancellation()

    class _StuckManager:
        async def stop_all(self) -> None:
            await resist_cancellation()

    runtime = WebSocketSessionRuntime(
        manager=_StuckManager(),
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="test-websocket-runtime",
        session_factory=lambda _connection: None,
    )
    connection = _StuckConnection()
    runtime._connections[1] = connection
    handler = asyncio.create_task(resist_cancellation())
    runtime._handler_tasks.add(handler)
    await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    started = loop.time()
    server = _StuckServer()
    await runtime.drain(
        server,
        drain_timeout_s=0.0,
        force_timeout_s=0.05,
    )
    elapsed = loop.time() - started

    assert elapsed < 0.15
    close_tasks = runtime._connection_close_task_scope.tasks()
    assert len(close_tasks) == 1
    assert close_tasks[0].get_name().startswith("easycat-websocket-close-")
    assert runtime._connection_cleanup_retry == {1: connection}

    release.set()
    await asyncio.gather(
        handler,
        *close_tasks,
        *runtime._cleanup_task_scope.tasks(),
        *runtime.survivor_registry.supervisor.tasks(),
        return_exceptions=True,
    )
    await asyncio.sleep(0)
    assert runtime._connection_close_task_scope.tasks() == ()
    await runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=1.0)
    assert runtime._connection_cleanup_retry == {}


class _CancellationResistantServer:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    def close(self, close_connections: bool = True) -> None:
        pass

    async def wait_closed(self) -> None:
        self.calls += 1
        self.started.set()
        try:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancel_seen.set()
        finally:
            self.finished.set()


async def test_owned_listener_timeout_is_anchored_attributed_and_retryable() -> None:
    records: list[tuple[str, dict[str, object]]] = []
    supervisor = RuntimeSupervisor(
        capacity=1,
        journal=lambda event, data: records.append((event, dict(data))),
    )
    events: list[str] = []
    runtime = WebSocketSessionRuntime(
        manager=_Manager(events),
        max_sessions=1,
        runtime_supervisor=supervisor,
        runtime_id="owned-listener-runtime",
        session_factory=lambda _connection: None,
    )
    server = _CancellationResistantServer()

    await runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=0.01)

    await asyncio.wait_for(server.cancel_seen.wait(), timeout=1)
    assert runtime.listener_cleanup_state is OwnerState.CLOSED_WITH_SURVIVORS
    assert runtime.listener_cleanup_metadata[0].root_id == "owned-listener-runtime"
    assert runtime.listener_cleanup_metadata[0].task_name == "websocket.listener_wait_closed"
    assert any(
        event == "owned_task_transition"
        and data["root_id"] == "owned-listener-runtime"
        and data["state"] == "parked"
        for event, data in records
    )

    server.release.set()
    await asyncio.wait_for(server.finished.wait(), timeout=1)
    await asyncio.sleep(0)
    await runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=1.0)

    assert server.calls == 1
    assert runtime.listener_cleanup_state is OwnerState.CLOSED
    assert runtime.listener_cleanup_metadata == ()
    assert supervisor.active_count == 0


async def test_external_drain_cancellation_parks_listener_before_reraise() -> None:
    supervisor = RuntimeSupervisor(capacity=1)
    runtime = WebSocketSessionRuntime(
        manager=_Manager([]),
        max_sessions=1,
        runtime_supervisor=supervisor,
        runtime_id="cancelled-listener-runtime",
        session_factory=lambda _connection: None,
    )
    server = _CancellationResistantServer()
    draining = asyncio.create_task(
        runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=60.0)
    )
    await server.started.wait()

    draining.cancel()
    with pytest.raises(asyncio.CancelledError):
        await draining

    await asyncio.wait_for(server.cancel_seen.wait(), timeout=1)
    assert runtime.listener_cleanup_state is OwnerState.CLOSED_WITH_SURVIVORS
    assert supervisor.survivor_count == 1

    server.release.set()
    await asyncio.wait_for(server.finished.wait(), timeout=1)
    await runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=1.0)
    assert server.calls == 1
    assert runtime.listener_cleanup_state is OwnerState.CLOSED


async def test_runtime_owner_drop_leaves_listener_task_supervisor_anchored() -> None:
    supervisor = RuntimeSupervisor(capacity=1)
    runtime = WebSocketSessionRuntime(
        manager=_Manager([]),
        max_sessions=1,
        runtime_supervisor=supervisor,
        runtime_id="dropped-listener-runtime",
        session_factory=lambda _connection: None,
    )
    runtime_ref = weakref.ref(runtime)
    server = _CancellationResistantServer()
    await runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=0.01)
    assert supervisor.survivor_count == 1

    del runtime
    gc.collect()

    assert runtime_ref() is None
    assert len(supervisor.tasks()) == 1
    server.release.set()
    await asyncio.wait_for(server.finished.wait(), timeout=1)
    await asyncio.sleep(0)
    assert supervisor.active_count == 0


@pytest.mark.parametrize("quota", ["root", "runtime"])
async def test_listener_factory_is_not_invoked_when_survivor_quota_is_full(
    quota: str,
) -> None:
    supervisor = RuntimeSupervisor(capacity=2 if quota == "root" else 1)
    runtime = WebSocketSessionRuntime(
        manager=_Manager([]),
        max_sessions=1,
        runtime_supervisor=supervisor,
        runtime_id=f"{quota}-quota-runtime",
        survivor_capacity=1 if quota == "root" else 2,
        session_factory=lambda _connection: None,
    )
    blocker_registry = (
        runtime.survivor_registry
        if quota == "root"
        else SurvivorRegistry(supervisor=supervisor, root_id="other-root", capacity=1)
    )
    release = asyncio.Event()
    blocker = await start_owned(
        release.wait,
        registry=blocker_registry,
        owner_id="quota-blocker",
        task_name="quota.blocker",
    )
    events: list[str] = []
    server = _Server(events)

    with pytest.raises(SurvivorCapacityError) as exc_info:
        await runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=1.0)

    assert exc_info.value.quota == quota
    assert "listener_wait_closed" not in events
    error = await reap(blocker)
    assert isinstance(error, asyncio.CancelledError)


async def test_owned_listener_preserves_cleanup_exception_policy() -> None:
    class _FailingWaitServer(_Server):
        async def wait_closed(self) -> None:
            self.events.append("listener_wait_closed")
            raise RuntimeError("listener wait failed")

    events: list[str] = []
    runtime = WebSocketSessionRuntime(
        manager=_Manager(events),
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="failing-listener-runtime",
        session_factory=lambda _connection: None,
    )

    with pytest.raises(RuntimeError, match="listener wait failed"):
        await runtime.drain(
            _FailingWaitServer(events),
            drain_timeout_s=0.0,
            force_timeout_s=1.0,
        )

    assert "manager_stop_all" not in events
    assert runtime.listener_cleanup_state is OwnerState.CLOSED


async def test_cooperative_hard_timeout_retries_without_raising_expected_cancel() -> None:
    class _CooperativeServer:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()

        def close(self, close_connections: bool = True) -> None:
            pass

        async def wait_closed(self) -> None:
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await asyncio.Event().wait()

    runtime = WebSocketSessionRuntime(
        manager=_Manager([]),
        max_sessions=1,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="cooperative-listener-runtime",
        session_factory=lambda _connection: None,
    )
    server = _CooperativeServer()

    await runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=0.01)
    await server.started.wait()
    await asyncio.sleep(0)
    await runtime.drain(server, drain_timeout_s=0.0, force_timeout_s=1.0)

    assert server.calls == 2
    assert runtime.listener_cleanup_state is OwnerState.CLOSED
