from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
import websockets

import easycat.transports.websocket as websocket_module
from easycat.transports.websocket import (
    WebSocketConnectionTransport,
    WebSocketSessionServerConfig,
    WebSocketTransportConfig,
    serve_websocket_config_sessions,
    serve_websocket_sessions,
)


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
async def test_serve_websocket_sessions_disables_compression(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []

    class FakeServer:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def fake_serve(*_args: object, **kwargs: object) -> FakeServer:
        calls.append(kwargs)
        return FakeServer()

    stop_event = asyncio.Event()
    stop_event.set()
    monkeypatch.setattr(websocket_module.websockets, "serve", fake_serve)

    await serve_websocket_sessions(
        lambda _ws: _FakeSession(),
        WebSocketSessionServerConfig(port=0),
        stop_event=stop_event,
        runtime_feedback=False,
        announce=False,
    )

    assert len(calls) == 1
    assert callable(calls[0]["process_request"])
    assert calls[0]["compression"] is None


@pytest.mark.asyncio
async def test_serve_websocket_sessions_manages_session_lifecycle(
    unused_tcp_port_factory: Callable[[], int],
) -> None:
    port = unused_tcp_port_factory()
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


@pytest.mark.asyncio
async def test_serve_websocket_config_sessions_builds_connection_transport(
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port_factory: Callable[[], int],
) -> None:
    import easycat.config as config_module

    port = unused_tcp_port_factory()
    stop_event = asyncio.Event()
    sessions: list[_FakeSession] = []
    configs: list[dict[str, object]] = []
    transports: list[WebSocketConnectionTransport] = []
    transport_config = WebSocketTransportConfig(max_pending_chunks=3)

    def create_session(config: dict[str, object]) -> _FakeSession:
        configs.append(config)
        session = _FakeSession()
        sessions.append(session)
        return session

    def config_factory(transport: WebSocketConnectionTransport) -> dict[str, object]:
        transports.append(transport)
        assert transport.audio_format == transport_config.audio_format
        return {"transport": transport, "agent": object()}

    monkeypatch.setattr(config_module, "create_session", create_session)

    task = asyncio.create_task(
        serve_websocket_config_sessions(
            config_factory,
            WebSocketSessionServerConfig(port=port),
            transport_config=transport_config,
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
            assert len(configs) == 1
            assert configs[0]["transport"] is transports[0]
            assert "agent" in configs[0]
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)
