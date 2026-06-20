# Neo Open Questions

Status: active decision log.

This file tracks decisions that should be answered before freezing the
next-major API. Each question includes a recommended default so implementation
can proceed unless maintainers choose otherwise.

## Product/API Questions

### Q1 — Should `VoiceApp` be top-level?

**Question:** Should users import `VoiceApp` from `easycat`, `easycat.app`, or
both?

**Recommended default:** Export top-level `VoiceApp` because it is the new
headline user surface. Also keep implementation in `src/easycat/voice_app.py`
for simplicity. Note: `easycat.__all__` is at 94/94 today
(`tests/test_public_api.py:126` asserts `<= 94`), so adding top-level `VoiceApp`
is a deliberate cap bump to 95 plus the triple-lock (`__all__`, `LAZY_EXPORTS`,
`docs/public-api.md`/`PUBLIC_API_SNAPSHOT`) in one PR.

### Q1b — Is `default_mode` real, and how do construction inputs combine?

**Question:** Does `VoiceApp` keep a `default_mode` field, and what happens when
a user supplies more than one of `agent`/high-level fields, `config`, and
`config_factory` at once?

**Recommended default:** DELETE `default_mode` — it is a dead field. Grep finds
exactly one occurrence repo-wide and no method reads it; `session()`/`serve()`/
`run()` each hardcode their own default mode. Replace it by giving every method a
`mode: VoiceMode | None = None` parameter (each method resolves its own fallback
when `None`).

Make the three construction inputs MUTUALLY EXCLUSIVE: supplying more than one of
(a) high-level fields (`agent`/`stt`/`tts`/`vad`/`debug`), (b) a fully-built
`config`, or (c) a `config_factory` raises `ValueError` naming the conflict. For
example `VoiceApp(agent=a, config=EasyConfig.browser(agent=b))` is a real,
currently-undefined conflict (`EasyConfig.browser(agent=...)` is valid via
`easy.py:891-916`) and must be rejected. Replace the 5-field `@dataclass` sketch:
it cannot accept `stt=`/`tts=` and raises `TypeError` for inputs the plan
documents. Define instead a FIELD ALLOW-LIST of which `EasyConfig` fields
`VoiceApp` forwards into the chosen preset via `**config_kwargs` (`agent`, `stt`,
`tts`, `vad`, `debug`, plus mode-appropriate transport/auth fields) versus which
`VoiceApp` owns (`dev`; `default_mode` deleted), and enforce that allow-list with
a test.

### Q2 — What should `easycat serve` mean?

**Question:** Should `easycat serve` remain a browser playground command or
become the unified dev/prod server command?

**Recommended default:** In Phase 1, keep `easycat serve` browser-first and add
`--mode`. In Phase 2, add `--manifest` and let the same command run
`VoiceServer` when a manifest is present.

**Note on `/health/ready` ownership (split per-milestone):** the readiness
contract is split across two milestones and must not be implemented as one
endpoint. M4 owns the serving / draining / capacity / route-ready signals ONLY.
M6b owns the manifest-loaded + plan-has-no-blocking-errors signals, and those
checks are gated behind the planner-vs-`create_session` parity test passing (the
planner verdict must match the `create_session` outcome for every one of the 7
roles before its readiness signal is trusted). M4's readiness therefore has an
explicit soft backward dependency on M6b — do not let M4 quietly assert
manifest/plan readiness.

### Q3 — Is `VoiceApp.run("browser")` always WebRTC?

**Question:** Should browser mode abstract over WebRTC and WebSocket browser
clients?

**Recommended default:** Yes for the product concept eventually, but start with
browser = WebRTC because that is the current strongest browser path. Keep raw
WebSocket as `mode="websocket"`.

### Q4 — How much Twilio belongs in Phase 1?

**Question:** Should Phase 1 include full Twilio server support or just set the
API shape?

**Recommended default:** Ship local/browser/websocket first, then extract Twilio
server helper as the next PR. Do not block `VoiceApp` on Twilio.

### Q4b — How does `VoiceApp` produce a fresh `EasyConfig` per connection?

**Question:** Should each connection get its config via a dedicated
`with_transport()` helper, via `dataclasses.replace`, or by mandating
`config_factory`?

