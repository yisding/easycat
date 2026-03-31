"""WebSocket server example for EasyCat (multi-session).

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra openai-agents
  uv run python examples/ws_server.py

Connect multiple clients streaming raw PCM16 audio to ws://localhost:8765.
Each connection gets its own EasyCat Session.
"""

from __future__ import annotations

import asyncio
import signal

import websockets
from websockets.asyncio.server import ServerConnection

from easycat import (
    EasyCatConfig,
    SessionManager,
    WebSocketConnectionTransport,
    attach_runtime_feedback,
    build_openai_agents_adapter,
    create_session,
    default_event_logging,
    require_env,
)


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    manager: SessionManager[int] = SessionManager()

    async def handle_connection(ws: ServerConnection) -> None:
        transport = WebSocketConnectionTransport(ws)

        adapter = build_openai_agents_adapter(instructions="You are a helpful voice assistant.")
        session = create_session(
            EasyCatConfig(
                openai_api_key=api_key,
                transport=transport,
                agent=adapter,
                event_logging=default_event_logging(),
            )
        )
        attach_runtime_feedback(session)

        key = id(ws)
        async with manager.connection(key, session):
            await ws.wait_closed()

    server = await websockets.serve(handle_connection, "0.0.0.0", 8765)
    print("\nServer ready. Connect WebSocket clients to ws://localhost:8765")
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
