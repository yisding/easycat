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
`EASYCAT_WS_DRAIN_TIMEOUT_S` (default `30`) controls the graceful session
window, while `EASYCAT_WS_FORCE_SHUTDOWN_TIMEOUT_S` (default `10`) bounds
forced cleanup.

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

### Binding a typed principal

`AuthResult` is deliberately only a verdict. Keep tenant and caller identity in
an application-owned type, and use one verifier in both places that need it:
the `AuthPolicy` rejects invalid credentials before session construction, then
the session factory reads the accepted handshake from the transport and
re-verifies it to recover the typed principal.

```python
from dataclasses import dataclass

from easycat import EasyConfig
from easycat.server import VoiceServer, VoiceServerConfig
from easycat.server.auth import AuthResult, RequestLike, from_websocket
from easycat.transports import WebSocketConnectionTransport


@dataclass(frozen=True)
class CallPrincipal:
    tenant_id: str
    agent_version_id: str
    call_id: str


class CallContextAuth:
    def authenticate(self, request: RequestLike) -> CallPrincipal | None:
        # Application code verifies the signature, expiry, audience, and claims.
        return verify_call_context(request.authorization_header)

    def authorize(self, request: RequestLike) -> AuthResult:
        principal = self.authenticate(request)
        reason = "allowed" if principal is not None else "invalid"
        return AuthResult(allowed=principal is not None, reason=reason)


auth = CallContextAuth()


def session_factory(transport: WebSocketConnectionTransport) -> EasyConfig:
    request = transport.request
    if request is None:
        raise RuntimeError("accepted WebSocket request is unavailable")
    principal = auth.authenticate(from_websocket(request.headers, request.path))
    if principal is None:
        raise RuntimeError("accepted credentials no longer validate")
    return EasyConfig(
        transport=transport,
        agent=build_agent(principal.tenant_id, principal.agent_version_id),
        session_id=principal.call_id,
    )


server = VoiceServer(
    VoiceServerConfig(host="0.0.0.0", auth=auth),
    session_factory=session_factory,
)
```

Do not parse a token without verifying it in the factory. Re-verification keeps
the factory correct even when credentials can expire or be revoked between the
accept check and session construction. For WebRTC, use the same pattern with
`transport.offer_request` and `from_aiohttp_request(...)`.

## Graceful shutdown

`VoiceServer.stop()` (and `stop(force=True)`) drain through the shared
capacity/draining collaborator, not `SessionManager`:

1. Set the draining flag — new connections are rejected (WS close code `1013`,
   reason `Server is draining`).
2. Close the aiohttp listeners and the raw-`websockets` `/ws` listener.
3. Wait for active sessions up to `drain_timeout_s` (default
   `drain_mode="stop_sessions"` starts graceful `session.stop()` immediately).
4. Force-escalate (`session.stop(force=True)`) anything still active after the
   window; `force_shutdown_timeout_s` bounds the forced phase.

`stop(force=True)` collapses the drain window to zero and force-stops
immediately.

For rolling restarts where calls should be allowed to reach caller hangup,
set `VoiceServerConfig(drain_mode="await_natural_end")`; new connections are
rejected, open `/ws` media sockets stay open until the caller disconnects or
`drain_timeout_s` expires, and stragglers are then force-stopped.

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
`getUserMedia()` works, configure TURN, set `WEBRTC_SIGNALING_TOKEN` so
`/config`, `/offer`, and `/stats` require a bearer token, and tune
`WEBRTC_MAX_SESSIONS` from load-test data before raising the default cap. The
bundled client can read that token from its initial `#token=` fragment, removes
it from the visible URL, and forwards it in the `Authorization` header. URL
fragments are not included in HTTP requests, while direct `?token=` query
authentication remains off unless `allow_query_token=True` is configured directly on `WebRTCTransportConfig`; the environment helper has no opt-in.

The server can use configured TURN credentials without returning TURN entries
from `/config`: the browser receives STUN-only config while the server peer can
still gather a relay candidate. Browser-side relay requires
`WEBRTC_EXPOSE_ICE_CREDENTIALS=1`; expose only short-lived TURN credentials (or
use this with a trusted demo), because every authorized client can read them.

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
`VoiceServer`'s root redirect appends `?webrtc=/webrtc` so the served client
targets the namespaced routes. It drops any legacy `?token=` rather than copy a
secret into another HTTP request; bootstrap the bundled client with the
`#token=` fragment instead. A custom client can target either surface by
setting `?webrtc=` (or its own base) accordingly.

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

EasyCat bounds stalled-client memory by inspecting aioquic's per-stream send
buffer. Because aioquic doesn't expose that value publicly, server startup
preflights the required private access path and refuses to bind with an
incompatible aioquic release. Treat that startup error as a dependency
compatibility failure; install the supported extra version or upgrade EasyCat
rather than bypassing the check.

