# Chapter 9: Serve Many Callers

A local microphone app owns one transport and one session. A server must turn
every accepted connection into an isolated EasyCat session, reject work it
cannot safely serve, and close the whole process in the right order.

This chapter runs a credential-free checkpoint around EasyCat's public auth,
capacity, and drain collaborators. The sessions are tiny lifecycle probes so
the lesson can isolate admission behavior from sockets, audio, and providers.

## Prerequisites

- Complete [chapter 6](../06-session-control/) for the single-session lifecycle
  and [chapter 8](../08-testing-evals/) for offline checkpoints.
- Run `uv sync --group dev` from the repository root.
- No API key, microphone, provider, or public network is needed.
- The checkpoint opens no socket, so it also runs in locked-down CI.

## Run the server checkpoint

```bash
uv run python docs/using-easycat/09-multi-caller/main.py
```

Expected output:

```text
PASS auth: missing bearer token rejected before session creation
PASS capacity: extra caller rejected instead of queued
PASS isolation: released slot created fresh session 2
PASS shutdown: draining rejected new work and stopped session 2
PASS bind guard: public unauthenticated endpoint failed closed
```

The script exercises `BearerTokenAuth`, `CapacityGate`, and
`enforce_bind_guard`, the transport-independent collaborators used by the
server paths. It admits simulated connection keys through the same order as a
real server: authenticate, reserve, create, track, drain, and release.

## One connection, one fresh session

The server boundary is a factory, not a shared `Session`:

```python
def session_factory(connection):
    return build_session_for(connection)
```

EasyCat calls it only after authentication and capacity reservation succeed.
Each accepted connection must get fresh session-owned collaborators:

- the concrete connection transport and its ingress/playback queues;
- the `Session`, turn state, event subscriptions, and journal context;
- stateful provider clients or agent runners that are not explicitly safe to
  share.

The checkpoint's second successful connection receives `DemoSession(2)`, not a
restarted `DemoSession(1)`. In an application, use a config factory so EasyCat
builds the real session around the per-connection transport:

```python
from easycat import EasyConfig
from easycat.server import run_websocket_config_server


def config_for(transport):
    return EasyConfig(transport=transport, agent=build_agent())


run_websocket_config_server(config_for)
```

Do not create one `EasyConfig` at import time and hand its live transport or
stateful providers to every caller.

## Authentication happens before allocation

`WebSocketSessionServerConfig(auth_token=...)` enables the same bearer-token
gate on the standalone WebSocket helper.
Non-browser clients authenticate the handshake with:

```text
Authorization: Bearer <token>
```

A missing or invalid token returns HTTP 401 before the session factory runs.
The checkpoint verifies this by asserting that its session list is still
empty. This ordering prevents unauthenticated requests from consuming provider
clients, journals, capacity slots, or background tasks.

Query-string tokens are off by default. They leak more easily through URLs,
history, proxy logs, and analytics. Browsers cannot add an `Authorization`
header to a native WebSocket handshake, so the standalone browser demo can opt
in for loopback development by passing `allow_query_token=True` to
`serve_websocket_sessions()` or `serve_websocket_config_sessions()` — it is a
keyword argument on those helpers (and on `BearerTokenAuth`), not a field on
`WebSocketSessionServerConfig`. Prefer bearer headers and edge-issued
credentials in production.

The simple helper reads `EASYCAT_WS_TOKEN` through its `EASYCAT_WS_*`
environment defaults.
The unified process layer standardizes on `EASYCAT_SERVE_TOKEN` through
`bearer_auth_from_env()`.

## Non-loopback binds fail closed

Both WebSocket and WebRTC serve paths apply the same bind guard. An
unauthenticated `127.0.0.1` listener is acceptable for local development, but
binding `0.0.0.0` without a token raises `ValueError` before a socket opens.

`unsafe_allow_no_auth=True` is the only structured escape hatch. Its name is
deliberately uncomfortable: do not use it merely to silence a deployment
error. Configure TLS at the ingress and authentication/authorization at the
edge or server.

## Capacity rejects instead of queueing callers

`max_sessions` is a per-process concurrent-session cap. When every slot is
reserved, `CapacityGate.try_acquire()` returns `False` instead of waiting. The
WebSocket helper maps that result to RFC 6455 close code `1013` (`Try Again
Later`) and the reason `Server is at the configured session limit`.

