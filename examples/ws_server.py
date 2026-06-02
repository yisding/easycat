"""WebSocket server example for EasyCat (multi-session).

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra openai-agents
  uv run python examples/ws_server.py

Connect clients streaming raw PCM16 audio to ws://localhost:8765.
Each connection gets its own EasyCat Session.

For non-local deployments, set EASYCAT_WS_TOKEN and send it as:
  Authorization: Bearer <token>
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from hmac import compare_digest
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from easycat import (
    EasyConfig,
    SessionManager,
    WebSocketConnectionTransport,
    attach_runtime_feedback,
    create_session,
    require_env,
)


@dataclass(frozen=True)
class WebSocketServerSettings:
    host: str
    port: int
    auth_token: str | None
    max_sessions: int


def _load_settings() -> WebSocketServerSettings:
    return WebSocketServerSettings(
        host=os.getenv("EASYCAT_WS_HOST", "127.0.0.1"),
        port=int(os.getenv("EASYCAT_WS_PORT", "8765")),
        auth_token=os.getenv("EASYCAT_WS_TOKEN"),
        max_sessions=int(os.getenv("EASYCAT_WS_MAX_SESSIONS", "10")),
    )


def _authorized(headers: Headers, path: str, token: str | None) -> bool:
    if token is None:
        return True
    value = headers.get("Authorization")
    if value is not None:
        scheme, separator, credential = value.partition(" ")
        if separator == " " and scheme.lower() == "bearer":
            return compare_digest(credential, token)

    query_token = parse_qs(urlsplit(path).query).get("token", [None])[0]
    return query_token is not None and compare_digest(query_token, token)


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


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    settings = _load_settings()
    from agents import Agent  # type: ignore[import-untyped]

    manager: SessionManager[int] = SessionManager()
    session_slots = asyncio.Semaphore(settings.max_sessions)

    def process_request(_ws: ServerConnection, request: Request) -> Response | None:
        if not _authorized(request.headers, request.path, settings.auth_token):
            return _plain_response(HTTPStatus.UNAUTHORIZED, "Missing or invalid bearer token.\n")
        return None

    async def handle_connection(ws: ServerConnection) -> None:
        if session_slots.locked():
            await ws.close(code=1013, reason="Server is at the configured session limit")
            return

        async with session_slots:
            transport = WebSocketConnectionTransport(ws)

            agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
            session = create_session(
                EasyConfig(
                    openai_api_key=api_key,
                    transport=transport,
                    agent=agent,
                )
            )
            attach_runtime_feedback(session)

            key = id(ws)
            async with manager.connection(key, session):
                await ws.wait_closed()

    server = await websockets.serve(
        handle_connection,
        settings.host,
        settings.port,
        process_request=process_request,
    )
    print(f"\nServer ready. Connect WebSocket clients to ws://{settings.host}:{settings.port}")
    print("Press Ctrl+C to stop.\n")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    server.close()
    await server.wait_closed()
    await manager.stop_all()


if __name__ == "__main__":
    asyncio.run(main())
