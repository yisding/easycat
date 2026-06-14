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
- What mode should it run in?
- How should mode-specific defaults be applied?
- Which lower-level config factory should be used for per-connection sessions?

It should be thin enough that advanced users can still drop down to
`EasyConfig` and `create_session`.

Recommended responsibilities:

- Normalize mode aliases: `local`/`mic`, `browser`, `websocket`/`ws`,
  `twilio`/`phone`.
- Build or clone `EasyConfig` per mode.
- Delegate local runs to `create_session` + `run_session`.
- Delegate browser/WebSocket runs to existing multi-session transport helpers.
- Delegate Twilio runs to a reusable telephony server helper.

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

It should create sessions through a supplied factory:

```python
Callable[[ConnectionContext], EasyConfig | Session]
```

If the factory returns `EasyConfig`, `VoiceServer` calls `create_session`. If it
returns `Session`, `VoiceServer` manages it directly. This supports advanced
users without duplicating construction internals.

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

## Provider/capability planning boundary

Provider planning should be a read/validation layer over existing provider
catalog metadata. It should not become a second provider factory.

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
- Promotion should warn about PII and default toward safer fixtures.
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
