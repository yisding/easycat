"""WebRTC voice chat server — deployable on EC2.

Serves a signaling HTTP endpoint and the static HTML client from the same
server.  A browser connects via WebRTC, sending microphone audio and
receiving the agent's TTS response as a real-time Opus stream.

Setup (local):
    export OPENAI_API_KEY="..."
    uv sync --extra openai-agents --extra webrtc
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
    SIGNALING_PORT        — Optional.  Listen port (default 8080).

Then open http://localhost:8080/webrtc_client.html in your browser.

NOTE: getUserMedia() requires a secure context.  For localhost this works
over plain HTTP.  For remote deployments, place the server behind an HTTPS
reverse proxy (e.g. nginx or Caddy with a TLS certificate).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from easycat import EasyCatConfig, ICEServer, WebRTCTransportConfig, create_session

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    build_openai_agents_adapter,
    default_event_logging,
    require_env,
    wait_for_shutdown_signal,
)
from runtime_feedback import attach_runtime_feedback  # noqa: E402

# Serves only the webrtc_static/ subdirectory (contains only the HTML client).
_STATIC_DIR = str(Path(__file__).parent / "webrtc_static")


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


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    adapter = build_openai_agents_adapter(
        instructions="You are a helpful voice assistant. Keep responses concise."
    )

    signaling_host = os.getenv("SIGNALING_HOST", "0.0.0.0")
    signaling_port = int(os.getenv("SIGNALING_PORT", "8080"))

    ice_servers = _build_ice_servers()

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=WebRTCTransportConfig(
            host=signaling_host,
            port=signaling_port,
            ice_servers=ice_servers,
            static_dir=_STATIC_DIR,
        ),
        agent=adapter,
        wrap_agent=False,
        event_logging=default_event_logging(),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    print(f"Open http://localhost:{signaling_port}/webrtc_client.html in your browser")
    if any(any("turn:" in u for u in s.urls) for s in ice_servers):
        print("TURN server:  configured")
    else:
        print("TURN server:  not configured (STUN only — NAT traversal may fail)")

    await session.start()

    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
