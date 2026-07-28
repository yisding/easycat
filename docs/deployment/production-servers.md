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
from easycat.server import run_websocket_config_server

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

> **Breaking change — `?token=` query auth is now off by default.** The
> WebSocket and WebRTC serve helpers used to accept a `?token=` query parameter
> unconditionally whenever a token was set. Query-token auth is now gated behind
> `allow_query_token` (default **`False`**). Bearer-header clients are
> unaffected. The bundled WebSocket browser client relies on `?token=` (browsers
> cannot set headers on the WebSocket handshake), so pass `allow_query_token=True`
> as a loopback/dev opt-in to keep it working locally:
>
> ```python
> run_websocket_config_server(config, allow_query_token=True)  # dev only
> ```
>
> The bundled WebRTC client sends `Authorization: Bearer` and is **not** affected.

## Unified auth model

The `easycat.server.auth` policy layer is shared by `VoiceServer`'s WebSocket
and WebRTC paths and by the standalone WebTransport server:

- `NoAuth` — open access (loopback/dev).
- `BearerTokenAuth(token=..., allow_query_token=False)` — constant-time
  (`hmac.compare_digest`) bearer-header auth; `?token=` query auth is opt-in.
- `bearer_auth_from_env("EASYCAT_SERVE_TOKEN")` — build a `BearerTokenAuth` from
  the shipped `EASYCAT_SERVE_TOKEN` env var (returns `None` when unset).

```python
from easycat.server import BearerTokenAuth, VoiceServer, VoiceServerConfig

server = VoiceServer.from_app(
    app,
    VoiceServerConfig(host="0.0.0.0", auth=BearerTokenAuth(token="...")),
)
server.run()
```

**Non-loopback binds require a token.** `VoiceServer`, the standalone
WebSocket/WebRTC helpers, and `WebTransportServer` all raise `ValueError` at
`start()` when binding a non-loopback host (for example `0.0.0.0`) without a
token. The **only** escape hatch is the structured
`unsafe_allow_no_auth=True` field. Twilio uses its own required webhook and
media-handshake signature validation because provider callbacks must be
public; local microphone transports do not bind a network listener.

## Graceful shutdown

`VoiceServer.stop()` (and `stop(force=True)`) drain through the shared
capacity/draining collaborator, not `SessionManager`:

1. Set the draining flag — new connections are rejected (WS close code `1013`,
   reason `Server is draining`).
2. Close the aiohttp listeners and the raw-`websockets` `/ws` listener.
3. Wait for active sessions up to `drain_timeout_s` (graceful `session.stop()`).
4. Force-escalate (`session.stop(force=True)`) anything still active after the
   window; `force_shutdown_timeout_s` bounds the forced phase.

`stop(force=True)` collapses the drain window to zero and force-stops
immediately.

## WebRTC browser servers

Use `run_webrtc_config_server()` for browser microphone deployments that need
many simultaneous tabs/users in one process:

