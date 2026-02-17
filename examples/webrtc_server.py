"""WebRTC voice chat server — deployable on EC2.

Serves a signaling HTTP endpoint and a static HTML client.  A browser
connects via WebRTC, sending microphone audio and receiving the agent's
TTS response as a real-time Opus stream.

Setup (local):
    export OPENAI_API_KEY="..."
    uv add easycat[openai-agents,webrtc]
    uv run python examples/webrtc_server.py

Setup (EC2):
    See examples/ec2_webrtc/deploy.sh for a full deployment script that
    installs coturn, configures HTTPS, and sets up systemd services.

Environment variables:
    OPENAI_API_KEY        — Required.  OpenAI API key for STT/TTS/Agent.
    TURN_SERVER_URL       — Optional.  TURN server URL (e.g. turn:1.2.3.4:3478).
    TURN_USERNAME         — Optional.  TURN server username.
    TURN_CREDENTIAL       — Optional.  TURN server credential.
    SIGNALING_HOST        — Optional.  Bind address (default 0.0.0.0).
    SIGNALING_PORT        — Optional.  Signaling HTTP port (default 8080).
    STATIC_PORT           — Optional.  Static file server port (default 8443).

Then open https://<your-ec2-ip>:8443/webrtc_client.html in your browser.
(For localhost testing, http://localhost:8443/webrtc_client.html also works.)
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
    create_session,
)
from easycat.transports.webrtc import ICEServer, WebRTCTransportConfig

_STATIC_DIR = str(Path(__file__).parent)


def _build_ice_servers() -> list[ICEServer]:
    """Build ICE server list from environment variables."""
    servers: list[ICEServer] = [
        # Public STUN — always useful for direct connections.
        ICEServer(urls="stun:stun.l.google.com:19302"),
    ]

    turn_url = os.getenv("TURN_SERVER_URL")
    if turn_url:
        servers.append(
            ICEServer(
                urls=turn_url,
                username=os.getenv("TURN_USERNAME", ""),
                credential=os.getenv("TURN_CREDENTIAL", ""),
            )
        )

    return servers


def _run_static_server(port: int) -> None:
    """Serve static files from the examples directory in a background thread."""
    handler = functools.partial(SimpleHTTPRequestHandler, directory=_STATIC_DIR)
    httpd = HTTPServer(("0.0.0.0", port), handler)
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

    signaling_host = os.getenv("SIGNALING_HOST", "0.0.0.0")
    signaling_port = int(os.getenv("SIGNALING_PORT", "8080"))
    static_port = int(os.getenv("STATIC_PORT", "8443"))

    ice_servers = _build_ice_servers()

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=WebRTCTransportConfig(
            host=signaling_host,
            port=signaling_port,
            ice_servers=ice_servers,
        ),
        agent=adapter,
        wrap_agent=False,
    )
    session = create_session(config)

    # Serve the HTML client on a background thread.
    http_thread = threading.Thread(target=_run_static_server, args=(static_port,), daemon=True)
    http_thread.start()

    print(f"Static files: http://localhost:{static_port}/webrtc_client.html")
    print(f"Signaling:    http://localhost:{signaling_port}/offer")
    if any(s.urls and "turn:" in (s.urls if isinstance(s.urls, str) else s.urls[0])
           for s in ice_servers):
        print("TURN server:  configured")
    else:
        print("TURN server:  not configured (STUN only — NAT traversal may fail)")

    await session.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
