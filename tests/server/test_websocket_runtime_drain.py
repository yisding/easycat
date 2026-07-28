"""Shutdown-order tests for the shared raw-WebSocket session runtime."""

from __future__ import annotations

import asyncio

from easycat.server.transports import WebSocketSessionRuntime


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


async def test_runtime_rejects_new_connection_after_drain_starts() -> None:
    events: list[str] = []
    manager = _Manager(events)
    server = _Server(events)
    runtime = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=1,
        session_factory=lambda _connection: _Session(events),
    )
    runtime.start_draining(server)
    connection = _Connection(events)

    await runtime.handle(connection)

    assert connection.close_calls == [(1013, "Server is draining")]
    assert manager.sessions == {}
    assert server.close_calls == [False]
