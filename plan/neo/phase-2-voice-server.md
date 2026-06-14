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

Reuse:

- `Session` as the per-conversation runtime.
- `create_session(EasyConfig)` as the provider/session construction path.
- `SessionManager` as the multi-session lifecycle registry.
- WebRTC config server helper as the prototype for health, auth, stats,
  session limits, and per-offer sessions.
- WebSocket config server helper for per-client WebSocket sessions.
- Runtime health-check capability detection and session health checkers.
- Existing provider catalogs for STT/TTS provider metadata.
- Existing observability module and safe attribute policy for metrics.
- Existing signal/shutdown utilities.

## New Package Layout

```text
src/easycat/server/
  __init__.py
  auth.py
  config.py
  context.py
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

## VoiceServer API

```python
class VoiceServer:
    def __init__(
        self,
        config: VoiceServerConfig | None = None,
        *,
        session_factory: Callable[[ConnectionContext], EasyConfig | Session] | None = None,
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
    async def serve_forever(self, stop_event: asyncio.Event | None = None) -> None: ...
    async def stop(self, *, force: bool = False) -> None: ...
    def run(self) -> None: ...
    async def health(self) -> VoiceServerHealth: ...
```

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

## Endpoint Set

Minimum server endpoints:

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
GET  /ws
```

Twilio endpoints after telephony server integration:

```text
POST /twilio/voice
GET  /twilio/media
```

## Auth Model

Add shared auth policies in `easycat.server.auth`:

```python
class AuthPolicy(Protocol):
    async def authorize(self, request: RequestLike) -> AuthResult: ...

@dataclass(frozen=True)
class AuthResult:
    allowed: bool
    reason: Literal["allowed", "missing", "invalid"]

@dataclass
class NoAuth:
    ...

@dataclass
class BearerTokenAuth:
    token: str
    allow_query_token: bool = False
```

Rules:

- Use constant-time comparison for secrets.
- Support `Authorization: Bearer ...`.
- Allow `?token=` only for browser/dev compatibility when explicitly enabled.
- Do not log tokens.
- Require auth for non-loopback production binds unless explicitly disabled.

## Health and Readiness

### `/health/live`

Returns `200` if the process and event loop can respond.

### `/health/ready`

Returns `200` when:

- manifest/config loaded successfully,
- provider/capability plan has no blocking errors,
- server is not draining,
- active sessions are below capacity,
- route stack is ready.

Returns `503` when draining, at capacity, or misconfigured.

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

1. Set state to `draining`.
2. Stop accepting new connections.
3. Close HTTP/WebSocket listeners.
4. Wait for active connection tasks up to `drain_timeout_s`.
5. Stop active sessions gracefully through `SessionManager`.
6. Escalate remaining sessions with `force=True` after timeout.
7. Close runner/app resources.

Do not call `SessionManager.stop_all()` while active connection handlers can
still add/remove sessions without coordinating those tasks.

## Metrics

Add server metrics while preserving low-cardinality labels:

```text
easycat.server.requests.total
easycat.server.request.duration
easycat.server.sessions.rejected.total
easycat.server.connections.active
easycat.server.draining
```

Safe labels:

```text
easycat.route
easycat.server_state
easycat.auth_result
easycat.transport
```

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
auth = "bearer-env:EASYCAT_SERVER_TOKEN"

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

Loader responsibilities:

- Discover path from `--manifest`, `EASYCAT_MANIFEST`, or `easycat.toml`.
- Resolve relative paths relative to the manifest directory.
- Validate without importing heavy provider/runtime SDKs.
- Resolve Python references such as `python:app:create_agent`.
- Convert selected profile to `EasyConfig`.
- Redact secrets for logs, debug bundles, and JSON output.

## Provider/Capability Planning

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
- Feed readiness checks.

Avoid duplicating factory rules. Prefer extracting/shared catalog metadata.

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
- `src/easycat/_observability.py`
- provider factory/catalog modules if metadata extraction is needed
- transport modules if route handlers are shared with `VoiceServer`
- `README.md`
- `docs/deployment/production-servers.md`
- `docs/deployment/docker.md`
- `docs/observability.md`
- `docs/reference/session-lifecycle.md`
- `docs/reference/easyconfig.md`

## Acceptance Criteria

- `VoiceServer.from_app(VoiceApp(...))` can serve WebSocket or WebRTC sessions.
- `VoiceServer.from_manifest("easycat.toml")` loads a profile and serves it.
- `/health/live`, `/health/ready`, and `/health` return stable JSON.
- Readiness returns 503 while draining or at capacity.
- Bearer auth accepts valid tokens and rejects invalid tokens.
- Tokens and PII are not exposed in metrics or JSON diagnostics.
- Graceful shutdown stops accepting sessions and drains active sessions.
- Provider plan reports missing env vars/extras without instantiating providers.
- Existing transport helpers keep working or delegate to shared internals.

## Suggested PR Slice

1. Add `easycat.server` skeleton with health endpoints and lifecycle tests.
2. Add shared auth and WebSocket route support.
3. Add graceful shutdown and capacity handling.
4. Add project manifest loader.
5. Add provider/capability planning and `easycat plan`.
6. Integrate WebRTC routes.
7. Add metrics.
