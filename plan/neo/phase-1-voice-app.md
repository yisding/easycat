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

Reuse these rather than duplicating them. The import surface is **not**
uniform — some are public top-level exports and some are internal-only. The
column below records where each one actually lives, because a reader-facing
`from easycat import <name>` against an internal symbol fails the public-import
gate (`tests/test_public_api.py:379-398`).

- `EasyConfig.mic()` for local microphone defaults. (public)
- `EasyConfig.browser()` for WebRTC/browser defaults and echo cancellation.
  (public)
- `EasyConfig.phone()` for Twilio/phone defaults. (public)
- `create_session(config)` for concrete provider/session construction. (public)
- `run_session(session)` for local blocking runs. **INTERNAL** — lives in
  `easycat.helpers` (`helpers.py:145`); it is NOT a top-level export
  (`hasattr(easycat, "run_session")` is `False`). Import it internally as
  `from easycat.helpers import run_session`, never as `from easycat import
  run_session`.
- `run_webrtc_config_server(...)` / `serve_webrtc_config_sessions(...)` for
  browser WebRTC. (public — present in `_public_api.py:135-141`)
- `run_websocket_config_server(...)` / `serve_websocket_config_sessions(...)`
  for per-client WebSocket sessions. **INTERNAL** — these are ABSENT from
  `_public_api.py` (unlike their WebRTC equivalents). Import them from their
  defining module, not from the package root.
- `SessionManager` for multi-session lifecycle. **Note:** it is a bare
  `add/remove/stop_all/connection` registry (`session_manager.py:18-105`) with
  NO `max_sessions`, no `__len__`, and no draining state. Capacity and draining
  live inline in the serve helpers today (see Phase 2); do not assume
  `SessionManager` owns them.
- `TwilioConnectionTransport`, `TwilioStreamTokenStore`, and TwiML helpers for
  telephony server extraction. (public)

There is **no** config clone / `with_transport()` / `replace_transport()`
helper anywhere in `config/`. The only safe per-connection path is a
per-transport `config_factory` (see Config Construction Rules below);
`dataclasses.replace` is unsafe for transport-bearing configs because it shares
the grouped sub-configs by reference.

## Public API

Add:

```python
from easycat import VoiceApp
```

Recommended class sketch:

```python
VoiceMode = Literal["local", "browser", "websocket", "twilio"]

# Canonical per-transport factory shape. `TransportT` is the concrete
# connection transport for the chosen mode:
#   browser    -> WebRTCTransport
#   websocket  -> WebSocketConnectionTransport
#   twilio     -> TwilioConnectionTransport
# There is no abstract `ConnectionContext` — the helpers already take the
# transport-specific argument.

class VoiceApp:
    def __init__(
        self,
        agent: Any | None = None,
        *,
        config: EasyConfig | None = None,
        config_factory: Callable[[Any], EasyConfig] | None = None,
        dev: bool = False,
        **config_kwargs: Any,  # high-level EasyConfig fields (see allow-list)
    ) -> None: ...

    def session(self, mode: VoiceMode | None = None, **kwargs: Any) -> Session: ...
    async def serve(self, mode: VoiceMode | None = None, **kwargs: Any) -> None: ...
    def run(self, mode: VoiceMode | None = None, **kwargs: Any) -> None: ...
```

This deliberately replaces the earlier 5-field `@dataclass` sketch. That sketch
could not accept the documented Construction Style #1 call
`VoiceApp(agent=..., stt="openai/realtime", tts="openai")` — `stt=`/`tts=` are
not dataclass fields, so the call raised `TypeError`. The constructor above
forwards high-level `EasyConfig` fields through `**config_kwargs` (governed by
the allow-list below). The dead `default_mode` field is **deleted**: it was
never read by any method (one grep hit repo-wide), and each of `session()` /
`serve()` / `run()` resolves its own per-method default (see Event Loop
Ownership). The `mode` argument is `VoiceMode | None = None`; each method
substitutes its own default when `None` is passed.

Aliases:

| Alias | Canonical |
|---|---|
| `mic` | `local` |
| `ws` | `websocket` |
| `phone` | `twilio` |

## Construction Precedence & Field Allow-List

`VoiceApp` accepts inputs three mutually-exclusive ways. A test enforces both
the allow-list and the mutual-exclusion rule below.

### Field allow-list

High-level `EasyConfig` fields passed via `**config_kwargs` are forwarded into
the chosen mode's preset (`EasyConfig.mic()/.browser()/.phone()`). Fields that
`VoiceApp` owns are never forwarded.