## Twilio multi-call servers

Use `VoiceApp.run("twilio")` or
`easycat.telephony.server.serve_twilio_voice_app` as the production starting
point for phone calls. The reusable helper authenticates Twilio's HTTP
webhooks and media WebSocket handshake before minting tokens or constructing
provider sessions, caps concurrent calls, and stops all sessions during
shutdown. `examples/twilio_app.py:create_app` is the lower-level reference when
you also need outbound-call, status-callback, or SMS routes.

For public Twilio deployments:

- Generate TwiML with a `wss://` stream URL.
- Validate `X-Twilio-Signature` on both HTTP webhooks and the media WebSocket
  handshake.
- Put call-control endpoints behind bearer auth before enabling outbound calls.
- Preserve Twilio `CallSid` and `StreamSid` in logs and metrics.
- Send barge-in `clear` messages when interruption policy requires clearing
  already-buffered Twilio playback.

## Telnyx multi-call servers

Use `VoiceApp.run("telnyx")` or
`easycat.telephony.telnyx_server.serve_telnyx_voice_app` as the production
starting point for Telnyx Call Control v2 calls. The reusable helper verifies
Telnyx's Ed25519 webhook signatures, answers `call.initiated` with a one-time
stream token embedded in the media `stream_url`, caps concurrent calls, and
stops all sessions during shutdown. `examples/telnyx_app.py:create_app` is the
lower-level reference when you also need outbound-call or status-callback
routes.

For public Telnyx deployments:

- Set `TELNYX_STREAM_URL` to a public `wss://` URL; it is rejected otherwise.
- Verify `telnyx-signature-ed25519` on every webhook (five-minute replay
  window).
- Treat the stream token as the entire media auth boundary — Telnyx does not
  sign the WebSocket handshake.
- Preserve `call_control_id` in logs and metrics for call lifecycle routing.
- L16 @ 16 kHz is the default negotiated codec and matches EasyCat's internal
  bus exactly.

## Journal persistence, replication, and metrics scraping

`debug="full"` (opt in — the `EasyConfig` default is the in-memory
`debug="light"`, which writes nothing to disk) writes a crash-durable SQLite
journal per session under `EasyConfig.data_dir` when set, otherwise
`EASYCAT_DATA_DIR` (default `.easycat`) — see
[`src/easycat/runtime/DURABILITY.md`](../../src/easycat/runtime/DURABILITY.md)
for the exact durability guarantees and storage layout. Records are committed
in bounded batches (100 ms / 100 records, plus every turn boundary), and the
SQLite WAL is auto-checkpointed during long calls; persistent journal work is
offloaded from the live audio loop. That promise only holds if the resolved
data directory is a **persistent** path: a container without a
volume mounted there, or a process directory that gets wiped on redeploy,
silently discards every journal. The Docker-specific version of this guidance
— including the image's `VOLUME` declaration and named-volume compose
config — lives in
[docker.md's "Persisting the journal across restarts"](docker.md#persisting-the-journal-across-restarts);
the same `EASYCAT_DATA_DIR` mount requirement applies to any long-lived
process host (systemd unit, EC2 instance, Kubernetes `Deployment`), not just
containers.

For continuous off-host replication instead of periodic filesystem backups,
choose a replication topology explicitly. The in-process
`journal_backend="sqlite+litestream"` backend uses
`EASYCAT_JOURNAL_LITESTREAM_REPLICA` and starts one `litestream replicate`
subprocess plus one stderr thread for each live session. It is convenient for
single-call demos or tightly bounded workers, but operators must account for
that per-session process/thread cost.

An external Litestream sidecar may instead share a volume with
`journal_backend="sqlite"`. Current Litestream releases support
[watched directory replication](https://litestream.io/guides/directory-watcher/):
configure the journal directory with `dir`, `pattern: "*.sqlite"`, and
`watch: true`. Litestream discovers databases created after startup and
namespaces each remote replica by its relative path. Pin the sidecar image to a
tested release rather than `latest`; [docker.md](docker.md#litestream-and-libsql-replicas-in-a-container)
shows a complete configuration.

The `journal_backend="libsql"` alternative uses `EASYCAT_LIBSQL_URL` and
`EASYCAT_LIBSQL_AUTH_TOKEN`; see
[docker.md's "Litestream and libSQL replicas in a container"](docker.md#litestream-and-libsql-replicas-in-a-container)
for container wiring and the crash-recovery gap on the libSQL backend.

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
[observability.md](../observability.md#d-opentelemetry-facade) for the full
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
- **Persistence:** mount the resolved data root before running with
  `debug="full"` in production. `EasyConfig.data_dir` takes precedence over
  `EASYCAT_DATA_DIR`; when neither is set, the root is `.easycat`. See
  "Journal persistence, replication, and metrics scraping" above.
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