The extra caller does not wait for a provider slot while holding unbounded
memory. It does not invoke the factory. When the first caller disconnects, its
session stops and the slot is released; the next caller gets a fresh session.

Choose a limit from measured CPU, memory, provider quotas, file descriptors,
and worst-case teardown time. A configured cap is overload protection, not a
throughput target. Load test before raising it.

## The helper owns connection teardown

In the real socket path, `serve_websocket_sessions` wraps each accepted session in
`SessionManager.connection(...)`:

1. reserve a capacity slot;
2. create and start the session;
3. wait for the WebSocket to close;
4. stop and remove the session in `finally`;
5. release the slot in an outer `finally`.

If a session fails to start or the handler is cancelled, those `finally`
blocks still release ownership. On process shutdown, the helper closes the
listener, waits for connection handlers, and calls `stop_all()` for anything
still registered.

The offline supervisor mirrors that guarantee: startup rollback restores its
bookkeeping before best-effort forced teardown, and disconnect accounting runs
in `finally` even when session shutdown raises or is cancelled.

## Use `VoiceServer` for one production process policy

The standalone helper is a focused WebSocket entry point. `VoiceServer`
co-hosts health endpoints and applies one process policy across WebSocket and
WebRTC:

```python
from easycat.server import BearerTokenAuth, VoiceServer, VoiceServerConfig

server = VoiceServer.from_app(
    app,  # the VoiceApp you have been building since chapter 0
    VoiceServerConfig(
        host="0.0.0.0",
        port=8080,
        max_sessions=64,
        auth=BearerTokenAuth(token=token),
        drain_timeout_s=30.0,
        force_shutdown_timeout_s=10.0,
    ),
)
server.run()
```

The shared capacity gate spans `/ws` and `/webrtc/offer`; 64 means 64 active
sessions across both paths, not 64 of each. `/health/live` answers whether the
process is alive, while `/health/ready` becomes unavailable when draining or at
capacity. Readiness is the signal an ingress should use to stop sending new
callers.

Use an environment-backed token instead of a source literal:

```python
from easycat.server import bearer_auth_from_env

auth = bearer_auth_from_env("EASYCAT_SERVE_TOKEN")
```

Never print the resolved policy, request headers, or token in diagnostics.

## Graceful shutdown is admission control plus a deadline

`await server.stop()` performs a bounded supervised shutdown:

1. mark the shared gate draining so new calls are rejected;
2. close the WebSocket and HTTP/WebRTC listeners;
3. ask active sessions to stop gracefully for `drain_timeout_s`
   (`drain_mode="stop_sessions"`, the default);
4. force-stop stragglers and bound that phase with
   `force_shutdown_timeout_s`.

`await server.stop(force=True)` skips the graceful window. Use it for process
failure or an outer deadline, not as the normal deployment path. The process
manager's termination grace period must be longer than EasyCat's drain and
forced-shutdown windows combined.

For rolling deploys where callers should finish naturally, configure
`VoiceServerConfig(drain_mode="await_natural_end")`; the server rejects new
connections, leaves existing media sockets open until caller hangup, and only
force-stops sessions still active after `drain_timeout_s`.

Sticky routing or an external control plane is needed when multiple worker
processes must address a specific live session: each process owns its own
in-memory registry and capacity gate.

## Which server surface should you choose?

| Need | Surface |
|---|---|
| One local voice interaction | `VoiceApp.run(...)` |
| Focused WebSocket multi-session server | `run_websocket_config_server(...)` |
| Async WebSocket server inside your supervisor | `serve_websocket_config_sessions(...)` |
| Unified WebSocket + WebRTC process policy | `VoiceServer.from_app(...)` |
| HTTP/3 + QUIC clients | WebTransport server helper |
| Phone calls and webhooks | Twilio app factory in chapter 10 |

The per-connection factory rule is the same for every row that serves multiple
callers.

Continue with [the exercises](./EXERCISES.md) to inspect factory allocation,
capacity recovery, the bind guard, and drain behavior.

## What you should be able to answer now

> Why not share one `Session` across connections?

It owns mutable turn, transport, task, and journal state for one interaction.

> Does `max_sessions=64` queue caller 65?

No. The capacity gate rejects it so overload remains bounded.

> What should happen before draining existing sessions?

Readiness must fail and listeners must stop admitting new sessions.

## What's next

Chapter 10 applies the same one-connection/one-session rule to phone calls,
where Twilio webhooks, Media Streams, call control, screening, and IVR add a
second control plane.
