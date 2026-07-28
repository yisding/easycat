from __future__ import annotations

import asyncio
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import pytest
import websockets

import easycat.server.websocket as websocket_module
from easycat.server.websocket import (
    run_websocket_config_server,
    serve_websocket_config_sessions,
    serve_websocket_sessions,
)
from easycat.transports.websocket import (
    WebSocketConnectionTransport,
    WebSocketSessionServerConfig,
    WebSocketTransportConfig,
)


class _FakeSession:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def stop(self, *, force: bool = False) -> None:
        self.stopped.set()


def _patch_serve_started(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    server_started = asyncio.Event()
    original_serve = websocket_module.websockets.serve

    async def serve_and_signal(*args: Any, **kwargs: Any) -> Any:
        server = await original_serve(*args, **kwargs)
        server_started.set()
        return server

    monkeypatch.setattr(websocket_module.websockets, "serve", serve_and_signal)
    return server_started


@pytest.mark.asyncio
async def test_serve_websocket_sessions_disables_compression(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []

    class FakeServer:
        def close(self, close_connections: bool = True) -> None:
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
@pytest.mark.integration_socket
async def test_serve_websocket_sessions_manages_session_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port_factory: Callable[[], int],
) -> None:
    port = unused_tcp_port_factory()
    stop_event = asyncio.Event()
    sessions: list[_FakeSession] = []

    def session_factory(_ws) -> _FakeSession:
        session = _FakeSession()
        sessions.append(session)
        return session

    server_started = _patch_serve_started(monkeypatch)
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
        await asyncio.wait_for(server_started.wait(), timeout=1)
        async with websockets.connect(f"ws://127.0.0.1:{port}"):
            assert sessions
            await asyncio.wait_for(sessions[0].started.wait(), timeout=1)
        await asyncio.wait_for(sessions[0].stopped.wait(), timeout=1)
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
@pytest.mark.integration_socket
async def test_serve_websocket_sessions_rejects_unauthorized_before_session_creation(
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port_factory: Callable[[], int],
) -> None:
    port = unused_tcp_port_factory()
    stop_event = asyncio.Event()
    sessions: list[_FakeSession] = []

    def session_factory(_ws) -> _FakeSession:
        session = _FakeSession()
        sessions.append(session)
        return session

    server_started = _patch_serve_started(monkeypatch)
    task = asyncio.create_task(
        serve_websocket_sessions(
            session_factory,
            WebSocketSessionServerConfig(port=port, auth_token="secret-token"),
            stop_event=stop_event,
            runtime_feedback=False,
            announce=False,
        )
    )
    try:
        await asyncio.wait_for(server_started.wait(), timeout=1)
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc_info:
            async with websockets.connect(f"ws://127.0.0.1:{port}"):
                pass

        assert exc_info.value.response.status_code == HTTPStatus.UNAUTHORIZED
        assert sessions == []
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
@pytest.mark.integration_socket
async def test_serve_websocket_sessions_accepts_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port_factory: Callable[[], int],
) -> None:
    port = unused_tcp_port_factory()
    stop_event = asyncio.Event()
    sessions: list[_FakeSession] = []

    def session_factory(_ws) -> _FakeSession:
        session = _FakeSession()
        sessions.append(session)
        return session

    server_started = _patch_serve_started(monkeypatch)
    task = asyncio.create_task(
        serve_websocket_sessions(
            session_factory,
            WebSocketSessionServerConfig(port=port, auth_token="secret-token"),
            stop_event=stop_event,
            runtime_feedback=False,
            announce=False,
        )
    )
    try:
        await asyncio.wait_for(server_started.wait(), timeout=1)
        async with websockets.connect(
            f"ws://127.0.0.1:{port}",
            additional_headers={"Authorization": "Bearer secret-token"},
        ):
            assert sessions
            await asyncio.wait_for(sessions[0].started.wait(), timeout=1)
        await asyncio.wait_for(sessions[0].stopped.wait(), timeout=1)
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
@pytest.mark.integration_socket
async def test_serve_websocket_sessions_accepts_query_token_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port_factory: Callable[[], int],
) -> None:
    # ``allow_query_token=True`` is the loopback/dev opt-in that keeps the
    # bundled browser client working (browsers cannot set handshake headers).
    port = unused_tcp_port_factory()
    stop_event = asyncio.Event()
    sessions: list[_FakeSession] = []

    def session_factory(_ws) -> _FakeSession:
        session = _FakeSession()
        sessions.append(session)
        return session

    server_started = _patch_serve_started(monkeypatch)
    task = asyncio.create_task(
        serve_websocket_sessions(
            session_factory,
            WebSocketSessionServerConfig(port=port, auth_token="secret-token"),
            stop_event=stop_event,
            runtime_feedback=False,
            announce=False,
            allow_query_token=True,
        )
    )
    try:
        await asyncio.wait_for(server_started.wait(), timeout=1)
        async with websockets.connect(f"ws://127.0.0.1:{port}/voice?token=secret-token"):
            assert sessions
            await asyncio.wait_for(sessions[0].started.wait(), timeout=1)
        await asyncio.wait_for(sessions[0].stopped.wait(), timeout=1)
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
@pytest.mark.integration_socket
async def test_serve_websocket_sessions_rejects_query_token_by_default(
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port_factory: Callable[[], int],
) -> None:
    # Default-OFF (the documented breaking change): a ``?token=`` query value is
    # NOT accepted, so the handshake is rejected with 401 even though the token
    # value is correct. Only ``Authorization: Bearer`` authenticates by default.
    port = unused_tcp_port_factory()
    stop_event = asyncio.Event()
    sessions: list[_FakeSession] = []

    def session_factory(_ws) -> _FakeSession:
        session = _FakeSession()
        sessions.append(session)
        return session

    server_started = _patch_serve_started(monkeypatch)
    task = asyncio.create_task(
        serve_websocket_sessions(
            session_factory,
            WebSocketSessionServerConfig(port=port, auth_token="secret-token"),
            stop_event=stop_event,
            runtime_feedback=False,
            announce=False,
        )
    )
    try:
        await asyncio.wait_for(server_started.wait(), timeout=1)
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
            async with websockets.connect(f"ws://127.0.0.1:{port}/voice?token=secret-token"):
                pass
        assert exc.value.response.status_code == HTTPStatus.UNAUTHORIZED
        assert sessions == []
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
@pytest.mark.integration_socket
async def test_serve_websocket_sessions_closes_extra_client_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port_factory: Callable[[], int],
) -> None:
    port = unused_tcp_port_factory()
    stop_event = asyncio.Event()
    sessions: list[_FakeSession] = []

    def session_factory(_ws) -> _FakeSession:
        session = _FakeSession()
        sessions.append(session)
        return session

    server_started = _patch_serve_started(monkeypatch)
    task = asyncio.create_task(
        serve_websocket_sessions(
            session_factory,
            WebSocketSessionServerConfig(port=port, max_sessions=1),
            stop_event=stop_event,
            runtime_feedback=False,
            announce=False,
        )
    )
    try:
        await asyncio.wait_for(server_started.wait(), timeout=1)
        async with websockets.connect(f"ws://127.0.0.1:{port}"):
            assert sessions
            await asyncio.wait_for(sessions[0].started.wait(), timeout=1)

            async with websockets.connect(f"ws://127.0.0.1:{port}") as extra_client:
                await asyncio.wait_for(extra_client.wait_closed(), timeout=1)
                assert extra_client.close_code == 1013
                assert extra_client.close_reason == "Server is at the configured session limit"

            assert len(sessions) == 1
        await asyncio.wait_for(sessions[0].stopped.wait(), timeout=1)
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
@pytest.mark.integration_socket
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

    server_started = _patch_serve_started(monkeypatch)
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
        await asyncio.wait_for(server_started.wait(), timeout=1)
        async with websockets.connect(f"ws://127.0.0.1:{port}"):
            assert sessions
            await asyncio.wait_for(sessions[0].started.wait(), timeout=1)
            assert len(configs) == 1
            assert configs[0]["transport"] is transports[0]
            assert "agent" in configs[0]
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_serve_websocket_sessions_non_loopback_requires_token() -> None:
    """A non-loopback bind without a token raises before opening a socket."""
    with pytest.raises(ValueError) as exc:
        await serve_websocket_sessions(
            lambda _ws: _FakeSession(),
            WebSocketSessionServerConfig(host="0.0.0.0", auth_token=None),
            runtime_feedback=False,
            announce=False,
        )
    message = str(exc.value)
    assert "0.0.0.0" in message
    assert "unsafe_allow_no_auth" in message