**Recommended default:** MANDATE `config_factory` as the ONLY safe per-connection
config mechanism — it is the only mechanism the serve helpers already require
safely. The canonical signature is the per-transport factory shape
`Callable[[TransportT], EasyConfig]`, where `TransportT` is the concrete
connection transport for the mode (`WebRTCTransport`,
`WebSocketConnectionTransport`, `WebTransportConnectionTransport`,
`TwilioConnectionTransport`).

Explicitly REJECT both `dataclasses.replace` and a hypothetical `with_transport()`
helper: no clone / `with_transport` / `replace_transport` helper exists in
`config/` today, and the grouped sub-configs (`observability`,
`audio_processing`, `session_policy`) are shared by reference. This is the
InitVar/proxy shared-state footgun: `dataclasses.replace` copies the top-level
fields but keeps the SAME grouped sub-config instances, so mutating a replaced
config's `observability.debug` flips the original (verified empirically). A naive
clone therefore does NOT isolate concurrent sessions. There is also no abstract
`ConnectionContext` type — it appears nowhere in the tree and must not be
introduced; the real serve helpers already take transport-specific args.

For the `VoiceServer` multi-transport seam the session factory is
`Callable[[TransportT], EasyConfig | Session]` selected per route/transport (NOT
a single unified `ConnectionContext`).

## Manifest Questions

### Q5 — TOML or YAML?

**Question:** Should project manifests be `easycat.toml`, `easycat.yaml`, or
both?

**Recommended default:** Start with `easycat.toml` because Python 3.11 includes
`tomllib`, avoiding a YAML dependency. Add YAML later only if demand is strong.

### Q6 — How should agents be referenced in manifests?

**Question:** What syntax should point to Python agent factories?

**Recommended default:** Use `python:module.path:function_name`, for example:

```toml
agent = "python:app:create_agent"
```

This is explicit and avoids importing arbitrary files by path until the loader
has validated the manifest.

### Q7 — Should secrets be allowed as literal values?

**Question:** Should manifests allow literal API keys or only env references?

**Recommended default:** Do not support literal secrets in the first version. Use
env references such as `bearer-env:EASYCAT_SERVE_TOKEN`. Make this a TESTABLE
contract, not prose:

- `auth`/`token` fields MUST match an env-reference grammar — `bearer-env:NAME`,
  where `NAME` is an env-var identifier. In the `bearer-env:NAME` model the env
  name is user-chosen; the examples use `bearer-env:EASYCAT_SERVE_TOKEN` purely
  for consistency with the shipped env var (see the env-var note below), but the
  loader accepts any valid env name.
- The loader MUST RAISE a coded error (e.g. `EASYCAT_Exxx`) when it sees a
  literal-looking secret, reusing `redaction._SECRET_RE` /
  `contains_unredacted_sensitive_text` for the detection. State plainly that NO
  loader exists today (grep: no `bearer-env`/`tomllib`/`ProjectManifest`), so
  this is net-new.
- Any echoed/dumped manifest routes through `redact_value` before serialization.
- Acceptance tests: (a) a literal secret is rejected with the coded error;
  (b) the `--json` / `/manifest` dump shows no resolved token value.

**Env-var name decision:** standardize on the EXISTING shipped env var
`EASYCAT_SERVE_TOKEN` (defined at `cli/serve.py:36,106`), NOT `EASYCAT_SERVER_TOKEN`.
The two names are one letter apart and the plan migrates `serve` through
`VoiceApp`/`VoiceServer` without saying which wins; keep the shipped name to avoid
a silent rename. Because the `bearer-env:NAME` value is user-chosen, this is
naming hygiene for our own examples/defaults, not an auth bypass.

## Server Questions

### Q8 — Should `VoiceServer` use aiohttp or FastAPI?

**Question:** Which framework should own the core server routes?

**Recommended default:** Use aiohttp internally. Existing WebRTC signaling and
debugger server already use aiohttp-style infrastructure, and it avoids making
FastAPI a core dependency.

### Q9 — Should health endpoints be always enabled?

**Question:** Can users disable health endpoints?

**Recommended default:** Health endpoints are always enabled for `VoiceServer`.
They are low risk and critical for deployment.

### Q10 — What is the default auth policy?

**Question:** Should a local server require auth?

**Recommended default:** Loopback server can run without auth. Non-loopback
requires explicit auth unless `unsafe_allow_no_auth=True` is set.

