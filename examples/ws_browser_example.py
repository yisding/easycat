"""Web-based voice chat with an EasyCat agent over WebSocket.

The browser captures microphone audio via getUserMedia (WebAudio API),
sends PCM16 frames over WebSocket, and plays back the agent's audio.

Browsers typically capture at 48 kHz while the pipeline expects 16 kHz.
The WebSocket transport handles this automatically: inbound audio is
resampled to the configured rate, and an ``audio_format`` control message
is sent to the client whenever the outbound TTS rate changes.

**Serving the HTML client is your responsibility.**  This example uses
Python's built-in ``http.server`` to serve ``ws_browser_client.html`` on
a separate port.  In production you would serve it from your own web
framework (FastAPI, Django, etc.) or a CDN.

Setup:
    export OPENAI_API_KEY="..."
    uv sync --extra openai-agents
    uv run python examples/ws_browser_example.py

Then open http://localhost:8080/ws_browser_client.html in your browser.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from easycat import (
    EasyConfig,
    WebSocketTransportConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)

HTTP_PORT = 8080
WS_PORT = 8765

_STATIC_DIR = str(Path(__file__).parent)


# ── HTTP server (your responsibility in production) ──────────────


def _run_http_server() -> None:
    """Serve static files from the examples directory in a background thread."""
    handler = functools.partial(SimpleHTTPRequestHandler, directory=_STATIC_DIR)
    httpd = HTTPServer(("0.0.0.0", HTTP_PORT), handler)
    httpd.serve_forever()


# ── Main ─────────────────────────────────────────────────────────


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(
        name="assistant",
        instructions="You are a helpful voice assistant. Keep responses concise.",
    )

    config = EasyConfig(
        openai_api_key=api_key,
        transport=WebSocketTransportConfig(port=WS_PORT),
        agent=agent,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    # Serve the HTML client — in production, use your own web framework.
    http_thread = threading.Thread(target=_run_http_server, daemon=True)
    http_thread.start()
    print(f"Open http://localhost:{HTTP_PORT}/ws_browser_client.html in your browser")

    await session.start()

    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
