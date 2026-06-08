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

from easycat import EasyConfig, create_session
from easycat.helpers import run_session
from easycat.transports import webrtc_transport_config_from_env


def main() -> None:
    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(
        name="assistant",
        instructions="You are a helpful voice assistant. Keep responses concise.",
    )

    transport = webrtc_transport_config_from_env()
    config = EasyConfig.browser(transport=transport, agent=agent)
    session = create_session(config)

    print(f"Open http://localhost:{transport.port} in your browser")
    if any(any("turn:" in u for u in s.urls) for s in transport.ice_servers):
        print("TURN server:  configured")
        if transport.expose_ice_credentials:
            print("TURN auth:    exposed via /config")
        else:
            print("TURN auth:    hidden from /config")
    else:
        print("TURN server:  not configured (STUN only — NAT traversal may fail)")

    run_session(session)


if __name__ == "__main__":
    main()
