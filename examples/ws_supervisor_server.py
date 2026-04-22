"""WebSocket voice chat with passive supervisor listen-only sockets.

This example keeps EasyCat's existing "one session per caller connection"
model.  A second WebSocket endpoint fans session audio out to passive
supervisors that subscribe by ``session_id``.

.. warning::

    This example has **no authentication**.  Any WebSocket client that
    knows (or guesses) a ``session_id`` can subscribe to a live call and
    hear both caller and assistant audio.  Before deploying anywhere
    reachable from the public internet, add auth on the supervisor
    endpoint (token in the subscribe message, signed URLs, or an HTTP
    gate in front of the WebSocket).

Setup:
    export OPENAI_API_KEY="..."
    uv sync --extra openai-agents
    uv run python examples/ws_supervisor_server.py

Open:
    Caller UI:     http://localhost:8080/ws_browser_client.html
    Supervisor UI: http://localhost:8080/ws_supervisor_client.html
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import json
import logging
import signal
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import websockets
from websockets.asyncio.server import ServerConnection

from easycat import (
    EasyCatConfig,
    SessionAudioBroadcaster,
    SessionManager,
    WebSocketConnectionTransport,
    attach_runtime_feedback,
    create_session,
    require_env,
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


def _serialize_audio_frame(frame) -> str:  # noqa: ANN001
    fmt = frame.chunk.format
    return json.dumps(
        {
            "type": "audio",
            "session_id": frame.session_id,
            "track": frame.track,
            "turn_id": frame.turn_id,
            "timestamp": frame.timestamp,
            "sample_rate": fmt.sample_rate,
            "channels": fmt.channels,
            "sample_width": fmt.sample_width,
            "encoding": fmt.encoding,
            "data": base64.b64encode(frame.chunk.data).decode("ascii"),
        }
    )


async def _close_with_error(
    ws: ServerConnection,
    *,
    code: int,
    reason: str,
    message: str,
) -> None:
    try:
        await ws.send(json.dumps({"type": "error", "message": message}))
    except websockets.exceptions.ConnectionClosed:
        return
    await ws.close(code, reason)


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    from agents import Agent  # type: ignore[import-untyped]

    manager: SessionManager[str] = SessionManager()
    broadcasters: dict[str, SessionAudioBroadcaster] = {}

    http_thread = threading.Thread(target=_run_http_server, daemon=True)
    http_thread.start()

    async def handle_caller(ws: ServerConnection) -> None:
        agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
        transport = WebSocketConnectionTransport(ws)
        session = create_session(
            EasyCatConfig(
                openai_api_key=api_key,
                transport=transport,
                agent=agent,
            )
        )
        attach_runtime_feedback(session)

        broadcaster = SessionAudioBroadcaster(session)
        session_id = session.session_id
        broadcasters[session_id] = broadcaster
        logger.info("Caller session created: %s", session_id)

        try:
            async with manager.connection(session_id, session):
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
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "role": "supervisor",
                    "message": 'Send {"type":"subscribe","session_id":"..."}',
                }
            )
        )

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        except TimeoutError:
            await _close_with_error(
                ws,
                code=4408,
                reason="Subscribe timed out",
                message="Expected subscribe message with session_id within 10 seconds.",
            )
            return

        if not isinstance(raw, str):
            await _close_with_error(
                ws,
                code=4400,
                reason="Invalid subscribe payload",
                message="Supervisor subscribe payload must be JSON text.",
            )
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await _close_with_error(
                ws,
                code=4400,
                reason="Invalid JSON",
                message="Supervisor subscribe payload must be valid JSON.",
            )
            return

        if msg.get("type") != "subscribe" or not isinstance(msg.get("session_id"), str):
            await _close_with_error(
                ws,
                code=4400,
                reason="Invalid subscribe message",
                message='Expected {"type":"subscribe","session_id":"..."}.',
            )
            return

        session_id = msg["session_id"]
        broadcaster = broadcasters.get(session_id)
        if broadcaster is None:
            await _close_with_error(
                ws,
                code=4404,
                reason="Unknown session",
                message=f"No active caller session found for {session_id}.",
            )
            return

        listener_id, queue = broadcaster.subscribe()
        logger.info("Supervisor attached to %s", session_id)

        async def _drain_inbound() -> None:
            # Supervisors are listen-only, but we still need to read from
            # the socket so a disconnect during caller silence surfaces
            # promptly instead of blocking on queue.get() forever.
            try:
                async for _ in ws:
                    pass
            except websockets.exceptions.ConnectionClosed:
                return

        recv_task = asyncio.create_task(_drain_inbound())
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "subscribed",
                        "session_id": session_id,
                        "listener_count": broadcaster.listener_count,
                    }
                )
            )
            while True:
                get_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {get_task, recv_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if recv_task in done:
                    get_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await get_task
                    break
                frame = get_task.result()
                if frame is None:
                    break
                await ws.send(_serialize_audio_frame(frame))
        except websockets.exceptions.ConnectionClosed:
            logger.info("Supervisor disconnected from %s", session_id)
        finally:
            recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recv_task
            broadcaster.unsubscribe(listener_id)

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

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

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
