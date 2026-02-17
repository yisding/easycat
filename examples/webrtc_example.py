"""Web-based voice chat with an EasyCat agent.

Serves a web page and runs an EasyCat voice session over WebSocket.
The browser captures microphone audio via getUserMedia (WebRTC API),
sends PCM16 frames over WebSocket, and plays back the agent's audio.

Setup:
    export OPENAI_API_KEY="..."
    uv add easycat[openai-agents]
    uv run python examples/webrtc_example.py

Then open http://localhost:8080/webrtc_example.html in your browser.
"""

from __future__ import annotations

import asyncio
import functools
import os
import signal
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from easycat import (
    EasyCatConfig,
    OpenAIAgentsAdapter,
    WebSocketTransportConfig,
    create_session,
)

HTTP_PORT = 8080
WS_PORT = 8765

_STATIC_DIR = str(Path(__file__).parent)


def _run_http_server() -> None:
    """Serve static files from the examples directory in a background thread."""
    handler = functools.partial(SimpleHTTPRequestHandler, directory=_STATIC_DIR)
    httpd = HTTPServer(("0.0.0.0", HTTP_PORT), handler)
    httpd.serve_forever()


async def main() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required.")

    try:
        from agents import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "OpenAI Agents SDK is required. Install with: uv add easycat[openai-agents]"
        ) from exc

    voice_agent = Agent(
        name="VoiceAssistant",
        instructions="You are a helpful voice assistant. Keep responses concise.",
    )
    adapter = OpenAIAgentsAdapter(voice_agent)

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=WebSocketTransportConfig(port=WS_PORT),
        agent=adapter,
        wrap_agent=False,
    )
    session = create_session(config)

    # Serve the HTML client on a background thread.
    http_thread = threading.Thread(target=_run_http_server, daemon=True)
    http_thread.start()
    print(f"Open http://localhost:{HTTP_PORT}/webrtc_example.html in your browser")

    # Start the EasyCat session (launches WebSocket server on WS_PORT).
    await session.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
