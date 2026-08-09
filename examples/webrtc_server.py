"""Multi-client WebRTC voice chat server — deployable on EC2.

A bundled HTML client is served automatically from the signaling server.
Each browser offer creates its own EasyCat session, so multiple browser tabs
or users can talk to isolated agents in one server process.

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
    WEBRTC_EXPOSE_ICE_CREDENTIALS — Optional. Set to 1 to return complete TURN
                                    entries from /config. Use only with
                                    trusted demos or short-lived credentials;
                                    otherwise browser config is STUN-only and
                                    TURN is server-side relay only.
    SIGNALING_HOST        — Optional.  Bind address (default 127.0.0.1).
    SIGNALING_PORT        — Optional.  Listen port (default 8080).
    WEBRTC_SIGNALING_TOKEN — Optional on localhost; required for public binds.
    WEBRTC_MAX_SESSIONS   — Optional.  Concurrent browser sessions (default 64).

Then open http://localhost:8080 in your browser. When
``WEBRTC_SIGNALING_TOKEN`` is set, use the printed ``#token=`` bootstrap URL and
replace its placeholder with the URL-encoded token value.
The bundled client is same-origin with the signaling server. If you host a
custom browser UI elsewhere, pass explicit cors_allowed_origins to
WebRTCTransportConfig instead of relying on wildcard CORS.

NOTE: getUserMedia() requires a secure context.  For localhost this works
over plain HTTP.  For remote deployments, place the server behind an HTTPS
reverse proxy (e.g. nginx or Caddy with a TLS certificate).
"""

from __future__ import annotations

from easycat import EasyConfig, require_env
from easycat.server import run_webrtc_config_server
from easycat.transports import webrtc_transport_config_from_env


def main() -> None:
    require_env("OPENAI_API_KEY")

    def config(transport):
        from agents import Agent  # type: ignore[import-untyped]

        agent = Agent(
            name="assistant",
            instructions="You are a helpful voice assistant. Keep responses concise.",
        )
        return EasyConfig.browser(transport=transport, agent=agent)

    transport = webrtc_transport_config_from_env()
    token_hint = "#token=<URL_ENCODED_TOKEN>" if transport.auth_token else ""
    print(f"Open http://localhost:{transport.port}/webrtc_client.html{token_hint} in your browser")
    ice_urls = (url for server in transport.ice_servers for url in server.urls)
    if any(url.lower().startswith(("turn:", "turns:")) for url in ice_urls):
        print("TURN server:  configured")
        if transport.expose_ice_credentials:
            print("TURN browser: /config includes only entries with complete credentials")
        else:
            print("TURN browser: omitted from /config (server-side relay only)")
    else:
        print("TURN server:  not configured (STUN only — NAT traversal may fail)")

    run_webrtc_config_server(config, transport)


if __name__ == "__main__":
    main()
