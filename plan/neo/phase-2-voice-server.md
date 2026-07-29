# Phase 2 — VoiceServer, Manifest Projects, Provider Planning

Status: active implementation plan.

## Goal

Add a production process layer that turns one or more `VoiceApp` instances into
a deployable server with health/readiness, auth, metrics, graceful shutdown,
manifest loading, and provider/capability planning.

Target API:

```python
from easycat.server import VoiceServer

server = VoiceServer.from_manifest("easycat.toml")
server.run()
```

Or programmatic mounting:

```python
server = VoiceServer()
server.mount("/support", VoiceApp(agent=support_agent))
server.mount("/sales", VoiceApp(agent=sales_agent))
server.run()
```

## Why This Matters

After a voice bot works locally, developers immediately need production
answers:

- How do I serve many browser or phone sessions?
- How do I expose health/readiness for deployment platforms?
- How do I put auth in front of the signaling/media endpoints?
- How do I cap sessions and drain gracefully?
- How do I know which providers, extras, and env vars are required?
- How do I inspect what is running without reading application code?

These concerns should be provided by EasyCat, not rebuilt in every app.

## Existing Building Blocks

Reuse (correctly characterized — see the net-new callouts below for what is
NOT reusable as assumed):

- `Session` as the per-conversation runtime.
- `create_session(EasyConfig)` as the provider/session construction path.
- `SessionManager` as a bare multi-session **registry** (`session_manager.py:18-105`):
  it exposes `add`/`remove`/`stop_all`/`connection` ONLY. It has **no**
  `max_sessions`, **no** `__len__`, and **no** draining state. Do NOT treat it
  as the owner of "session limits" or draining (see "Capacity & draining are
  net-new" below).
- WebRTC config server helper as the prototype for health, auth, stats,
  session limits, and per-offer sessions — but note its capacity/draining logic
  lives **inline** in the helper, not in `SessionManager`.
- WebSocket config server helper for per-client WebSocket sessions — note it
  has its own inline capacity/draining and **no** non-loopback auth guard (see
  Auth Model).
- Runtime health-check capability detection and session health checkers.
- Existing provider catalogs for **STT/TTS only** provider metadata
  (`_provider_catalog.py:1-2,285-353`). The other five planner roles
  (vad/transport/agent/noise/echo) have no catalog — see Provider/Capability
  Planning.
- Existing observability module and safe attribute policy for metrics — new
  server metric names/labels must be **registered** in the frozen allow-lists,
  not merely emitted (see Metrics).
- Existing signal/shutdown utilities.

### Capacity & draining are net-new at the shared layer (NOT in `SessionManager`)

`SessionManager` is a registry only. Capacity (`asyncio.Semaphore` + an active
session set) and draining state currently live **inline** in the two serve
helpers:

- WebRTC: `transports/webrtc.py:354,356-357,377-390,422-434,475-482`.
- WebSocket: `transports/websocket.py:146,154-156,177-179`.

**Work item (M5 scope):** LIFT the `Semaphore`/active-session-set/draining
logic out of both serve helpers into shared `VoiceServer` internals
(`server/voice_server.py` plus a small capacity/lifecycle collaborator). This
is **net-new code at the shared layer**, not a reuse of `SessionManager`. The
two existing serve helpers should then delegate to the shared internals so
capacity and draining behave identically across transports.

> **Cross-milestone dependency (D5 / D8 / SEQ-1).** The M4 `/health/ready`
> "active sessions are below capacity" check (see Health and Readiness) needs a
> capacity counter, but the full `Semaphore`/active-set/draining lift lands in
> **M5**, not M4. M4 therefore ships only a **minimal capacity counter** (an
> active-session count + the configured `max_sessions` limit) sufficient for the
> readiness check; the shared `Semaphore`/active-set/draining collaborator that
> rejects offers and drives graceful shutdown is the M5 deliverable. A
> maintainer must NOT implement the M4 readiness check against the M5
> collaborator — M4 reads the minimal counter, and M5 replaces it with the
> lifted shared machinery without changing the readiness contract.

## New Package Layout

```text
src/easycat/server/
  __init__.py
  auth.py
  config.py
  transports.py   # small per-transport helper types + capacity/draining collaborator
  health.py
  metrics.py
  routes.py
  voice_server.py

src/easycat/project/
  __init__.py
  loader.py
  manifest.py
  schema.py

src/easycat/planning/
  __init__.py
  provider_plan.py
  transport_registry.py
```

> Note: there is **no** `ConnectionContext` type. The earlier `context.py`
> sketch is removed. The serve helpers already take transport-specific args
> (`WebRTCTransport`, `WebSocketConnectionTransport`,
> `WebTransportConnectionTransport`, `TwilioConnectionTransport`), so the
> per-connection seam is a per-transport `config_factory`
> (`Callable[[TransportT], EasyConfig]`), not a unified context object. The
> renamed `transports.py` module holds the small per-transport helper types and
> the lifted capacity/draining collaborator; it does NOT introduce an abstract
> context.

## VoiceServer API

The per-connection config seam is a **per-transport** factory, selected per
route/transport. `ConnectionContext` does not exist and is removed from the
plan — use the transport-specific shape `Callable[[TransportT], EasyConfig | Session]`
where `TransportT` is the concrete connection transport for the route's mode
(`WebRTCTransport`, `WebSocketConnectionTransport`,
`WebTransportConnectionTransport`, `TwilioConnectionTransport`). This matches
the signature used in `architecture-boundaries.md` and `phase-1-voice-app.md`.

```python
# TransportT is the concrete per-route transport type (WebRTCTransport,
# WebSocketConnectionTransport, WebTransportConnectionTransport,
# TwilioConnectionTransport). Each mounted app/route carries its own
# transport-typed factory; there is NO single unified context object.
SessionFactory = Callable[[TransportT], EasyConfig | Session]


class VoiceServer:
    def __init__(
        self,
        config: VoiceServerConfig | None = None,
        *,
        # Selected per route/transport. For a single-route server this is one
        # transport-typed factory; for a multi-route server each mounted app
        # contributes its own.
        session_factory: SessionFactory | None = None,
    ) -> None: ...

    @classmethod
    def from_app(cls, app: VoiceApp, config: VoiceServerConfig | None = None) -> VoiceServer: ...

    @classmethod
    def from_manifest(
        cls,
        path: str | Path = "easycat.toml",
        *,
        profile: str = "default",
    ) -> VoiceServer: ...

    def mount(self, path: str, app: VoiceApp, *, profile: str | None = None) -> None: ...

    async def start(self) -> None: ...
    async def serve(self, stop_event: asyncio.Event | None = None) -> None: ...
    async def stop(self, *, force: bool = False) -> None: ...
    def run(self) -> None: ...
    async def health(self) -> VoiceServerHealth: ...
```

### Event loop ownership

One rule across `VoiceApp` and `VoiceServer`:

- `run()` is the ONLY method that calls `asyncio.run()` (sole loop owner).
- The async verb is `serve()` on both objects (the asymmetric `serve_forever`
  is dropped to match `VoiceApp.serve()`).
- `VoiceServer` composes mounted apps via their per-transport `config_factory`
  ONLY. It NEVER calls `VoiceApp.run()` (which would nest `asyncio.run()`).

### `from_app` / `mount` layering and ownership rule

`VoiceApp.serve()` and `VoiceServer` are two overlapping server entry points
with **divergent defaults** today: `VoiceServerConfig` defaults
`max_sessions=64` / `port=8080`, while `WebSocketSessionServerConfig` defaults
`max_sessions=10` / `port=8765`. Define a single ownership rule to resolve the
overlap:

- A mounted `VoiceApp` contributes ONLY its per-transport `config_factory`
  (how to build an `EasyConfig`/`Session` for a connection).
- `VoiceServerConfig` owns ALL process policy: host/port, capacity
  (`max_sessions`), draining timeouts, auth, CORS, metrics, health.

`from_app(VoiceApp(...))` therefore lifts the app's factory into the server and
applies the server's process policy on top; it does not inherit the
single-app's transport-server defaults.

## VoiceServerConfig

```python
@dataclass
class VoiceServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    public_base_url: str | None = None
    max_sessions: int = 64
    drain_timeout_s: float = 30.0
    force_shutdown_timeout_s: float = 10.0
    auth: AuthPolicy | None = None
    # Mirror of the AuthPolicy escape hatch: the ONLY way to bind a non-loopback
    # host with no token. Default keeps the unified guard armed.
    unsafe_allow_no_auth: bool = False
    cors_allowed_origins: tuple[str, ...] = ()
    enable_websocket: bool = True
    enable_webrtc: bool = True
    enable_health: bool = True
    enable_metrics: bool = True
    manifest_path: Path | None = None
    profile: str = "default"
```

Transport-specific config should be embedded or derived from existing transport
config dataclasses rather than duplicated field-by-field.

## Routing Framework Decision

Use `aiohttp` first.

Rationale:

- WebRTC signaling already uses `aiohttp`.
- The debugger server is aiohttp-backed.
- Avoid making FastAPI a hard dependency for all production servers.
- A future FastAPI adapter can wrap the same `VoiceServer` internals.

### WebRTC handler decoupling sub-task (M7)

The existing WebRTC route handlers are NOT cleanly reusable: they are bound to a
throwaway transport instance (`shim = WebRTCTransport(settings)` at
`webrtc.py:359`) and hardcode flat routes (`/offer`, `/config`, `/stats`). To
mount WebRTC under `VoiceServer`:

- Extract the WebRTC route handlers OFF `WebRTCTransport` so they no longer
  require a throwaway shim instance; the shared `VoiceServer` owns the route
  table and the per-connection `config_factory`.
- Decide flat-vs-namespaced paths. The bundled client targets the FLAT routes
  `/offer`, `/config`, `/stats` (`webrtc_client.html:301,425,455`). If the
  server adopts namespaced `/webrtc/*` routes, the bundled client and any
  `?token=` usage must migrate — account for that migration cost here.
- This decoupling + bundled-client migration is the reason M7 is flagged as the
  second-most under-sized milestone.

## Endpoint Set

> **This is a LOGICAL surface listing, NOT an aiohttp route manifest.** Not
> every entry is an aiohttp route. `/ws` (`websocket.py:162`) and
> `/twilio/media` (`examples/twilio_app.py:128`;
> `TwilioConnectionTransport` consumes a `websockets` `ServerConnection` at
> `twilio_media.py:918`) are raw `websockets.serve` listeners on **separate
> ports**, not aiohttp request handlers. The HTTP endpoints
> (`/health/*`, `/metrics`, `/manifest`, `/plan`, `/capabilities`,
> `/webrtc/*`, `/twilio/voice`) are aiohttp routes; the WebSocket/media
> entries are co-hosted raw-websockets listeners. `health()`/draining must
> span both the aiohttp listeners and the raw-websockets listeners.

Minimum server endpoints (aiohttp routes unless marked):

```text
GET  /health/live
GET  /health/ready
GET  /health
GET  /metrics
GET  /manifest
GET  /plan
GET  /capabilities
POST /webrtc/offer
GET  /webrtc/config
GET  /webrtc/stats
GET  /ws            # raw websockets.serve listener (NOT an aiohttp route)
```

Twilio endpoints after telephony server integration:

```text
POST /twilio/voice  # aiohttp route (returns TwiML)
GET  /twilio/media  # raw websockets.serve listener on a separate port (NOT aiohttp)
```

Twilio media is modeled exactly as `phase-1`'s two-port
`TwilioVoiceServerConfig` already does: an aiohttp app for `/twilio/voice`
(TwiML) plus a co-hosted raw-websockets listener for `/twilio/media` on its own
port. The unified `health()`/draining logic must observe both listeners.

## Auth Model

Add shared auth policies in `easycat.server.auth`. The non-loopback-requires-token
guard is a PROPERTY of this unified layer and is applied to BOTH WebSocket and
WebRTC transports.

> **This CLOSES a real unauthenticated-`0.0.0.0` voice endpoint, it does not
> preserve parity.** Today WebRTC enforces a loopback/token guard
> (`webrtc.py:347-351,924-927` raise `ValueError`), but WebSocket has **none**:
> `auth_token` defaults `None` (`websocket.py:84`), the host is overridable via
> `EASYCAT_WS_HOST` (`:93`), and `websocket_server_authorized` returns `True`
> whenever the token is `None` (`:100-111`). A `0.0.0.0` unauthenticated
> WebSocket voice endpoint is reachable in the current tree. The unified guard
> below fixes that.

```python
class AuthPolicy(Protocol):
    async def authorize(self, request: RequestLike) -> AuthResult: ...


@dataclass(frozen=True)
class AuthResult:
    allowed: bool
    reason: Literal["allowed", "missing", "invalid"]


@dataclass
class NoAuth:
    # The ONLY escape hatch for a non-loopback bind with no token.
    # Must be set explicitly; default keeps the guard armed.
    unsafe_allow_no_auth: bool = False


@dataclass
class BearerTokenAuth:
    token: str
    # Default-OFF: query tokens are a browser/dev opt-in only (see below).
    allow_query_token: bool = False
    unsafe_allow_no_auth: bool = False
```

The `unsafe_allow_no_auth: bool = False` field is a STRUCTURED field (also
mirrored on `VoiceServerConfig`), not prose-only — it is the single escape
hatch for binding to a non-loopback host without a token. The guard itself is a
property of the unified layer, evaluated for every transport at bind time.

Rules:

- Use constant-time comparison for secrets (`hmac.compare_digest`, already the
  standard across all five existing auth surfaces — reuse, do not reinvent).
- Support `Authorization: Bearer ...`.
- **Non-loopback binds require a token.** Binding to a non-loopback host with no
  token RAISES unless `unsafe_allow_no_auth=True` is set explicitly. This
  applies to BOTH the WebSocket and WebRTC paths via the unified layer.
- Do not log tokens.

### `?token=` query auth (default-off, breaking for the WS browser client)

`?token=` is UNCONDITIONAL today whenever a token is set, on BOTH transports:
WebRTC (`webrtc.py:826-834`) and WebSocket (`websocket.py:102-111`). Tightening
to `allow_query_token: bool = False` is the correct default-off posture, but it
is a BREAKING CHANGE:

- The bundled WebSocket browser client relies on `?token=`
  (`examples/ws_browser_client.html:91-93`) because browsers CANNOT set headers
  on the WebSocket handshake. With `allow_query_token=False` it stops
  authenticating.
- The bundled WebRTC client is UNAFFECTED — it sends `Authorization: Bearer`.

The new default applies to the EXISTING handlers too (not just the new
`VoiceServer` layer). Document the WS-client break in the deployment docs and
provide `allow_query_token=True` as a loopback/dev opt-in so the bundled WS
client keeps working locally.

### Server auth env var name

Standardize on the EXISTING shipped env var `EASYCAT_SERVE_TOKEN`
(`cli/serve.py:36,106`). Do NOT introduce `EASYCAT_SERVER_TOKEN` (one letter
apart — a silent rename hazard). In the manifest `bearer-env:NAME` model the
NAME is user-chosen, so the loader accepts any env var name; the canonical
example uses `bearer-env:EASYCAT_SERVE_TOKEN` for consistency with the CLI
default.

## Health and Readiness

### `/health/live`

Returns `200` if the process and event loop can respond.

### `/health/ready`

The readiness contract is SPLIT across milestones. Each check is annotated with
its owning milestone, and the manifest+plan checks are deferred to M6b and
gated behind the planner-vs-`create_session` parity test passing (see Provider/
Capability Planning).

Returns `200` when:

- server is not draining,                      _(M4)_
- active sessions are below capacity,          _(M4 — reads the minimal counter; see below)_
- route stack is ready,                         _(M4)_
- manifest/config loaded successfully,          _(M6b — deferred)_
- provider/capability plan has no blocking errors. _(M6b — deferred; gated on parity test)_

> **Capacity dependency (D5 / SEQ-1).** The "below capacity" check is M4-owned,
> but the full capacity machinery (`Semaphore`/active-set/draining) is **lifted
> into shared internals in M5**, not M4 (see "Capacity & draining are net-new").
> M4 therefore ships a **minimal capacity counter** (active count vs
> `max_sessions`) sufficient for this readiness check; M5 then replaces it with
> the lifted shared collaborator without changing the readiness contract. Do
> NOT implement the M4 readiness check against the M5 collaborator — it does not
> exist until M5.

Until M6b lands, `/health/ready` evaluates ONLY the M4 checks
(draining/capacity/route-ready). The manifest-loaded and plan-no-blocking-errors
checks are wired in M6b once the planner parity test passes, so readiness never
trusts a planner verdict that can diverge from `create_session`.

Returns `503` when draining, at capacity, or (after M6b) misconfigured.

### `/health`

Human/debug JSON:

```json
{
  "status": "ok",
  "state": "serving",
  "active_sessions": 3,
  "max_sessions": 64,
  "draining": false,
  "checks": {
    "manifest": {"status": "ok"},
    "providers": {"status": "ok"},
    "sessions": {"status": "ok"}
  }
}
```

## Graceful Shutdown

`VoiceServer.stop()` should:

1. Set the shared draining flag (state `draining`). This flag is owned by the
   net-new shared capacity/lifecycle collaborator, NOT by `SessionManager`.
2. Stop accepting new connections (close the capacity gate; reject new offers/
   handshakes).
3. Close HTTP/WebSocket listeners (both the aiohttp listeners and the raw
   `websockets.serve` listeners for `/ws` and `/twilio/media`).
4. Wait for active connection tasks up to `drain_timeout_s`, tracked via the
   shared active-session set lifted out of the serve helpers.
5. Stop remaining active sessions gracefully. `SessionManager` is a bare
   registry, so the shared collaborator iterates the active set and calls
   `session.stop()` per session; it does NOT delegate draining to
   `SessionManager` (which has no draining state). `SessionManager.stop_all()`
   may be used only as the final hard sweep once no connection handler can
   still add/remove sessions.
6. Escalate remaining sessions with `force=True` after `drain_timeout_s`.
7. Close runner/app resources.

Do not call `SessionManager.stop_all()` while active connection handlers can
still add/remove sessions without coordinating those tasks through the shared
collaborator's draining flag and active set.

## Metrics

Owning milestone: **M8** — "Server Metrics + Endpoints" (see roadmap; inserted
immediately after M7, which keeps the WebRTC number). Server metrics MUST be
added in the SAME PR that registers them, because the observability layer is a
hard allow-list — `_record_metric` and `sanitize_attributes` RAISE `ValueError`
on any unregistered metric name or attribute key
(`_observability.py:199,209-210`).

Add server metrics while preserving low-cardinality labels:

```text
easycat.server.requests.total
easycat.server.request.duration
easycat.server.sessions.rejected.total
easycat.server.connections.active
easycat.server.draining
```

> **Registration is mandatory and same-PR.** The new `easycat.server.*` metric
> names above MUST be added to `METRIC_DEFINITIONS` in `_observability.py`, and
> the new labels below MUST be added to `LOW_CARDINALITY_ATTRIBUTE_KEYS`, in the
> SAME PR that emits them. Add a test that the server emits through
> `sanitize_attributes` (i.e. that emission does not raise on the registered
> names/keys).

Safe labels:

```text
easycat.route          # NEW — must be added to LOW_CARDINALITY_ATTRIBUTE_KEYS
easycat.server_state   # NEW — must be added to LOW_CARDINALITY_ATTRIBUTE_KEYS
easycat.auth_result    # NEW — must be added to LOW_CARDINALITY_ATTRIBUTE_KEYS
easycat.transport      # ALREADY registered (_observability.py:59) — do NOT re-add
```

> **`easycat.route` is a PII/cardinality hazard.** The key-name allow-list
> cannot distinguish a route TEMPLATE from a raw path carrying user content.
> Constrain `easycat.route` to an ENUMERATED set of route templates
> (`/health/ready`, `/metrics`, `/webrtc/offer`, `/ws`, ...) and ASSERT the
> value is in that template set BEFORE recording. Never record a resolved/raw
> path.

Never include:

- session IDs,
- phone numbers,
- IP addresses,
- auth tokens,
- raw paths with user content,
- transcript text.

## Manifest-First Projects

Add a project manifest type called `ProjectManifest` or
`VoiceProjectManifest`. Do not expose another ambiguous top-level `Manifest`.

Example `easycat.toml`:

```toml
[project]
name = "support-voice-agent"

[server]
host = "0.0.0.0"
port = 8080
max_sessions = 64
# bearer-env:NAME references an env var by name (NAME is user-chosen). The
# loader reads the token from the environment; a literal token here is rejected.
auth = "bearer-env:EASYCAT_SERVE_TOKEN"

[voice.default]
transport = "webrtc"
agent = "python:app:create_agent"
stt = "openai/realtime"
tts = "openai"
vad = "silero"
debug = "light"

[voice.websocket]
transport = "websocket"
path = "/ws"

[voice.phone]
transport = "twilio"
stream_url = "wss://example.com/twilio/media"
```

> **Net-new: no loader exists today.** There is no `bearer-env`, no `tomllib`
> manifest parsing, and no `ProjectManifest` in the tree (grep-confirmed). The
> loader, the `bearer-env:NAME` grammar, and the secret rule below are all
> net-new in M6a.

Loader responsibilities:

- Discover path from `--manifest`, `EASYCAT_MANIFEST`, or `easycat.toml`.
- Resolve relative paths relative to the manifest directory.
- Validate without importing heavy provider/runtime SDKs.
- Resolve Python references such as `python:app:create_agent`.
- Convert selected profile to `EasyConfig`.
- Redact secrets for logs, debug bundles, and JSON output.

### Manifest secret rule (testable contract)

The secret rule is a TESTABLE contract, not prose-only:

- `auth`/`token` fields MUST match an env-reference grammar: `bearer-env:NAME`
  where `NAME` is a valid env var identifier. The loader resolves the token from
  the environment at load time.
- The loader MUST RAISE a coded error (e.g. `EASYCAT_Exxx`) when a field looks
  like a LITERAL secret instead of an env reference. Reuse
  `redaction._SECRET_RE` / `contains_unredacted_sensitive_text` to detect a
  literal-looking secret.
- Any echoed or dumped manifest (logs, `--json`, `/manifest`) routes through
  `redact_value` so a resolved token value never appears.

Acceptance tests:

- (a) a literal secret in `auth`/`token` is REJECTED with the coded error;
- (b) the `--json` dump and the `/manifest` endpoint show NO resolved token
  value (only the `bearer-env:NAME` reference or a redacted placeholder).

## Provider/Capability Planning

Net-new: `easycat.planning`, `ProviderPlan`, `ProviderSelection`, the
`python:module:function` agent resolver, transport string shortcuts, and the
`easycat plan` CLI do NOT exist today and are all introduced here (M6a/M6b).

Add `easycat.planning.ProviderPlan`:

```python
@dataclass(frozen=True)
class ProviderSelection:
    role: Literal[
        "stt",
        "tts",
        "vad",
        "transport",
        "agent",
        "noise_reducer",
        "echo_canceller",
    ]
    provider: str
    model: str | None
    config_type: str
    extra: str | None
    required_env: str | None
    capabilities: frozenset[str]


@dataclass(frozen=True)
class ProviderPlan:
    profile: str
    selected: dict[str, ProviderSelection]
    missing_env: tuple[str, ...]
    missing_extras: tuple[str, ...]
    warnings: tuple[str, ...]
```

Planner responsibilities:

- Resolve provider shortcut strings.
- Report missing env vars.
- Report missing optional extras.
- Detect incompatible provider/transport combinations.
- Expose JSON through `easycat plan --json` and `/plan`.
- Feed readiness checks (M6b; gated on the parity test below).

### Reuse narrative: only STT/TTS have a catalog

The earlier "prefer extracting/shared catalog metadata" guidance was wrong for 5
of 7 roles. Correct sourcing of `extra`/`required_env`/`capabilities`:

- **stt / tts** — REUSE the `ProviderCatalog` metadata
  (`_provider_catalog.py:1-2,285-353`). This is the only role pair with a static
  catalog.
- **vad** — NET-NEW declarative metadata. There is no catalog; VAD resolves by
  try/except with extras embedded in an error string (`vad/factory.py:91-151`).
- **transport** — NET-NEW declarative metadata. Resolution is config-type
  dispatch (`config/_factory.py:110-139`), not a catalog.
- **agent** — NET-NEW declarative metadata (the `python:module:function`
  resolver is itself net-new in M6a).
- **noise_reducer / echo_canceller** — NET-NEW declarative metadata. Only
  hardcoded extras exist (`noise_reduction.py:40,96`,
  `echo_cancellation.py:11,90`).
- **`ProviderSelection.capabilities`** — has NO static source.
  `validation/provider_capabilities.py:2-5` is a LIVE-derived report, not a
  static table; capabilities for the plan must be declared net-new.

So the planner is `stt/tts via catalog; vad/transport/agent/noise/echo require
NET-NEW declarative metadata`. The large net-new surface (5 of 7 roles) is
exactly why the divergence risk (R6) is amplified and why the parity test below
is REQUIRED.

### Required acceptance: planner-vs-`create_session` parity test

Because 5 of 7 roles hand-roll resolution outside any catalog, the planner can
silently diverge from what `create_session` actually does. A PARITY TEST is a
required gate: the planner verdict (provider/extra/env/blocking-error) MUST match
the `create_session` outcome for EVERY one of the 7 roles. The manifest-loaded
and plan-no-blocking-errors readiness checks (M6b) are GATED behind this parity
test passing — readiness never trusts a planner verdict that can diverge.

Avoid duplicating factory rules where a shared source exists (stt/tts), but do
NOT pretend the other five roles have shared metadata to extract.

## CLI Changes

Add:

```bash
easycat serve --manifest easycat.toml
easycat serve --profile browser
easycat serve --host 0.0.0.0 --port 8080
easycat plan
easycat plan --json
```

`easycat serve` should eventually become the process entry point for both dev
and production, with `VoiceApp.run(...)` remaining the programmatic local API.

### `--json` envelope coverage for new commands

`easycat plan --json` must emit the standard JSON envelope (`schema_version=1`).
The envelope guard `tests/cli/test_json_schema.py` is HAND-WRITTEN with NO
registry walk, and `tests/cli/test_app.py:49-115` walks commands but only checks
`--help` — so a new `--json` command is NOT covered automatically. Therefore:

- Add explicit `tests/cli/test_json_schema.py` cases for `easycat plan --json`
  (envelope shape + `schema_version=1`).
- Ideally add a coverage test that FAILS when any `--json` command lacks an
  envelope assertion, so future `--json` commands cannot slip past the guard.

## Files To Add

- `src/easycat/server/*`
- `src/easycat/project/*`
- `src/easycat/planning/*`
- `tests/server/*`
- `tests/project/*`
- `tests/planning/*`

## Files To Update

- `src/easycat/cli/serve.py`
- `src/easycat/cli/_app.py`
- `src/easycat/_observability.py` — REQUIRED: register the new `easycat.server.*`
  metric names in `METRIC_DEFINITIONS` and the new labels
  (`easycat.server_state`, `easycat.auth_result`, `easycat.route`) in
  `LOW_CARDINALITY_ATTRIBUTE_KEYS`, in the same PR that emits them
  (`easycat.transport` is already registered — do not re-add).
- `tests/cli/test_json_schema.py` — add explicit envelope-assertion cases for
  `easycat plan --json` (and any other new `--json` command in this phase).
- provider factory/catalog modules if metadata extraction is needed (stt/tts
  only — see Provider/Capability Planning).
- transport modules — lift inline capacity/draining out of `webrtc.py` /
  `websocket.py` into shared `VoiceServer` internals; extract WebRTC route
  handlers off `WebRTCTransport`.
- `README.md`
- `docs/deployment/production-servers.md`
- `docs/deployment/docker.md`
- `docs/observability.md`
- `docs/reference/session-lifecycle.md`
- `docs/reference/easyconfig.md`

> The four endpoints `/metrics`, `/manifest`, `/plan`, `/capabilities` have an
> OWNING milestone: **M8** (Server Metrics + Endpoints). Each needs an
> acceptance row (200 + JSON shape) in `acceptance-matrix.md`.

## Acceptance Criteria

- `VoiceServer.from_app(VoiceApp(...))` can serve WebSocket or WebRTC sessions.
- `VoiceServer.from_manifest("easycat.toml")` loads a profile and serves it.
- `/health/live`, `/health/ready`, and `/health` return stable JSON.
- Readiness returns 503 while draining or at capacity (M4 checks); after M6b,
  also returns 503 when the manifest fails to load or the plan has blocking
  errors.
- Bearer auth accepts valid tokens and rejects invalid tokens.
- A non-loopback WebSocket bind with NO token and WITHOUT `unsafe_allow_no_auth`
  RAISES (the unified guard, applied to both WS and WebRTC).
- A literal secret in a manifest `auth`/`token` field is rejected with a coded
  error; the `--json`/`/manifest` dump shows no resolved token value.
- Tokens and PII are not exposed in metrics or JSON diagnostics.
- `easycat.route` only ever records an enumerated route template, never a raw
  path.
- Graceful shutdown stops accepting sessions and drains active sessions through
  the shared collaborator (not `SessionManager`).
- Provider plan reports missing env vars/extras without instantiating providers.
- The planner verdict matches the `create_session` outcome for every one of the
  7 roles (parity test); manifest/plan readiness checks are gated on it.
- `GET /metrics`, `/manifest`, `/plan`, `/capabilities` each return 200 with the
  expected JSON shape.
- `easycat plan --json` emits the standard envelope (`schema_version=1`).
- Existing transport helpers keep working or delegate to shared internals.

## Suggested PR Slice

These map to the roadmap milestones (VoiceServer spans **M4–M8**); M6 is split
into M6a/M6b (highest-risk milestone), WebRTC keeps the number **M7** (the
second-most under-sized milestone), and a new **M8** (server metrics + read-only
endpoints) is inserted after M7. The numbers below match the roadmap's
Dependency Map and per-milestone sections exactly.

1. **M4** — Add `easycat.server` skeleton with health endpoints, the bare
   `SessionManager` registry, a **minimal capacity counter** for readiness, and
   lifecycle tests. M4's `/health/ready` covers serving/draining/capacity/
   route-ready ONLY; it must NOT import the planner.
2. **M5** — Add the unified `AuthPolicy` (non-loopback guard + `unsafe_allow_no_auth`
   + `allow_query_token`) shared by WebSocket and WebRTC, WebSocket route
   support, then LIFT the inline capacity/`Semaphore`/draining logic out of the
   WebRTC/WebSocket serve helpers into shared `VoiceServer` internals, plus
   graceful shutdown + forced escalation. (Auth, the capacity lift, and graceful
   shutdown are all M5.)
3. **M6a** — Project manifest loader + `python:` agent resolver +
   transport-registry metadata + `easycat.toml`→`EasyConfig` profile conversion
   + the testable secret rule/redaction.
4. **M6b** — `ProviderPlan` (stt/tts via catalog; vad/transport/agent/noise/echo
   as net-new declarative metadata) + `easycat plan --json` + `/plan` +
   readiness wiring (manifest-loaded + plan-no-blocking-errors), gated behind the
   planner-vs-`create_session` parity test.
5. **M7** — Integrate WebRTC routes (extract handlers off `WebRTCTransport`;
   decide flat-vs-namespaced paths + bundled-client/`?token=` migration).
6. **M8 (Server Metrics + Endpoints)** — Register `easycat.server.*` names + the
   three new labels in `_observability.py` and emit them; complete and add
   acceptance rows for `/metrics`, `/manifest`, `/plan`, `/capabilities`.