```python
from easycat import EasyConfig, require_env
from easycat.server import run_webrtc_config_server
from easycat.transports import webrtc_transport_config_from_env

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

### Flat routes vs. the `VoiceServer` `/webrtc/*` namespace

The route paths differ by surface, but the handlers are one shared
implementation (`easycat.server.webrtc_routes.WebRTCRoutes`):

- **Standalone `run_webrtc_config_server()`** keeps the **flat** routes
  `/offer`, `/config`, `/stats` (and root + bundled client at `/`).
- **`VoiceServer`** mounts the **namespaced** routes `/webrtc/offer`,
  `/webrtc/config`, `/webrtc/stats` on the same aiohttp listener as
  `/health/*`. WebRTC offers reserve through the SAME capacity gate as `/ws`,
  so capacity, draining, and `stop()` drain span both transports. Enable it via
  `VoiceServerConfig(enable_webrtc=True)` (the default) with a configured
  `session_factory`; the unified `AuthPolicy` guards the mounted routes, and the
  `allow_query_token` default-off posture applies (the WebRTC client uses the
  `Authorization` header, so it is unaffected).

The SAME bundled client HTML serves both. It resolves its route base from a
`?webrtc=<prefix>` query parameter (defaulting to `""` for the flat helper);
`VoiceServer`'s root redirect appends `?webrtc=/webrtc` (preserving any
`?token=`) so the served client targets the namespaced routes. A custom client
can target either surface by setting `?webrtc=` (or its own base) accordingly.

## WebTransport servers

Use `run_webtransport_config_server()` when the client needs HTTP/3 + QUIC
streaming semantics and your ingress can support UDP/443 end to end. Keep
WebTransport behind the optional `webtransport` extra and deploy it only where
certificate, HTTP/3, QUIC, and load-balancer support are explicit.

`WebTransportTransportConfig` defaults to `host="127.0.0.1"`. For a public
bind, set `auth_token` (the example reads `EASYCAT_SERVE_TOKEN`); the HTTP/3
CONNECT is rejected with `401` before a session is created unless it carries
`Authorization: Bearer <token>`. Browser WebTransport cannot set arbitrary
CONNECT headers, so browser deployments must explicitly set
`allow_query_token=True` and connect to
`https://host/easycat?token=<token>`. Query-token auth is off by default, and
token-bearing URLs must be treated as secrets. Use
`unsafe_allow_no_auth=True` only for a deliberately unauthenticated public
endpoint.

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

## Journal persistence, replication, and metrics scraping

`debug="full"` (opt in — the `EasyConfig` default is the in-memory
`debug="light"`, which writes nothing to disk) writes a crash-durable SQLite
journal per session under `EASYCAT_DATA_DIR` (default `.easycat`) — see
[`src/easycat/runtime/DURABILITY.md`](../../src/easycat/runtime/DURABILITY.md)
for the exact durability guarantees and storage layout. That promise only
holds if `EASYCAT_DATA_DIR` is a **persistent** path: a container without a
volume mounted there, or a process directory that gets wiped on redeploy,
silently discards every journal. The Docker-specific version of this guidance
— including the image's `VOLUME` declaration and named-volume compose
config — lives in
[docker.md's "Persisting the journal across restarts"](docker.md#persisting-the-journal-across-restarts);
the same `EASYCAT_DATA_DIR` mount requirement applies to any long-lived
process host (systemd unit, EC2 instance, Kubernetes `Deployment`), not just
containers.

For continuous off-host replication instead of periodic filesystem backups,
set `journal_backend="sqlite+litestream"` or `journal_backend="libsql"` on
`EasyConfig`/`SessionConfig` and configure the replica target through
environment variables (`EASYCAT_JOURNAL_LITESTREAM_REPLICA`,
`EASYCAT_LIBSQL_URL` / `EASYCAT_LIBSQL_AUTH_TOKEN`) — see
[docker.md's "Litestream and libSQL replicas in a container"](docker.md#litestream-and-libsql-replicas-in-a-container)
for the sidecar-vs-bundled-binary tradeoff and the crash-recovery gap on the
libSQL backend.

**Readiness probes.** `VoiceServer` (the process layer behind
`run_webrtc_config_server()` and `VoiceServer.from_app(...)`) serves
`GET /health/live` (loop responsiveness), `GET /health/ready` (draining /
capacity / route-stack / manifest+plan checks — 200 only when every check
passes), and `GET /health` (the full JSON snapshot, also summarized above).
Point a Kubernetes readiness probe, an ALB target-group health check, or a
Docker `HEALTHCHECK` at `/health/ready` rather than a bare TCP connect —
plain WebSocket-only servers such as `run_websocket_config_server()` do not
serve an HTTP endpoint at all, so they can only be probed at the TCP level
(see docker.md's ["Container health checks"](docker.md#container-health-checks)
for the concrete `HEALTHCHECK` wiring and its `EASYCAT_HEALTH_URL` switch).

**Metrics scraping.** `VoiceServer`'s `GET /metrics` is a read-only, PII-safe
JSON snapshot of in-process counters/gauges — poll it directly without an
OTel SDK. For histograms and traces, install an OTel SDK/exporter and
initialize `MeterProvider`/`TracerProvider` yourself before creating
sessions (EasyCat's `easycat._observability` facade is a no-op without one);
point the standard `OTEL_EXPORTER_OTLP_ENDPOINT` /
`OTEL_EXPORTER_OTLP_PROTOCOL` / `OTEL_SERVICE_NAME` environment variables at
your collector, which then bridges to a Prometheus scrape target or your
backend of choice. See
[observability.md](../observability.md#d-—-opentelemetry-facade) for the full
metric/attribute catalog and the PII-safety allow-list, and docker.md's
["Scraping metrics"](docker.md#scraping-metrics) for the container-specific
walkthrough.

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
- **Persistence:** mount a persistent volume/path at `EASYCAT_DATA_DIR` before
  running with `debug="full"` in production — see "Journal persistence,
  replication, and metrics scraping" above.
- **Health probes:** point liveness/readiness checks at `/health/live` /
  `/health/ready` for `VoiceServer`-based servers; fall back to a TCP connect
  for WebSocket-only servers with no HTTP surface.
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
