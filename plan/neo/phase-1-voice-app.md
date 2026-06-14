# Phase 1 — VoiceApp, Browser-First Dev, Unified Modes

Status: active implementation plan.

## Goal

Introduce a product-level app object that becomes the primary first-run surface:

```python
from easycat import VoiceApp

app = VoiceApp(agent=agent)
app.run("browser")
```

Mode changes should be simple:

```python
app.run("local")
app.run("websocket")
app.run("twilio")
```

## Why This Matters

Today EasyCat has a strong low-level shape: `EasyConfig`, `create_session`,
transport configs, provider configs, and session helpers. That is powerful, but
new users should not have to understand session construction before they hear a
bot speak.

`VoiceApp` gives developers one noun for their product and preserves the
existing lower-level escape hatches.

## Existing Building Blocks

Reuse these rather than duplicating them:

- `EasyConfig.mic()` for local microphone defaults.
- `EasyConfig.browser()` for WebRTC/browser defaults and echo cancellation.
- `EasyConfig.phone()` for Twilio/phone defaults.
- `create_session(config)` for concrete provider/session construction.
- `run_session(session)` for local blocking runs.
- `run_webrtc_config_server(...)` / `serve_webrtc_config_sessions(...)` for
  browser WebRTC.
- `run_websocket_config_server(...)` / `serve_websocket_config_sessions(...)`
  for per-client WebSocket sessions.
- `SessionManager` for multi-session lifecycle.
- `TwilioConnectionTransport`, `TwilioStreamTokenStore`, and TwiML helpers for
  telephony server extraction.

## Public API

Add:

```python
from easycat import VoiceApp
```

Recommended class sketch:

```python
VoiceMode = Literal["local", "browser", "websocket", "twilio"]

@dataclass
class VoiceApp:
    agent: Any | None = None
    config: EasyConfig | None = None
    config_factory: Callable[[Any], EasyConfig] | None = None
    default_mode: VoiceMode = "browser"
    dev: bool = False

    def session(self, mode: VoiceMode = "local", **kwargs: Any) -> Session: ...
    async def serve(self, mode: VoiceMode = "browser", **kwargs: Any) -> None: ...
    def run(self, mode: VoiceMode = "browser", **kwargs: Any) -> None: ...
```

Aliases:

| Alias | Canonical |
|---|---|
| `mic` | `local` |
| `ws` | `websocket` |
| `phone` | `twilio` |

## Config Construction Rules

`VoiceApp` should support three construction styles:

### 1. High-level app fields

```python
VoiceApp(agent=agent, stt="openai/realtime", tts="openai")
```

The app chooses the relevant `EasyConfig` preset per mode.

### 2. Static `EasyConfig`

```python
VoiceApp(config=EasyConfig.browser(agent=agent, debug="light"))
```

For per-connection modes, clone the config with a new transport. Never mutate or
reuse a transport-bearing config across concurrent sessions.

### 3. Per-transport config factory

```python
VoiceApp(
    config_factory=lambda transport: EasyConfig.browser(
        transport=transport,
        agent=agent,
        debug="light",
    )
)
```

This is the advanced path and should be the most explicit.

## Mode Implementations

### Local mode

Implementation:

1. Build `EasyConfig.mic(...)` unless the user supplied config/factory.
2. Call `create_session(config)`.
3. Call `run_session(session)`.

### Browser mode

Browser mode should mean WebRTC + bundled browser client.

Implementation:

1. Build a `WebRTCTransportConfig(host, port, auth_token, ...)`.
2. Build a per-transport config factory.
3. Delegate to `run_webrtc_config_server(factory, transport_config)`.
4. Print the browser URL.
5. Preserve non-loopback token requirement.

Important: browser mode should be multi-session by default, using the existing
WebRTC config server helper rather than a single session with one transport.

### WebSocket mode

Implementation:

1. Build `WebSocketSessionServerConfig(host, port, auth_token, max_sessions)`.
2. Build a per-connection config factory around `WebSocketConnectionTransport`.
3. Delegate to `run_websocket_config_server(...)`.

Initial scope: raw WebSocket server. A browser WebSocket static client can be a
follow-up if product demand justifies it.

### Twilio mode

Twilio needs a reusable server helper extracted from the current example shape.

Add:

```text
src/easycat/telephony/server.py
```

Suggested API:

```python
@dataclass
class TwilioVoiceServerConfig:
    host: str = "0.0.0.0"
    media_port: int = 8766
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    stream_url: str | None = None
    stream_token_secret: str | None = None
    enable_dtmf_aggregator: bool = True
    enable_voicemail_detector: bool = True

async def serve_twilio_voice_app(
    config_factory: Callable[[TwilioConnectionTransport], EasyConfig],
    config: TwilioVoiceServerConfig,
) -> None: ...
```

Then `VoiceApp.run("twilio")` delegates to that helper.

Keep Twilio HTTP/TwiML orchestration above the transport layer. Do not make the
transport class own the full app server.

## CLI Changes

Migrate `easycat serve` to `VoiceApp`:

```bash
easycat serve --mode browser
easycat serve --mode websocket
easycat serve --mode local
easycat serve --mode twilio
```

Defaults:

- `--mode browser`
- `--host 127.0.0.1`
- `--port 8080`
- non-loopback host requires token

Preserve current URL-printing behavior.

## Files To Add

- `src/easycat/voice_app.py`
- `src/easycat/telephony/server.py`
- `examples/voice_app.py`
- `tests/test_voice_app.py`
- `tests/transports/test_voice_app_modes.py`
- `tests/telephony/test_voice_app_twilio.py`

## Files To Update

- `src/easycat/__init__.py`
- `src/easycat/_public_api.py`
- `src/easycat/cli/serve.py`
- `src/easycat/cli/_app.py`
- `docs/public-api.md`
- `docs/browser-playground.md`
- `docs/reference/easyconfig.md`
- `README.md`
- `examples/README.md`
- public API snapshot tests
- CLI serve tests

## Acceptance Criteria

- `from easycat import VoiceApp` works without importing heavy provider SDKs.
- `VoiceApp(agent=agent).run("local")` delegates to `EasyConfig.mic` +
  `create_session` + `run_session`.
- `VoiceApp(agent=agent).run("browser")` starts the WebRTC browser server.
- `VoiceApp(agent=agent).run("websocket")` starts a per-client WebSocket server.
- `VoiceApp(agent=agent).run("twilio")` starts the extracted Twilio server
  helper or raises a clear optional-extra error.
- `easycat serve` default behavior remains browser/WebRTC.
- Non-loopback `easycat serve` still requires a token.
- Static configs are not mutated or reused unsafely across per-connection
  sessions.
- Docs and examples show `VoiceApp` as the headline path.

## Suggested PR Slice

1. Add `VoiceApp` with local/browser/websocket modes and tests.
2. Migrate `easycat serve` to use `VoiceApp` while preserving behavior.
3. Export public API and update docs.
4. Extract Twilio server helper.
5. Add `VoiceApp.run("twilio")` and Twilio docs/tests.
