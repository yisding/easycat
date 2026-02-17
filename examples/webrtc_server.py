"""WebRTC voice chat server — deployable on EC2.

Serves a signaling HTTP endpoint and the static HTML client from the same
server.  A browser connects via WebRTC, sending microphone audio and
receiving the agent's TTS response as a real-time Opus stream.

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
    SIGNALING_PORT        — Optional.  Listen port (default 8080).

Then open http://localhost:8080/webrtc_client.html in your browser.

NOTE: getUserMedia() requires a secure context.  For localhost this works
over plain HTTP.  For remote deployments, place the server behind an HTTPS
reverse proxy (e.g. nginx or Caddy with a TLS certificate).
"""

from __future__ import annotations

import asyncio
import os
import signal
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
    )
    session = create_session(config)

    print(f"Open http://localhost:{signaling_port}/webrtc_client.html in your browser")
    if any(
        s.urls and "turn:" in (s.urls if isinstance(s.urls, str) else s.urls[0])
        for s in ice_servers
    ):
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
