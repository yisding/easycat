# Neo Acceptance Matrix

Status: active review checklist.

Use this matrix during implementation reviews. A milestone is not done until its
behavior, tests, docs, and migration notes are represented here.

## Phase 1 — VoiceApp

| Capability | Acceptance Criteria | Evidence |
|---|---|---|
| Public import | `from easycat import VoiceApp` is lazy and does not import heavy provider SDKs. | Public API test; import-cost/lazy import test. |
| Local mode | `VoiceApp(agent=agent).session("local")` builds a local-session config and `run("local")` delegates to `run_session`. | Unit test with monkeypatched `create_session`/`run_session`. |
| Browser mode | `run("browser")` starts WebRTC config server with per-offer session factory. | Unit test with monkeypatched `run_webrtc_config_server`; WebRTC focused test. |
| WebSocket mode | `run("websocket")` starts WebSocket config server with per-client transport. | Unit test with monkeypatched `run_websocket_config_server`. |
| Static config safety | A static `EasyConfig` is cloned/replaced per connection; transport instances are not reused unsafely. | Concurrency/config clone unit test. |
| Factory mode | `config_factory` receives the connection transport and is called once per session. | Unit test. |
| CLI default | `easycat serve` still defaults to browser mode. | CLI test. |
| CLI security | Non-loopback `easycat serve` still requires token. | CLI test. |
| Twilio mode | `run("twilio")` starts reusable Twilio helper or raises clear optional-extra error. | Telephony fake tests. |
| Docs | README, browser playground docs, examples, and public API docs show `VoiceApp`. | Docs tests / markdown guard. |

## Phase 2 — VoiceServer

| Capability | Acceptance Criteria | Evidence |
|---|---|---|
| Server skeleton | `VoiceServer.from_app(VoiceApp(...))` starts and stops without leaking sessions. | `tests/server/test_voice_server_lifecycle.py`. |
| Health live | `/health/live` returns 200 while process can respond. | Aiohttp route test. |
| Health ready | `/health/ready` returns 200 when serving and 503 while draining/at capacity. | Health route tests. |
| Auth | Bearer token accepted; missing/invalid token rejected; query-token compatibility only when enabled. | Auth tests. |
| Secret hygiene | Tokens do not appear in JSON diagnostics or metrics labels. | Auth/metrics tests. |
| Capacity | New sessions are rejected after max active sessions. | Capacity test. |
| Graceful shutdown | Server stops accepting new sessions, drains active sessions, then escalates after timeout. | Shutdown test with fake sessions. |
| Manifest loading | `easycat.toml` discovered, parsed, resolved relative to manifest dir, and converted to `EasyConfig`. | Project loader tests. |
| Provider plan | Missing env/extras reported without provider instantiation. | Planner tests. |
| CLI plan | `easycat plan --json` emits provider plan envelope. | CLI JSON test. |
| WebRTC route | `VoiceServer` can serve browser WebRTC without breaking old helper APIs. | WebRTC integration tests. |
| Metrics | Server counters/gauges use safe low-cardinality labels. | Metrics tests. |
| Docs | Deployment, Docker, observability, and README docs explain `VoiceServer` and manifest. | Guard docs. |

## Phase 3 — Feedback Loop

| Capability | Acceptance Criteria | Evidence |
|---|---|---|
| Dev activation | `EASYCAT_DEV=1` or `VoiceApp(dev=True)` enables dev debugger defaults. | Dev debugger tests. |
| No surprise autolaunch | `debug="full"` alone does not open debugger UI. | Existing/new DX test. |
| Session registry | One debugger process can list/switch live sessions. | Debugger server/API tests. |
| Budget overlays | Debugger API returns budget report; UI renders budget status. | API test + static/UI test. |
| Evals package | `easycat.evals` exports scenario runner and existing assertion helpers. | Import/public API tests. |
| Text scenario | Eval runner executes text turns without live audio. | Eval runner tests. |
| CLI eval | `easycat eval run` executes scenario files and emits JSON report. | CLI eval tests. |
| Promotion | `easycat eval promote PATH TURN_ID` generates pytest skeleton. | Promotion tests. |
| PII warning | Promotion warns and defaults to no audio. | CLI tests. |
| Replay safety | Promoted replay denies tool execution by default. | Replay-as-test tests. |
| Budgets package | `easycat.budgets.LatencyBudget` works and legacy import remains valid. | Budget alias tests. |
| Cost budget | `CostBudget` coexists with `max_session_cost_usd` alias. | Config/budget tests. |
| Runtime milestones | First-token/first-audio metrics are recorded where observable. | Session/runtime tests. |
| Shared report | Debugger, eval, CLI, and validation use shared budget report semantics. | Cross-surface tests. |
| Docs | Observability, evals, latency, validation, and teaching docs reflect new APIs. | Guard docs. |

## Global Acceptance Criteria

- New public APIs are documented and covered by public API tests.
- New CLIs support `--json` when they emit structured output.
- Optional dependencies remain optional and fail with actionable messages.
- No secrets or PII are introduced into metrics labels or unredacted planning output.
- Existing lower-level APIs remain available until a deliberate next-major
  removal/deprecation pass.
- Generated docs route files are regenerated when route maps change.
