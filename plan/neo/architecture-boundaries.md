# Neo Architecture Boundaries

Status: active proposal.

This file defines the boundaries that should stay stable while implementing the
Neo phases. The highest-risk failure mode is creating overlapping abstractions
that each own a different version of session construction, provider planning,
server lifecycle, or debugging.

## Boundary Summary

| Layer | Owns | Must Not Own |
|---|---|---|
| `Session` | One conversation/call/client pipeline and lifecycle. | Process routing, multi-client server policy, manifests, deployment auth. |
| `EasyConfig` | Detailed session/provider/transport/observability config. | Product-level run modes or process-level server lifecycle. |
| `VoiceApp` | Product-level app surface and mode selection. | Provider instantiation internals or HTTP server policy. |
| `VoiceServer` | Production process runtime: routes, auth, health, metrics, shutdown, capacity. | Audio pipeline details or provider implementation. |
| Journals/bundles | Durable truth for runtime inspection and replay. | Project/application manifest semantics. |
| Evals/budgets | CI-friendly assertions over conversations, journals, and runtime metrics. | Live transport implementation details. |

## `Session`: one conversation runtime

`Session` should remain the per-call/per-client pipeline object. It owns:

- STT, TTS, VAD, noise reduction, echo cancellation, transport, agent bridge.
- Event bus and turn state.
- Start/stop/cancel/reset/send-text lifecycle.
- Journal sink and runtime budget enforcement.
- Telephony helper attachment for a single session.

Do not turn `Session` into a server or registry. Server-level concerns such as
active session limits, readiness, auth, route mounting, and graceful process
shutdown belong above it.

## `EasyConfig`: detailed session declaration

`EasyConfig` should remain the complete declaration for one session. It already
knows how to express:

- STT/TTS provider shortcuts, configs, or instances.
- Audio processing choices.
- Transport config or transport instance.
- Turn taking, timeouts, session policy, telephony, actions, and observability.
- Agent adaptation and MCP/remote-agent settings.

`VoiceApp` and `VoiceServer` should produce `EasyConfig` objects or accept user
factories that produce `EasyConfig` objects. They should not bypass
`create_session` unless a very narrow testing seam requires it.

## `VoiceApp`: app-level product surface

`VoiceApp` is the developer-facing object that answers:

- What agent is this?
- How are mode-specific defaults applied when a mode is requested?
- Which lower-level config factory should be used for per-connection sessions?

`VoiceApp` has **no** `default_mode` field. The run mode is supplied per
`run()`/`serve()`/`session()` call; each method keeps its own default. (The
earlier sketch's stored `default_mode` is deleted — it was never read by any
method.)

The three construction inputs are mutually exclusive:

- `agent=` plus high-level fields (the simple path),
- `config=` (a fully built `EasyConfig`), and
- `config_factory=` (a per-connection factory).

Supplying more than one raises `ValueError` naming the conflict (for example,
`VoiceApp(agent=a, config=EasyConfig.browser(agent=b))` is rejected because the
agent is declared twice). See `phase-1-voice-app.md` for the forwarded-field
allow-list that backs the simple path.

It should be thin enough that advanced users can still drop down to
`EasyConfig` and `create_session`.

Recommended responsibilities:

- Normalize mode aliases: `local`/`mic`, `browser`, `websocket`/`ws`,
  `twilio`/`phone`.
- Build a per-connection `EasyConfig` via a `config_factory` (the only safe
  per-connection mechanism). `dataclasses.replace` is UNSAFE for per-connection
  cloning because the grouped sub-configs (`observability`, `audio_processing`,
  `session_policy`) are shared by reference — mutating a replaced config flips
  the original, so a naive clone does NOT isolate concurrent sessions. No
  `with_transport`/`replace_transport`/clone helper exists in `config/`; do not
  assume one.
- Delegate local runs to `create_session` + `run_session` (note `run_session`
  lives in `easycat.helpers`, not the top-level package — see
  `phase-1-voice-app.md`).
- Delegate browser/WebSocket runs to existing multi-session transport helpers.
- Delegate Twilio runs to a reusable telephony server helper.

## Event loop ownership

One rule across both `VoiceApp` and `VoiceServer`:

- `run()` is the **sole** `asyncio.run()` owner on both objects. No other method
  may call `asyncio.run()`.
- The async verb is aligned on both objects: use `serve()` on `VoiceApp` and
  `VoiceServer` (drop the asymmetric `serve_forever`, or keep it only as an
  alias). `serve()` is awaited inside a caller-provided running loop.
- `session(mode=...)` returns an **un-started, caller-owned** `Session`
  (matching `create_session` at `config/_factory.py:351`). It is valid only for
  single-session modes (`local`) and RAISES for server/multi-session modes
  (`browser`/`websocket`/`twilio`), where returning a single `Session` would be
  ambiguous.
- `VoiceServer` composes mounted apps via their `config_factory` ONLY. It NEVER
  calls `VoiceApp.run()` — doing so would nest `asyncio.run()` inside the
  server's own loop.

## `VoiceServer`: production process layer

`VoiceServer` owns concerns that happen around sessions:

- HTTP/WebSocket/WebRTC/Twilio route mounting.
- Auth and CORS policy.
- Liveness/readiness/health endpoints.
- Metrics and safe low-cardinality labels.
- Active session limits and rejection behavior.
- Draining and graceful shutdown.
- Manifest/profile loading.
- Provider/capability planning.