The non-loopback-requires-token guard is a PROPERTY of the unified `VoiceServer`/
`AuthPolicy` layer, applied to BOTH the WebSocket and WebRTC transports. Add
`unsafe_allow_no_auth: bool = False` as a structured field on `AuthPolicy`
(and/or `VoiceServerConfig`) — it must be a real field, not prose-only — and that
field is the ONLY escape hatch. State plainly that today this is NOT parity work:
WebRTC already enforces the guard (`webrtc.py:347-351,924-927` raise
`ValueError`), but the WebSocket path has NO loopback check at all
(`websocket.py:84` defaults `auth_token=None`, `:93` lets `EASYCAT_WS_HOST`
override the host, and `:100-111` returns authorized whenever the token is
`None`), so a `0.0.0.0` unauthenticated voice endpoint is reachable today. The
unified guard CLOSES that real gap. Acceptance test: a non-loopback WebSocket bind
with no token and without `unsafe_allow_no_auth` RAISES.

**`?token=` query auth (`allow_query_token`):** default it to `False`
(default-off is the correct posture). State that `?token=` is UNCONDITIONAL today
whenever a token is set — WebRTC (`webrtc.py:826-834`) and WebSocket
(`websocket.py:102-111`) both accept it — so `allow_query_token=False` is a
BREAKING CHANGE for the WebSocket browser client
(`examples/ws_browser_client.html:91-93`: browsers cannot set headers on the WS
handshake). The bundled WebRTC client is UNAFFECTED (it sends
`Authorization: Bearer`). Confirm the new default applies to the EXISTING
handlers, document the WS-client break in the plan, and provide an
`allow_query_token=True` loopback opt-in for the WS browser client.

## Debug/Eval Questions

### Q11 — What exactly does “always-on debugger” mean?

**Question:** Does it mean always recording, always serving a debugger, or always
opening the browser?

**Recommended default:** In dev mode, always record and serve a loopback
debugger. Browser opening is still explicit or dev-only. In production,
recording/debugging follows observability config and never autolaunches UI by
default.

### Q12 — Should `easycat.evals` depend on pytest?

**Question:** Should eval APIs import pytest or remain test-framework neutral?

**Recommended default:** Core `easycat.evals` must be pytest-free. Put pytest
fixtures/adapters in `easycat.evals.pytest`.

### Q13 — Should promotion generate code or data?

**Question:** Should `easycat eval promote` generate pytest files, scenario
files, bundle fixtures, or all of the above?

**Recommended default:** Generate a pytest skeleton and a scenario/bundle
fixture reference. Let users choose `--mode record-assertion` or
`--mode artifact-replay`.

### Q14 — How strict should redaction be?

**Question:** Should promotion refuse to write unredacted fixtures?

**Recommended default:** State plainly first that the CURRENT path is UNSAFE: the
existing `journal promote` → `slice_bundle_by_turn` → `debug/export.py:154-170`
chain copies full raw NDJSON, every audio blob, and the verbatim transcript into a
committed file with ZERO redaction. So this is HARDENING, not preserving safe
behavior. The hardened answer:

- Redact-by-default: route records through `redact_value` before serialization.
- `--no-audio` is the DEFAULT (mirror `bundles export` at `bundles.py:1160`).
- Unredacted transcript/tool text requires explicit `--allow-pii`, gated by a
  `contains_unredacted_sensitive_text()` tripwire (mirror
  `_assert_context_pack_redacted`) that refuses to write when the flag is absent.
- The record-assertion mode DEFAULTS to assert on a HASH/REGEX over the reply
  rather than embedding raw reply text — because redaction is field-name +
  secret-regex only (no NER), so a transcript that is itself the assertion target
  cannot be both redacted and useful.
- Reuse `_promote_test_stub` / `_validate_promoted_slice` rather than
  re-implementing.

**Extend-vs-fork decision:** FORK a new `eval promote` command rather than
silently extending `journal promote` (leaving both unreconciled is itself a
footgun). Reconcile the two verbs in scope: document `journal promote` as the
unsafe legacy path and `eval promote` as the hardened replacement.

## Budget Questions

### Q15 — Should `LatencyBudget` move packages?

**Question:** Should `LatencyBudget` move from `easycat.validation.latency` to
`easycat.budgets`?

**Recommended default:** Add `easycat.budgets.LatencyBudget` as the preferred
import and keep `easycat.validation.latency.LatencyBudget` as an alias.

### Q16 — Should budget violations stop sessions?

**Question:** Should latency budget violations trigger runtime action like cost
budgets do?

**Recommended default:** Not by default. Latency budget violations should record
and report. Cost budget can keep stop behavior because it is explicit spend
control.