@pytest.mark.asyncio
async def test_serve_websocket_config_sessions_non_loopback_requires_token() -> None:
    """The config-factory serve helper enforces the same non-loopback guard."""
    with pytest.raises(ValueError) as exc:
        await serve_websocket_config_sessions(
            lambda _t: {"agent": object()},
            WebSocketSessionServerConfig(host="0.0.0.0", auth_token=None),
            runtime_feedback=False,
            announce=False,
        )
    assert "0.0.0.0" in str(exc.value)


@pytest.mark.asyncio
async def test_serve_websocket_sessions_non_loopback_unsafe_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``unsafe_allow_no_auth=True`` allows a non-loopback unauthenticated bind."""

    class FakeServer:
        def close(self, close_connections: bool = True) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    served: list[dict[str, object]] = []

    async def fake_serve(*_args: object, **kwargs: object) -> FakeServer:
        served.append(kwargs)
        return FakeServer()

    monkeypatch.setattr(websocket_module.websockets, "serve", fake_serve)
    stop_event = asyncio.Event()
    stop_event.set()

    await serve_websocket_sessions(
        lambda _ws: _FakeSession(),
        WebSocketSessionServerConfig(host="0.0.0.0", auth_token=None),
        stop_event=stop_event,
        runtime_feedback=False,
        announce=False,
        unsafe_allow_no_auth=True,
    )

    # The guard did not fire; the server was actually started.
    assert len(served) == 1


@pytest.mark.asyncio
async def test_serve_websocket_sessions_loopback_no_token_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loopback bind without a token stays allowed (unchanged behavior)."""

    class FakeServer:
        def close(self, close_connections: bool = True) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    served: list[dict[str, object]] = []

    async def fake_serve(*_args: object, **kwargs: object) -> FakeServer:
        served.append(kwargs)
        return FakeServer()

    monkeypatch.setattr(websocket_module.websockets, "serve", fake_serve)
    stop_event = asyncio.Event()
    stop_event.set()

    await serve_websocket_sessions(
        lambda _ws: _FakeSession(),
        WebSocketSessionServerConfig(host="127.0.0.1", auth_token=None),
        stop_event=stop_event,
        runtime_feedback=False,
        announce=False,
    )

    assert len(served) == 1