| Field | Owner / behavior |
|---|---|
| `agent` | forwarded into the preset |
| `stt` | forwarded into the preset |
| `tts` | forwarded into the preset |
| `vad` | forwarded into the preset |
| `debug` | forwarded into the preset |
| mode-appropriate transport fields | forwarded into the preset (e.g. `host`/`port`/`auth_token` for server modes) |
| mode-appropriate auth fields | forwarded into the preset |
| `dev` | **owned by `VoiceApp`** — controls the dev/debugger opt-in, never forwarded into the preset |

Any `**config_kwargs` key outside this allow-list is a `ValueError` (so a typo
or a misplaced server-policy field fails loudly rather than silently dropping).

### Mutual exclusion

`config`, `config_factory`, and high-level `**config_kwargs` are **mutually
exclusive**. Supplying more than one raises `ValueError` naming the conflict.

Worked example — this is a real, previously-undefined conflict and must raise:

```python
# `EasyConfig.browser(agent=b)` is valid (easy.py:891-916), so the two agents
# silently disagree unless we reject the combination outright.
VoiceApp(agent=a, config=EasyConfig.browser(agent=b))
# -> ValueError: cannot pass both `config` and high-level field `agent`
```

The precedence is therefore not "last wins" or "config overrides fields" — it
is "exactly one input style per app," validated at construction time.

## Event Loop Ownership & session() Semantics

One loop-ownership rule spans `VoiceApp` and `VoiceServer`:

- `run()` is the **only** method that calls `asyncio.run()`. It is the sole
  loop owner; nothing else creates or enters an event loop.
- `serve()` is the async coroutine entry point. The async verb is `serve()` on
  **both** `VoiceApp` and `VoiceServer` (the asymmetric `serve_forever` is
  dropped, or aliased to `serve`), so callers compose them without guessing the
  name.
- `VoiceServer` composes a mounted `VoiceApp` through its `config_factory`
  **only** — it never calls `VoiceApp.run()`, which would nest `asyncio.run()`
  inside an already-running loop.

`session(mode=...)` returns an **un-started, caller-owned `Session`** (matching
`create_session` at `_factory.py:351`) — the caller is responsible for starting
and stopping it. Because a `Session` is a single object, `session()` is
restricted to single-session modes:

- `session("local")` → returns one un-started `Session`.
- `session("browser")` / `session("websocket")` / `session("twilio")` →
  `ValueError`. These are multi-session modes (browser is multi-session by
  default, see Browser mode below); there is no single `Session` to hand back,
  so use `serve()` / `run()` instead.

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

A static, transport-bearing `EasyConfig` is **only** valid for the
single-session `local` mode. It CANNOT be safely cloned per connection:

- There is no `with_transport()` / `replace_transport()` / clone helper in
  `config/`.
- `dataclasses.replace` does NOT isolate sessions: it shares the grouped
  sub-config instances (`observability`, `audio_processing`, `session_policy`)
  by reference. This is verified empirically — mutating a replaced config's
  `observability.debug` flips the original. (The same shared-reference / InitVar
  proxy hazard means a naive replace also leaks per-connection auth/transport
  state between concurrent sessions.)

Therefore: for every per-connection mode (`browser`, `websocket`, `twilio`) a
static `config` is rejected; those modes **require** `config_factory` (style #3
below). A static `config` is accepted for `local` only.

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

This is the advanced path and the only safe per-connection mechanism. The
factory type is the canonical per-transport shape
`Callable[[TransportT], EasyConfig]`, where `TransportT` is the concrete
connection transport for the mode (there is no abstract `ConnectionContext` —
it does not exist in the tree, and the serve helpers already take the
transport-specific argument):

| Mode | Factory signature |
|---|---|
| browser | `Callable[[WebRTCTransport], EasyConfig]` |
| websocket | `Callable[[WebSocketConnectionTransport], EasyConfig]` |
| twilio | `Callable[[TwilioConnectionTransport], EasyConfig]` |

These signatures match `architecture-boundaries.md` and `phase-2-voice-server.md`
exactly.

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
2. Build a per-transport config factory typed
   `Callable[[WebRTCTransport], EasyConfig]`.
3. Delegate to `run_webrtc_config_server(factory, transport_config)`.
4. Print the browser URL.
5. Preserve the non-loopback token requirement (token from
   `EASYCAT_SERVE_TOKEN`; WebRTC already enforces this at
   `webrtc.py:347-351,924-927`).

Important: browser mode should be multi-session by default, using the existing
WebRTC config server helper rather than a single session with one transport.

### WebSocket mode

Implementation:

