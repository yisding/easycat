from __future__ import annotations

import asyncio

import pytest
import websockets

from easycat.transports.websocket import (
    WebSocketSessionServerConfig,
    serve_websocket_sessions,
)

from .conftest import find_free_port


class _FakeSession:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def stop(self) -> None:
        self.stopped.set()


async def _connect_with_retry(uri: str):
    last: OSError | None = None
    for _ in range(20):
        try:
            return await websockets.connect(uri)
        except OSError as exc:
            last = exc
            await asyncio.sleep(0.05)
    assert last is not None
    raise last


@pytest.mark.asyncio
async def test_serve_websocket_sessions_manages_session_lifecycle() -> None:
    port = find_free_port()
    stop_event = asyncio.Event()
    sessions: list[_FakeSession] = []

    def session_factory(_ws) -> _FakeSession:
        session = _FakeSession()
        sessions.append(session)
        return session

    task = asyncio.create_task(
        serve_websocket_sessions(
            session_factory,
            WebSocketSessionServerConfig(port=port),
            stop_event=stop_event,
            runtime_feedback=False,
            announce=False,
        )
    )
    try:
        ws = await _connect_with_retry(f"ws://127.0.0.1:{port}")
        async with ws:
            assert sessions
            await asyncio.wait_for(sessions[0].started.wait(), timeout=1)
        await asyncio.wait_for(sessions[0].stopped.wait(), timeout=1)
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)
