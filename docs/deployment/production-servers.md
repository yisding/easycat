# Production multi-client servers

EasyCat production servers should create **one EasyCat `Session` per live
browser client, WebTransport connection, or phone call**. Do not share a
single transport instance across callers: each transport keeps its own audio
queues, outbound playback state, interruption state, browser event channel,
and debug journal context.

## The standard server shape

Use this shape for every network transport:

1. Accept a client connection or signaling offer.
2. Create a fresh per-client transport.
3. Build an `EasyConfig` with that transport.
4. Create and start one `Session` with `SessionManager`.
5. Wait for the protocol connection to close.
6. Stop that session and release the concurrency slot.
7. During process shutdown, stop the listener first, then stop active sessions.

The built-in server helpers already implement that lifecycle for browser and
WebTransport clients. Twilio's app factory follows the same pattern for phone
calls and adds Twilio webhook/status routing.

## WebSocket servers

Use `run_websocket_config_server()` for a simple synchronous entry point, or
`serve_websocket_config_sessions()` inside your own asyncio supervisor:

```python
from easycat import EasyConfig, require_env
from easycat.transports import run_websocket_config_server

require_env("OPENAI_API_KEY")


def config(transport):
    from agents import Agent

    return EasyConfig(
        transport=transport,
        agent=Agent(name="assistant", instructions="Be concise."),
    )


run_websocket_config_server(config)
```

Set `EASYCAT_WS_TOKEN` before exposing the server beyond loopback, and tune
`EASYCAT_WS_MAX_SESSIONS` from measured CPU/RAM capacity. Non-browser clients
should authenticate with `Authorization: Bearer <token>`.

## WebRTC browser servers

Use `run_webrtc_config_server()` for browser microphone deployments that need
many simultaneous tabs/users in one process:

```python
from easycat import EasyConfig, require_env
from easycat.transports import run_webrtc_config_server, webrtc_transport_config_from_env

require_env("OPENAI_API_KEY")


def config(transport):
    from agents import Agent

    return EasyConfig.browser(
        transport=transport,
        agent=Agent(name="assistant", instructions="Be concise."),
    )


run_webrtc_config_server(config, webrtc_transport_config_from_env())
```

Each `POST /offer` creates an isolated EasyCat session. `GET /config` serves
ICE server configuration for the bundled browser client, `POST /stats` accepts
sanitized WebRTC stats snapshots, and `/health` reports `status`,
`active_sessions`, and `max_sessions` for readiness checks.
For public deployments, put the signaling server behind HTTPS so
`getUserMedia()` works, configure TURN, set `SIGNALING_AUTH_TOKEN` so `/offer`
and `/stats` require a bearer/query token, and tune `WEBRTC_MAX_SESSIONS` from
load-test data before raising the default cap.

## WebTransport servers

Use `run_webtransport_config_server()` when the client needs HTTP/3 + QUIC
streaming semantics and your ingress can support UDP/443 end to end. Keep
WebTransport behind the optional `webtransport` extra and deploy it only where
certificate, HTTP/3, QUIC, and load-balancer support are explicit.

## Twilio multi-call servers

Use `examples/twilio_app.py:create_app` as the production starting point for
phone calls. It creates one `TwilioConnectionTransport` and one `Session` per
Media Stream WebSocket, tracks `CallSid -> session` for status callbacks, and
stops all sessions during FastAPI lifespan shutdown.

For public Twilio deployments:

- Generate TwiML with a `wss://` stream URL.
- Validate Twilio webhook signatures.
- Put call-control endpoints behind bearer auth before enabling outbound calls.
- Preserve Twilio `CallSid` and `StreamSid` in logs and metrics.
- Send barge-in `clear` messages when interruption policy requires clearing
  already-buffered Twilio playback.

## Operations checklist

- **Ingress:** terminate TLS/WSS at a reverse proxy or load balancer; forward
  WebSocket upgrade headers; avoid proxy buffering on streaming routes.
- **Workers:** in-memory session registries are per process. Use sticky routing
  or an external control plane if a deployment uses multiple workers and needs
  cross-session control.
- **Limits:** set per-process session caps (`EASYCAT_WS_MAX_SESSIONS`,
  `WEBRTC_MAX_SESSIONS` / `WebRTCTransportConfig.max_sessions`, or
  WebTransport's `max_concurrent_sessions`) and load test before raising them.
- **Shutdown:** fail readiness first, stop accepting new connections, give live
  calls a bounded drain window, then force-stop remaining sessions before the
  process manager's graceful-shutdown timeout expires.
- **Observability:** export journals/debug bundles, track connect/disconnect
  counts, close codes, queue drops, WebRTC ICE states, Twilio `stop` events,
  and first-audio latency per transport.

## Source references

This guide follows the current official operational guidance from FastAPI
lifespan docs, Starlette WebSocket/lifespan docs, Uvicorn deployment/server
behavior docs, aiortc's peer-connection API and server example, Twilio Media
Streams/TwiML docs, and aioquic WebTransport examples.

## Copyable commands

```bash
uv run easycat docs --audience operators
uv run easycat docs --audience operators --json
uv run python examples/ws_server.py
uv run python examples/webrtc_server.py
uv run python examples/webtransport_server.py
uv run uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0
uv run pytest tests/transports/test_websocket_session_server.py
uv run pytest tests/transports/test_webrtc_config.py
uv run pytest tests/transports/test_webrtc_lifecycle_server.py
uv run pytest tests/transports/test_webtransport_session.py
```