def test_run_websocket_config_server_delegates_with_env_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    transport_config = WebSocketTransportConfig(max_pending_chunks=7)

    def config_factory(_transport: WebSocketConnectionTransport) -> dict[str, object]:
        return {"agent": object()}

    async def fake_serve_websocket_config_sessions(
        config_factory_arg: Callable[[WebSocketConnectionTransport], object],
        config_arg: WebSocketSessionServerConfig,
        *,
        transport_config: WebSocketTransportConfig | None = None,
        runtime_feedback: bool = True,
        announce: bool = True,
        unsafe_allow_no_auth: bool = False,
        allow_query_token: bool = False,
    ) -> None:
        calls.append(
            {
                "config_factory": config_factory_arg,
                "config": config_arg,
                "transport_config": transport_config,
                "runtime_feedback": runtime_feedback,
                "announce": announce,
                "unsafe_allow_no_auth": unsafe_allow_no_auth,
                "allow_query_token": allow_query_token,
            }
        )

    monkeypatch.setenv("EASYCAT_WS_HOST", "0.0.0.0")
    monkeypatch.setenv("EASYCAT_WS_PORT", "9876")
    monkeypatch.setenv("EASYCAT_WS_TOKEN", "env-token")
    monkeypatch.setenv("EASYCAT_WS_MAX_SESSIONS", "4")
    monkeypatch.setenv("EASYCAT_WS_DRAIN_TIMEOUT_S", "12.5")
    monkeypatch.setenv("EASYCAT_WS_FORCE_SHUTDOWN_TIMEOUT_S", "3.5")
    monkeypatch.setattr(
        websocket_module,
        "serve_websocket_config_sessions",
        fake_serve_websocket_config_sessions,
    )

    run_websocket_config_server(
        config_factory,
        transport_config=transport_config,
        runtime_feedback=False,
        announce=False,
    )

    assert calls == [
        {
            "config_factory": config_factory,
            "config": WebSocketSessionServerConfig(
                host="0.0.0.0",
                port=9876,
                auth_token="env-token",
                max_sessions=4,
                drain_timeout_s=12.5,
                force_shutdown_timeout_s=3.5,
            ),
            "transport_config": transport_config,
            "runtime_feedback": False,
            "announce": False,
            "unsafe_allow_no_auth": False,
            "allow_query_token": False,
        }
    ]
