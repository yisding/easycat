"""WebSocket voice chat with passive supervisor listen-only sockets.

This example keeps EasyCat's existing "one session per caller connection"
model.  A second WebSocket endpoint fans session audio out to passive
supervisors that subscribe by ``session_id``.

By default, the supervisor endpoint is unauthenticated for local demos. Set
``EASYCAT_SUPERVISOR_TOKEN`` to require that token in supervisor subscribe
messages before exposing this example beyond localhost.

Setup:
    export OPENAI_API_KEY="..."
    export EASYCAT_SUPERVISOR_TOKEN="..."  # optional but recommended
    uv sync --extra openai --extra openai-agents --group dev
    uv run easycat doctor
    uv run easycat doctor --env-file .env  # if keys live in .env
    uv run easycat doctor --env-file .env --json  # for parseable checks
    uv run python examples/ws_supervisor_server.py
    uv run --env-file .env python examples/ws_supervisor_server.py  # if keys live in .env

Open:
    Caller UI:     http://localhost:8080/ws_browser_client.html
    Supervisor UI: http://localhost:8080/ws_supervisor_client.html
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import websockets
from websockets.asyncio.server import ServerConnection

from easycat import (
    EasyConfig,
    SessionAudioBroadcaster,
    SessionManager,
    WebSocketConnectionTransport,
    create_session,
    require_env,
)
from easycat.helpers import create_shutdown_event
from easycat.supervisor import (
    SUPERVISOR_TOKEN_ENV,
    serve_supervisor_websocket,
    supervisor_auth_token_from_env,
)

logger = logging.getLogger(__name__)

HTTP_PORT = 8080
CALLER_WS_PORT = 8765
SUPERVISOR_WS_PORT = 8766
_STATIC_DIR = str(Path(__file__).parent)


def _run_http_server() -> None:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=_STATIC_DIR)
    httpd = HTTPServer(("0.0.0.0", HTTP_PORT), handler)
    httpd.serve_forever()


async def main() -> None:
    require_env("OPENAI_API_KEY")
    from agents import Agent  # type: ignore[import-untyped]

    manager: SessionManager[str] = SessionManager()
    broadcasters: dict[str, SessionAudioBroadcaster] = {}
    supervisor_token = supervisor_auth_token_from_env()
    if supervisor_token is None:
        logger.warning(
            "Supervisor endpoint is unauthenticated; set %s before exposing it.",
            SUPERVISOR_TOKEN_ENV,
        )
    else:
        logger.info("Supervisor token auth is enabled.")

    http_thread = threading.Thread(target=_run_http_server, daemon=True)
    http_thread.start()

    async def handle_caller(ws: ServerConnection) -> None:
        agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
        transport = WebSocketConnectionTransport(ws)
        session = create_session(
            EasyConfig(
                transport=transport,
                agent=agent,
            )
        )

        broadcaster = SessionAudioBroadcaster(session)
        session_id = session.session_id
        broadcasters[session_id] = broadcaster
        logger.info("Caller session created: %s", session_id)

        try:
            async with manager.connection(session_id, session, runtime_feedback=True):
                await ws.send(
                    json.dumps(
                        {
                            "type": "session",
                            "session_id": session_id,
                            "supervisor_ws_url": f"ws://localhost:{SUPERVISOR_WS_PORT}",
                        }
                    )
                )
                await ws.wait_closed()
        finally:
            broadcasters.pop(session_id, None)
            broadcaster.close()
            logger.info("Caller session closed: %s", session_id)

    async def handle_supervisor(ws: ServerConnection) -> None:
        await serve_supervisor_websocket(
            ws,
            broadcasters,
            expected_token=supervisor_token,
        )

    caller_server = await websockets.serve(handle_caller, "0.0.0.0", CALLER_WS_PORT)
    supervisor_server = await websockets.serve(
        handle_supervisor,
        "0.0.0.0",
        SUPERVISOR_WS_PORT,
    )

    print(f"Caller UI:     http://localhost:{HTTP_PORT}/ws_browser_client.html")
    print(f"Supervisor UI: http://localhost:{HTTP_PORT}/ws_supervisor_client.html")
    print(f"Caller WS:     ws://localhost:{CALLER_WS_PORT}")
    print(f"Supervisor WS: ws://localhost:{SUPERVISOR_WS_PORT}")
    print("Press Ctrl+C to stop.")

    stop_event = create_shutdown_event()

    try:
        await stop_event.wait()
    finally:
        caller_server.close()
        supervisor_server.close()
        await caller_server.wait_closed()
        await supervisor_server.wait_closed()
        for broadcaster in list(broadcasters.values()):
            broadcaster.close()
        broadcasters.clear()
        await manager.stop_all()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(main())
