"""WebRTC voice chat server — deployable on EC2.

A bundled HTML client is served automatically from the signaling server.
A browser connects via WebRTC, sending microphone audio and receiving the
agent's TTS response as a real-time Opus stream.

Setup (local):
    export OPENAI_API_KEY="..."
    uv sync --extra openai --extra openai-agents --extra webrtc --group dev
    uv run easycat doctor
    uv run easycat doctor --env-file .env  # if keys live in .env
    uv run easycat doctor --env-file .env --json  # for parseable checks
    uv run python examples/webrtc_server.py
    uv run --env-file .env python examples/webrtc_server.py  # if keys live in .env

Setup (EC2):
    See examples/ec2_webrtc/deploy.sh for a full deployment script that
    installs coturn and systemd services. For browser microphone access on
    a public hostname, put the HTTP server behind an HTTPS reverse proxy.

Environment variables:
    OPENAI_API_KEY        — Required.  OpenAI API key for STT/TTS/Agent.
    TURN_SERVER_URL       — Optional.  TURN server URL (e.g. turn:1.2.3.4:3478).
    TURN_USERNAME         — Optional.  TURN server username.
    TURN_CREDENTIAL       — Optional.  TURN server credential.
    WEBRTC_EXPOSE_ICE_CREDENTIALS — Optional. Set to 1 to return TURN
                                    credentials from /config. Use only with
                                    trusted demos or short-lived credentials.
    SIGNALING_HOST        — Optional.  Bind address (default 127.0.0.1).
    SIGNALING_PORT        — Optional.  Listen port (default 8080).

Then open http://localhost:8080 in your browser.
The bundled client is same-origin with the signaling server. If you host a
custom browser UI elsewhere, pass explicit cors_allowed_origins to
WebRTCTransportConfig instead of relying on wildcard CORS.

NOTE: getUserMedia() requires a secure context.  For localhost this works
over plain HTTP.  For remote deployments, place the server behind an HTTPS
reverse proxy (e.g. nginx or Caddy with a TLS certificate).
"""

from __future__ import annotations

import asyncio
import os

from easycat import (
    EasyConfig,
    ICEServer,
    WebRTCTransportConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)


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


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


async def main() -> None:
    require_env("OPENAI_API_KEY")
    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(
        name="assistant",
        instructions="You are a helpful voice assistant. Keep responses concise.",
    )

    signaling_host = os.getenv("SIGNALING_HOST", "127.0.0.1")
    signaling_port = int(os.getenv("SIGNALING_PORT", "8080"))
    expose_ice_credentials = _env_flag("WEBRTC_EXPOSE_ICE_CREDENTIALS")

    ice_servers = _build_ice_servers()

    config = EasyConfig(
        transport=WebRTCTransportConfig(
            host=signaling_host,
            port=signaling_port,
            ice_servers=ice_servers,
            expose_ice_credentials=expose_ice_credentials,
        ),
        agent=agent,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    print(f"Open http://localhost:{signaling_port} in your browser")
    if any(any("turn:" in u for u in s.urls) for s in ice_servers):
        print("TURN server:  configured")
        if expose_ice_credentials:
            print("TURN auth:    exposed via /config")
        else:
            print("TURN auth:    hidden from /config")
    else:
        print("TURN server:  not configured (STUN only — NAT traversal may fail)")

    await session.start()

    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