It should create sessions through a supplied **per-transport** factory. There is
no abstract `ConnectionContext` — that type does not exist in the tree and has
been removed from the plan everywhere it appeared. The serve helpers already
take transport-specific args, so the seam is per-transport:

```python
Callable[[TransportT], EasyConfig | Session]
```

where `TransportT` is the concrete connection transport for the route's mode:

- `WebRTCTransport` (browser/WebRTC route),
- `WebSocketConnectionTransport` (WebSocket route),
- `WebTransportConnectionTransport` (WebTransport route),
- `TwilioConnectionTransport` (Twilio media route).

`VoiceServer` selects the factory per route/transport; it is NOT a single
unified callable over one context type. If a factory returns `EasyConfig`,
`VoiceServer` calls `create_session`. If it returns `Session`, `VoiceServer`
manages it directly. This supports advanced users without duplicating
construction internals.

`dataclasses.replace` and a hypothetical `with_transport()` are explicitly
rejected as per-connection mechanisms: the grouped sub-configs
(`observability`, `audio_processing`, `session_policy`) are shared by reference,
so cloning a base config does not isolate concurrent sessions. The
`config_factory` is the only safe per-connection mechanism (see `VoiceApp`
above).

## Manifest vs debug bundle manifest

Do not overload the word `Manifest` at the top level. There are two distinct
concepts:

1. **Project manifest**: a user-authored `easycat.toml` or `easycat.yaml` that
   describes app profiles, server config, provider choices, and deployment
   metadata.
2. **Debug bundle manifest**: exported runtime metadata for an observed session
   or bundle.

Use explicit names:

- `ProjectManifest` or `VoiceProjectManifest` for user project config.
- Existing debug/bundle manifest names remain scoped to debug modules.

**Project manifest secret rule.** No manifest loader exists today (this is
net-new). The `auth`/`token` fields MUST be env-references using the
`bearer-env:NAME` grammar — never literal secrets. The loader MUST reject a
literal-looking secret (reuse `redaction._SECRET_RE` /
`contains_unredacted_sensitive_text`) and any echoed or dumped manifest is
routed through `redact_value` so a resolved token value can never appear in
`--json` or `/manifest` output. See `phase-2-voice-server.md` for the testable
grammar and the coded-error contract.

## Provider/capability planning boundary

Provider planning should be a read/validation layer that does NOT become a
second provider factory. But it is NOT a uniform "read over existing catalog
metadata": only **STT and TTS** have a `ProviderCatalog`
(`_provider_catalog.py:1-2,285-353`) the planner can read. The other five roles
have NO static catalog:

- **VAD** resolves by try/except with the install extra embedded in an error
  string (`vad/factory.py:91-151`).
- **Transport** is config-type dispatch (`config/_factory.py:110-139`).
- **noise_reducer** / **echo_canceller** carry hardcoded extras only
  (`noise_reduction.py:40,96`, `echo_cancellation.py:11,90`).
- **agent** has no catalog; `ProviderSelection.capabilities` has no static
  source (`validation/provider_capabilities.py:2-5` is a live-derived report).

Therefore the planner must define NET-NEW declarative metadata for
vad/transport/agent/noise_reducer/echo_canceller — it cannot "extract shared
catalog metadata" for them. Because that metadata is hand-rolled and can drift,
the planner's verdict MUST be validated against `create_session`: a
planner-vs-`create_session` parity test (the planner's verdict matches the
`create_session` outcome for every one of the 7 roles) is a required gate, and
this is why R6 divergence risk is amplified. See `phase-2-voice-server.md`
(M6a/M6b split) for the planner scope and the parity gate.

Planner outputs should answer:

- Which provider/config will be used for each role?
- Which env vars are missing?
- Which extras are missing?
- Which requested capabilities are unsupported?
- Which transport/provider combinations are suspicious?

Provider instantiation still happens through the existing factories and
`create_session`.

## Debug/eval boundary

The journal remains the shared substrate. Debugger, replay, evals, budget
reports, and promotion-to-test should all consume journal/bundle records rather
than each inventing a trace format.

Rules:

- Eval results should expose a `records()` protocol compatible with existing
  bundle/test helpers.
- Replay should keep side effects denied by default.
- Promotion is **redact-by-default**, not warn-and-hope (D6 / MF-1, CRITICAL;
  R8; Q14). This replaces the earlier "warn about PII" framing, which documented
  the *existing unsafe* behavior as if it were safe: today `journal promote` →
  `slice_bundle_by_turn` → `debug/export.py:154-170` copies full raw NDJSON +
  every audio blob + verbatim transcript into a committed file with ZERO
  redaction. The hardened contract instead requires that promotion route records
  through `redact_value` before serialization, default `--no-audio`, default the
  record assertion to a hash/regex over the reply (not embedded raw text), and
  trip `contains_unredacted_sensitive_text()` unless `--allow-pii` is passed. See
  `phase-3-feedback-loop.md` (Promotion CLI) and R8 for the full contract.
- Budget stage names should be stable across runtime, validation, debugger,
  evals, and docs.

## Compatibility Policy During Neo

This is a next-major plan, so backward compatibility can be broken if the gain
is high enough. Still, the lowest-risk implementation path is additive first:

1. Add `VoiceApp` over existing config/session primitives.
2. Add `VoiceServer` over existing transport/server/session primitives.
3. Add `easycat.evals` and `easycat.budgets` while re-exporting current helpers.
4. Deprecate old first-run paths after the new paths are proven.

Breaking removals should come after docs, scaffolds, and examples have moved to
Neo surfaces.