1. Build `WebSocketSessionServerConfig(host, port, auth_token, max_sessions)`.
2. Build a per-connection config factory typed
   `Callable[[WebSocketConnectionTransport], EasyConfig]`.
3. Delegate to `run_websocket_config_server(...)` (an INTERNAL helper — see
   Existing Building Blocks; it is not a top-level export).

Note: the WebSocket server is a **raw `websockets.serve` listener**
(`websocket.py:162`), not an aiohttp route. This matters when `VoiceServer`
later co-hosts WebSocket alongside aiohttp routes — the unified endpoint table
in Phase 2 is a *logical* surface listing, not an aiohttp route manifest. Keep
this consistent with phase-2's logical-endpoint-table clarification.

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

async def serve_twilio_voice_app(
    config_factory: Callable[[TwilioConnectionTransport], EasyConfig],
    config: TwilioVoiceServerConfig,
) -> None: ...
```

`enable_dtmf_aggregator` / `enable_voicemail_detector` are deliberately
**NOT** on `TwilioVoiceServerConfig`. They already exist on `TelephonyConfig`
(default `False`, `config/easy.py:405-406`) and are set per-connection via
`TelephonyConfig(...)` (`examples/twilio_app.py:74-77`). Putting them on a
server config would duplicate/shadow the existing fields and invert their
default to `True`. Instead, the helper threads per-connection telephony
behavior through the `config_factory`, which builds
`EasyConfig.phone(telephony=TelephonyConfig(...))`:

```python
def config_factory(transport: TwilioConnectionTransport) -> EasyConfig:
    return EasyConfig.phone(
        transport=transport,
        agent=agent,
        telephony=TelephonyConfig(
            enable_dtmf_aggregator=True,      # opt in per-connection
            enable_voicemail_detector=True,
        ),
    )
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
- non-loopback host requires a token, read from the **existing shipped** env
  var `EASYCAT_SERVE_TOKEN` (`cli/serve.py:36,106`) — NOT `EASYCAT_SERVER_TOKEN`.
  Do not silently rename it while migrating `serve` through `VoiceApp`.

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
- **Raise the public-API `__all__` cap `94 → 95`** in
  `tests/test_public_api.py:126` (currently asserts `<= 94`; the surface is
  exhausted at 94/94). Adding top-level `VoiceApp` requires the **triple-lock**
  in the same PR: update `__all__` **and** `LAZY_EXPORTS` **and**
  `docs/public-api.md` (plus the `PUBLIC_API_SNAPSHOT`). Note: only the
  top-level `VoiceApp` export counts against this cap — `CostBudget` /
  `LatencyBudget` / `VoiceServer` / `EvalRunner` are submodule exports
  (`easycat.budgets`, `easycat.server`, `easycat.evals`) and do NOT count
  against the top-level cap. See R13 in the risk register.

## Acceptance Criteria

- `from easycat import VoiceApp` works without importing heavy provider SDKs.
- `VoiceApp(agent=agent).run("local")` delegates to `EasyConfig.mic` +
  `create_session` + `run_session`.
- `VoiceApp(agent=agent).run("browser")` starts the WebRTC browser server.
- `VoiceApp(agent=agent).run("websocket")` starts a per-client WebSocket server.
- `VoiceApp(agent=agent).run("twilio")` starts the extracted Twilio server
  helper or raises a clear optional-extra error.
- `easycat serve` default behavior remains browser/WebRTC.
- Non-loopback `easycat serve` requires a token — and this guard must hold for
  **both** the browser/WebRTC path AND the websocket path. Today only WebRTC
  enforces it (`webrtc.py:347-351,924-927`); the websocket path has NO
  loopback guard (`websocket.py:84,93,100-111`), so a `0.0.0.0` unauthenticated
  voice endpoint is reachable there. `VoiceApp` must add the guard on the
  websocket path (or defer to the unified `AuthPolicy` layer introduced in
  Phase 2), with `unsafe_allow_no_auth` as the only structured escape hatch. The
  token is read from `EASYCAT_SERVE_TOKEN`.
- Static configs are not mutated or reused unsafely across per-connection
  sessions. Concretely: per-connection modes reject a static `config` and
  require `config_factory` (no clone helper exists; `dataclasses.replace`
  shares sub-configs by reference).
- Docs and examples show `VoiceApp` as the headline path.

## Suggested PR Slice

1. Add `VoiceApp` with local/browser/websocket modes and tests.
2. Migrate `easycat serve` to use `VoiceApp` while preserving behavior.
3. Export public API and update docs.
4. Extract Twilio server helper.
5. Add `VoiceApp.run("twilio")` and Twilio docs/tests.
