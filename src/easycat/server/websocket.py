"""Standalone multi-session WebSocket server orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from easycat._concurrency import RuntimeSupervisor
from easycat._net import normalize_auth_token
from easycat._signals import create_shutdown_event
from easycat.server.auth import BearerTokenAuth, authorized_bind, from_websocket
from easycat.server.transports import WebSocketSessionRuntime
from easycat.session import Session
from easycat.session_manager import SessionManager
from easycat.transports._limits import MAX_WEBSOCKET_MESSAGE_BYTES
from easycat.transports.websocket import (
    WebSocketConnectionTransport,
    WebSocketSessionServerConfig,
    WebSocketTransportConfig,
    websocket_session_server_config_from_env,
)


def _plain_response(status: HTTPStatus, body: str) -> Response:
    payload = body.encode()
    return Response(
        status.value,
        status.phrase,
        Headers(
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(payload))),
            ]
        ),
        payload,
    )


async def serve_websocket_sessions(
    session_factory: Callable[[ServerConnection], Session],
    config: WebSocketSessionServerConfig | None = None,
    *,
    stop_event: asyncio.Event | None = None,
    runtime_feedback: bool = True,
    announce: bool = True,
    unsafe_allow_no_auth: bool = False,
    allow_query_token: bool = False,
) -> None:
    """Serve one EasyCat session per accepted WebSocket connection."""
    settings = config or WebSocketSessionServerConfig()
    auth_token = normalize_auth_token(settings.auth_token)
    auth_policy = (
        BearerTokenAuth(token=auth_token, allow_query_token=allow_query_token)
        if auth_token is not None
        else None
    )
    manager: SessionManager[int] = SessionManager()
    runtime = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=settings.max_sessions,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="standalone-websocket-server",
        session_factory=session_factory,
        runtime_feedback=runtime_feedback,
    )

    def process_request(_ws: ServerConnection, request: Request) -> Response | None:
        if auth_policy is not None:
            result = auth_policy.authorize(from_websocket(request.headers, request.path))
            if not result.allowed:
                return _plain_response(
                    HTTPStatus.UNAUTHORIZED, "Missing or invalid bearer token.\n"
                )
        return None

    server = await authorized_bind(
        settings.host,
        auth=auth_policy,
        unsafe_allow_no_auth=unsafe_allow_no_auth,
        binder=lambda bind_host: websockets.serve(
            runtime.handle,
            bind_host,
            settings.port,
            process_request=process_request,
            compression=None,
            max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
        ),
    )
    if announce:
        print(f"\nServer ready. Connect WebSocket clients to ws://{settings.host}:{settings.port}")
        print("Press Ctrl+C to stop.\n")

    event = stop_event or create_shutdown_event()
    try:
        await event.wait()
    finally:
        await runtime.drain(
            server,
            drain_timeout_s=settings.drain_timeout_s,
            force_timeout_s=settings.force_shutdown_timeout_s,
        )


async def serve_websocket_config_sessions(
    config_factory: Callable[[WebSocketConnectionTransport], Any],
    config: WebSocketSessionServerConfig | None = None,
    *,
    transport_config: WebSocketTransportConfig | None = None,
    stop_event: asyncio.Event | None = None,
    runtime_feedback: bool = True,
    announce: bool = True,
    unsafe_allow_no_auth: bool = False,
    allow_query_token: bool = False,
) -> None:
    """Serve one session per connection using an app-config factory."""
    from easycat.config import create_session

    def session_factory(ws: ServerConnection) -> Session:
        transport = WebSocketConnectionTransport(ws, transport_config)
        return create_session(config_factory(transport))

    await serve_websocket_sessions(
        session_factory,
        config,
        stop_event=stop_event,
        runtime_feedback=runtime_feedback,
        announce=announce,
        unsafe_allow_no_auth=unsafe_allow_no_auth,
        allow_query_token=allow_query_token,
    )


def run_websocket_config_server(
    config_factory: Callable[[WebSocketConnectionTransport], Any],
    config: WebSocketSessionServerConfig | None = None,
    *,
    transport_config: WebSocketTransportConfig | None = None,
    runtime_feedback: bool = True,
    announce: bool = True,
    unsafe_allow_no_auth: bool = False,
    allow_query_token: bool = False,
) -> None:
    """Run a WebSocket session server using ``EASYCAT_WS_*`` defaults."""
    settings = config or websocket_session_server_config_from_env()
    asyncio.run(
        serve_websocket_config_sessions(
            config_factory,
            settings,
            transport_config=transport_config,
            runtime_feedback=runtime_feedback,
            announce=announce,
            unsafe_allow_no_auth=unsafe_allow_no_auth,
            allow_query_token=allow_query_token,
        )
    )
