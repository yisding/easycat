# EasyCat Architecture

This page explains how EasyCat fits together: the audio pipeline, the
`session/` orchestrator package, the stage and provider layers, the agent
bridges, and the fallback chains. It is the maintained home for architecture
prose; [CLAUDE.md](../CLAUDE.md) keeps only a short orientation map that links
here. Pair this explanation with the reference pages for
[events](reference/events.md), [EasyConfig fields](reference/easyconfig.md),
and the [session lifecycle](reference/session-lifecycle.md).

Discover the maintainer route map with
`uv run easycat docs --audience maintainers`, or
`uv run easycat docs --audience maintainers --json` when a script or coding
agent needs route entries and command hints.

## Pipeline Flow

Transport (audio in) → EchoCanceller → NoiseReducer → VAD → STT →
[SmartTurn] → Agent → TTS → Transport (audio out).

AEC runs on the raw mic signal *before* NoiseReducer because NR's nonlinear
processing breaks AEC convergence.

The `EchoCanceller` also consumes TTS output as reference audio (fed in by
`session/_audio_router.py`) so it can subtract the bot's own playback from the
captured mic signal.

## The Session Package

`session/` contains the core orchestrator. Key files:

- `session/_session.py` — `Session` class. Owns the public lifecycle API
  (start/stop/cancel_turn/reset_state/send_text/etc.) and turn-pointer state.
  `Session.__init__` is a short field-assignment shell that ends with a single
  `build_session(self, cfg)` call — a newcomer can scan the constructor in
  under a minute.
- `session/_builder.py` — `build_session(session, cfg) -> SessionComponents`.
  Owns ALL collaborator construction that used to be inlined in `__init__`:
  the 7 stages, the shared `RunContext`, the `no-turn` `TurnContext`, the
  journal sink, the outbound audio queue, and every collaborator
  (AudioRouter/STTCommitter/TTSScheduler/CancelOrchestrator/TurnRunner/
  GreetingController), plus their deferred event-bus
  subscriptions and TurnManager bindings. Returns a frozen
  `SessionComponents` bundle the constructor unpacks onto private fields.
- `session/_wiring.py` — `SessionWiringContext`, a typed frozen dataclass of
  late-binding getters/setters (current_turn, is_running, enable_* flags,
  provider/agent getters, emit, drain_session_actions,
  caller_id_system_message, stop, …) built once from the live Session and
  passed to every collaborator constructor in place of ~40 inline lambdas.
  Also holds `_SessionTurnHandle` (the `TurnHandle` adapter). Imports
  `Session` only under `TYPE_CHECKING`.
- `session/_greeting.py` — `GreetingController`. Subscribes itself to
  `CallAnswered` and speaks the configured greeting once (warm-transfer
  re-answer ignored) via the bypass synth path.
- `session/_caller_id.py` — `CallerIdState`. Holds the caller/callee identity
  + exposure policy and renders the caller-ID system message.
  `Session.call_identity` / `caller_id_exposure` delegate here;
  `private_identity` is the raw value used by `config/` wiring.
- `session/_telephony_facade.py` — `TelephonyFacade` exposed as
  `session.telephony`. Wraps the helper list with `.get(type)` plus typed
  accessors (`outbound_call_manager`, `outbound_call_state_machine`,
  `number_health_monitor`, `call_disposition_tracker`). `session.get_helper`
  delegates to it.
- `session/_streaming.py` — `consume_agent_stream()` translates agent stream
  events into TTS payloads on sentence boundaries.
- `session/_turn_runner.py` — Drives a single turn end-to-end (agent run →
  streaming → TTS scheduling), holding the logic that used to be inlined in
  `session/_session.py`.
- `session/_audio_router.py` — Routes captured audio through echo cancellation
  / noise reduction and feeds TTS output back as AEC reference audio.
- `session/_tts_scheduler.py` — `TTSScheduler.prepare()` builds and
  normalizes TTS payload text before scheduling synthesis/playback.
- `session/_stt_committer.py` — Commits finalized STT transcripts into the
  turn lifecycle.
- `session/interruption.py` — Audio-byte estimation for barge-in: maps TTS
  output back to what the user heard.
- `session/text.py` — Sentence splitting, markdown checking, speech energy
  detection, and spoken-text timeline normalization
  (`_text_for_estimation_timeline`). The audio-byte→text estimation itself
  (`_estimate_text_spoken`) lives in `session/interruption.py`.
- `session/_types.py` — `SessionConfig`, `TurnState`, `Agent` protocol.

## Core Modules Around the Session

- `config/` — `EasyConfig` (simplified, auto-wires OpenAI providers) and
  `SessionConfig` (advanced, explicit providers). `create_session()` factory
  builds a wired Session. Field-by-field documentation lives in the
  [EasyConfig reference](reference/easyconfig.md).
- `events.py` — `EventBus` pub/sub with sync/async handlers. Two event
  layers: provider-scoped (`STTEvent`, `TTSEvent`) emitted by providers,
  mapped to EasyCat-level events (`STTFinal`, `TTSAudio`, `TurnStarted`,
  etc.) by Session. The full catalog lives in the
  [events reference](reference/events.md).
- `providers.py` — `@runtime_checkable` Protocol definitions for all provider
  interfaces (`STTProvider`, `TTSProvider`, `VADProvider`, `Transport`,
  `NoiseReducer`, `EchoCanceller`). Providers use duck typing, not
  inheritance.
- `turn_manager.py` — 5-state FSM (IDLE → USER_SPEAKING → USER_PAUSED →
  PROCESSING → BOT_SPEAKING) with pre-roll buffering and interruption
  detection. Supports VAD (automatic) and PUSH_TO_TALK turn modes.
- `runtime/` — Journal-based debug-first runtime. `ExecutionJournal` records
  events, spans, and metrics. `JournalView` provides query access. The
  journal is the single source of truth for all observability.
- `validation/` — Validation report models, redaction, and runner helpers
  behind the `easycat validate` lanes. `validation/_lane_harness.py` holds
  `LaneHarness`, the shared start/finish bookkeeping (run-id allocation,
  report directory, atomic report writes, `latest.json`) every lane runs
  through.
- `stages/` — Pipeline stages wrapping providers with a uniform `execute` /
  `snapshot_state` / `handle_upstream` surface and optional journal
  recording. `Stage` protocol defined in `stages/base.py`.
- `debug/` — `RunBundle` for serializing/loading complete session
  recordings. `load_bundle()` for test fixtures. `debug/_serialize.py` is
  the canonical record/config → JSON-safe-dict walk shared by the bundle
  exporter and the debugger (one serializer, so live views and exported
  bundles cannot drift).
- `debugger/` — aiohttp debugger UI for live journals and exported bundles.
  `debugger/server.py` keeps application assembly separate from the
  class-based HTTP route controller and its explicit per-app state; the leaf
  modules behind that surface are `debugger/_records.py` (record
  filtering/search), `debugger/_audio.py` (PCM/WAV coercion),
  `debugger/_sources.py` (`DebuggerSource` and journal/bundle sources), and
  `debugger/_aec_routes.py` (AEC diagnostics routes over `debugger/_aec.py`).
- `cli/` — Typer command surface for `init`, `doctor`, `docs`, `bundles`,
  `inspect`, `replay`, and `validate`. The `bundles`/`journal` command
  implementations live one-per-file under `cli/debug/` (follow, grep,
  diff, export, promote, latency, replay), with `cli/debug/bundles.py`
  as the Typer app assembly.
- `server/` — `VoiceServer` plus the shared signaling surface:
  `server/auth.py` owns the unified `AuthPolicy` / `BearerTokenAuth` layer,
  and `server/_webrtc_handlers.py` holds `WebRTCSignalingHandlers`, the
  single copy of the stateless WebRTC config/stats/health/root/CORS
  handlers that both the singleton `transports/webrtc.py` transport and the
  multi-session `server/webrtc_routes.py` delegate to.
- `_net.py` / `_env.py` (package root) — leaf helpers with zero
  intra-package imports: loopback-host + auth-token normalization shared by
  transports, CLI serve, server auth, and the debugger origin guard
  (`_net.py`), and env-var flag parsing (`_env.py`).
- `smart_turn.py` — Optional ONNX-based endpoint detection that classifies
  whether a user has finished speaking, enabling faster turn transitions
  without waiting for silence timeout.
- `_turn_context.py` (package root) — `TurnContext` per-turn state (timing,
  playback tracking, cancel token; created fresh each turn) and the
  `TurnHandle` protocol. Lives at the root as a leaf (depends only on
  `cancel.py`) so both `session/` and the lower `stages/` layer import it
  downward — preserving the `Session → Stages → Providers` direction without
  an import cycle.

## Provider Subpackages

`stt/`, `tts/`, `vad/`, `transports/`, `telephony/`: one provider per file,
each implementing the corresponding Protocol. Base classes (`STTBase`,
`TTSBase`, `ServerTransportBase`) provide shared plumbing; `AudioQueueMixin`,
`ServerTransportBase`, and `TransportDegraded` are re-exported from
`easycat.transports` for out-of-tree transports (see
[extending/](extending/README.md)).

`stt/factory.py` and `tts/factory.py` each build a `ProviderCatalog`
(`_provider_catalog.py`) from one `ProviderSpec` per backend. The catalog
derives the `_PROVIDER_TO_CONFIG`, credential, install-extra, and API-domain
views used by doctor, scaffolding, validation, and redaction. To add a new
STT/TTS provider, add one spec and its config dataclass.

## Agent Bridges

`integrations/agents/` defines `ExternalAgentBridge` (the single contract
between Session and agents) with implementations `OpenAIAgentsBridge`,
`PydanticAIBridge`, `GenericWorkflowBridge`, `RemoteResponsesAPIBridge`,
`LlamaAgentsBridge`, `LangChainBridge`, and `LangGraphBridge`. `AgentRunner`
(in `integrations/agents/_agent_runner.py`) implements `ExternalAgentBridge`
by wrapping a simple `async run(text) -> str` object — used for basic agents
that need timeout/cancellation/history. `auto_adapt_agent()` in
`integrations/agents/_factory.py` detects known framework objects and returns
the right bridge.

Bridge text is either a flat sequence of unindexed `text_delta` events or an
indexed sequence whose `text_replace` snapshots replace complete response
parts and whose deltas append within those parts. Session repairs replacements
before TTS admission; a replacement that crosses admitted speech clears and
suppresses the rest of that turn's playback while preserving the corrected
final transcript.

## Dual-Backend Fallback

- VAD: `create_vad` auto-resolves Silero → FunASR → TEN → Krisp and raises if
  none resolve.
- Noise reduction: `create_noise_reducer` auto-resolves Krisp → RNNoise →
  passthrough.
- Echo cancellation: `create_echo_canceller` builds from
  `EchoCancellationConfig` — LiveKitAEC when enabled and available, else
  `PassthroughAEC`; `EasyConfig` derives a transport-aware default via
  `enable_echo_cancellation`.

VAD and noise reduction can each be forced to a single backend via
`VADConfig.backend` / `NoiseReducerConfig.backend`.

## Key Patterns

- **Protocol over inheritance** — all providers defined as `typing.Protocol`
  in `providers.py`.
- **Async-first** — all I/O is async; providers are async iterators.
- **Cooperative cancellation** — `CancelToken` (not exceptions) for turn/TTS
  cancellation.
- **Factory functions** — `create_session()`, `create_vad()`,
  `create_noise_reducer()`.
- **Event bus injection** — a provider config that declares optional
  `event_bus` receives the session bus when unset; no provider requires it.
  Injected STT/TTS/VAD/noise/AEC/transport instances that emit
  provider-scoped events expose synchronous `set_event_bus(bus)`. Private
  attribute probes are compatibility-only.
- **Noop stubs** (`stubs.py`) — `NoopSTT`, `NoopTTS`, `NoopVAD`,
  `NoopTransport` for test isolation.

## Firm Architecture Decisions

**Status: accepted.** These decisions are the default for implementation,
review, and release work. They exist to prevent a new audit or isolated fix
from reopening a settled cross-cutting contract. A change that contradicts
one of them must update this section first, explain the migration and
compatibility impact, and update the named contract tests in the same pull
request. A passing implementation test alone is not enough to reverse a
decision.

### Public API and session lifecycle

- `await session.stop(force=False)` is the single public graceful teardown
  operation. It drains in-flight work before releasing every owned backend.
- `await session.stop(force=True)` is the single public forceful teardown
  operation. It cancels in-flight work first and then performs the same
  complete backend teardown. `async with session:` uses this mode on exit.
- `stop()` is idempotent. There are no separate public `close()` or
  `destroy()` phases and no aliases for them.
- A clean stop preserves a read-only postmortem view for
  `session.journal.read()` and `session.export_debug_bundle(...)`.
- The curated top-level exports and the documented bridge/provider protocols
  are the public API. New exports require an intentional public-API update;
  removals require a documented deprecation and migration period, including
  before 1.0.

This supersedes proposals to restore teardown aliases, expose backend-specific
close phases, or infer the public API from everything importable. Enforce it in
`tests/test_public_api.py` and the session lifecycle teardown tests.

### Interruption, cancellation, and agent history

- Barge-in stops audible output immediately: cancel TTS generation and clear
  buffered transport playback without waiting for agent or tool cleanup.
- Transport playback acknowledgements are authoritative for what the user
  heard. When a transport cannot acknowledge playback, EasyCat uses a
  conservative delivered-audio estimate rather than treating generated text
  as delivered text.
- Audio cutoff, model generation cancellation, and tool/action cancellation
  are separate policies. A non-interruptible tool may finish, but it never
  keeps audio playing.
- Assistant history commits only content known to have been delivered. The
  current user request survives interruption; undelivered assistant output
  does not.
- Every stateful bridge is session-owned unless its contract explicitly
  declares safe sharing. Each turn uses one authoritative history key.
  Framework checkpoints and conversational history are separate state and
  must not overwrite each other.
- Cancellation-resistant tasks remain owned until they finish or their
  resource boundary is forcibly terminated. A queue, send, journal append, or
  cleanup task cannot be detached merely to make a timeout appear bounded.

This supersedes generated-text history commits, bridge-global mutable history,
and coupling audible cutoff to tool completion. Enforce the shared semantics
through `tests/contracts/test_agent_bridge_contracts.py`, the bridge-specific
cancellation suites, and session cancellation/lifecycle tests.

### Journal completeness, privacy, and retention

The three debug modes have distinct, stable meanings:

| Mode | Journal contract |
| --- | --- |
| `off` | No session journal or captured artifacts. |
| `light` | Bounded in-memory turn, control, event, and error history; no per-frame audio artifacts. Any eviction is counted and surfaced. |
| `full` | Durable, replay-complete stage detail and configured artifacts. |

- Audio persistence is controlled by the separate capture-consent policy.
  Raising the debug mode never bypasses that policy.
- Raw journals are PII-bearing diagnostic data. The default write policy
  removes credentials and secrets while preserving replay-critical content.
  Irreversible PII redaction is an explicit policy choice.
- Redacted CLI views and coding-agent context exports apply their own
  share-safe projection regardless of the raw journal policy.
- Redaction, storage backend, durability, capacity, and retention are
  independent settings; changing one must not silently change another.
- Live export reads a stable journal and artifact snapshot or reports a
  sequence gap. It never presents a racing partial export as complete.

This supersedes treating light mode as per-frame capture, treating raw bundles
as share-safe, and coupling retention to redaction. Enforce the matrix in the
EasyConfig default tests, journal backend tests, bundle/export tests, and
redaction contract tests.

### Provider and extension boundaries

- `providers.py` owns structural protocols. Provider implementations use
  structural typing and do not need framework inheritance.
- Provider catalogs are the sole source of construction metadata:
  implementation/config classes, capabilities, credentials, install extras,
  probe modules, entry points, and sensitive API domains.
- Provider identity is role-qualified. STT, TTS, VAD, audio-processing, and
  transport registrations cannot silently overwrite one another because they
  reuse a display name or install extra.
- Factories, doctor, scaffolding, validation, and redaction derive their views
  from the catalogs. They do not maintain parallel provider lists.
- Runtime dependencies such as the event bus are injected centrally at the
  factory boundary.
- Provider failures map to the stable EasyCat error taxonomy and are published
  without awaiting application handlers while provider lifecycle locks are
  held.
- A public provider guarantee describes observable behavior. An accepted
  control request is not documented as a guaranteed transcript unless every
  shipped implementation and the installable contract kit prove it.

This supersedes name-only cross-catalog merges, hand-maintained capability
tables, and provider-specific exception surfaces. Enforce it through
`tests/contracts/`, `tests/testing/`, the provider session matrix, and public
API tests.

### Streaming audio format and clock semantics

- `AudioChunk.format` is authoritative at every boundary. EasyCat has no
  implicit process-wide sample rate.
- Resampling and other streaming conversion keep state across chunks. Splitting
  identical input at different chunk boundaries must not materially change the
  resulting audio, duration, or sample accounting.
- Conversion happens once at a named provider or transport boundary. Live
  paths use the selected quality backend when available and use the documented
  fallback only when it is not.
- The AEC far-end reference is the normalized audio accepted for outbound
  playback, not raw provider output or a pre-send approximation.
- VAD debounce, pre-roll, playback progress, and interruption estimates use
  audio position rather than event-loop scheduling time.
- Observability handlers never sit on the serialized audio delivery path in a
  way that can delay, abort, or mis-account an already accepted frame.

This supersedes stateless per-frame resampling, repeated implicit conversions,
and wall-clock VAD accounting. Enforce it with chunk-split equivalence,
duration-drift, AEC reference, VAD/pre-roll, and transport accounting tests.

### Toolchain, dependency majors, and extras

- CI uses an exact version in every `setup-uv` step; contributors may use any
  compatible `0.11.x` or `0.12.x` release accepted by `pyproject.toml`.
  `[tool.uv].required-version` accepts the compatible contributor range that
  can faithfully consume the committed lockfile. The `uv_build` requirement is
  an independent bounded build-backend range.
- A dependency extra never spans behaviorally incompatible SDK majors.
  PydanticAI and LangChain major lines are explicitly named and tested in
  isolated environments. PydanticAI v2 is the primary supported line, and its
  v1 compatibility extra has a documented retirement path; LangChain 0.3 and
  1.x remain separate supported lines.
- Extras describe dependencies, not whether a capability exists. Provider
  discovery comes from the catalog, so empty marker extras are not a capability
  registry.
- `quickstart` contains the smallest supported happy path. Heavy audio
  processing, AEC, alternative frameworks, and server stacks remain opt-in.
- The `all` extra is generated or mechanically validated from the supported
  dependency extras, with intentional license or major-version exclusions
  listed once.
- Minimum-version, current-version, and incompatible-major coverage run in
  distinct CI lanes; widening a range requires evidence from the corresponding
  lane.

This supersedes alternating between exact and ranged uv policy in the same
field, allowing one SDK extra to cross a major boundary, and using empty extras
as discovery metadata. Enforce it in `tests/test_dependency_policy.py`, extras
matrix tests, minimum-version CI, and release validation.

### Documentation generation and validation lanes

- Human explanations are authored prose. Route tables, generated command
  blocks, navigation fragments, and schema-derived references have exactly one
  generator and one checked-in output.
- Documentation guards validate commands, links, anchors, schemas, generated
  drift, and public contracts. They do not snapshot incidental prose wording.
- Generated-output guards remain in place even when prose-scanning tests are
  simplified. Tests that execute product behavior remain in the fast product
  loop rather than being reclassified wholesale as documentation guards.
- `validate quick` is deterministic, credential-free, and time-bounded.
  Optional extras, supported minimums, and real installed SDK contracts run in
  dedicated CI or nightly lanes. Live provider tests remain explicitly marked
  and separate.
- In-process documentation/script tests restore every module and global they
  replace; otherwise they use subprocess isolation.

This supersedes exact-prose guards, multiple generated sources of truth, and
moving behavioral coverage out of the fast loop for convenience. Enforce it
with the maintained docs, teaching, examples, validation, and contributing
guard recipes.

### Transport ingress and server ownership

- Public ingress authenticates and validates resource bounds before minting a
  stream token, constructing providers, or starting a session.
- `VoiceServer` and its shared transport/auth helpers own multi-session
  admission, startup rollback, draining, and shutdown. Runnable examples and
  scaffolds delegate to those helpers instead of maintaining alternate
  production lifecycle implementations.
- In-progress startup is not published as a live session. Shutdown cancels and
  rolls it back; successfully started sessions are registered before normal
  handling continues.
- Inbound audio and outbound event queues are byte/count bounded. Forced
  shutdown and stalled-send bounds use hard deadlines that do not wait
  indefinitely for cancellation acknowledgement.
- Public configuration additions preserve existing positional field order or
  are keyword-only.

This supersedes authenticate-after-allocation flows, independently managed
example servers, unbounded transport queues, and soft timeouts presented as
hard deadlines. Enforce it with transport contracts, server lifecycle tests,
telephony authentication tests, and public configuration compatibility tests.

## Layer Ownership

The highest-risk architectural failure mode is overlapping abstractions that
each own a different version of session construction, provider planning, server
lifecycle, or debugging. The **Must Not Own** column is the load-bearing half:

| Layer | Owns | Must Not Own |
|---|---|---|
| `Session` | One conversation/call/client pipeline and lifecycle. | Process routing, multi-client server policy, manifests, deployment auth. |
| `EasyConfig` | Detailed session/provider/transport/observability config. | Product-level run modes or process-level server lifecycle. |
| Journals/bundles | Durable truth for runtime inspection and replay. | Project/application manifest semantics. |
| Evals | CI-friendly assertions over conversations, journals, and runtime metrics. | Live transport implementation details. |

`Session` is the per-call pipeline object: STT, TTS, VAD, noise reduction, echo
cancellation, transport, agent bridge, event bus, turn state, the
start/stop/cancel/reset/send-text lifecycle, the journal sink, and telephony
helper attachment for a single session. Do not turn it into a server or a
registry — active session limits, readiness, auth, route mounting, and graceful
process shutdown belong above it.

`EasyConfig` is the complete declaration for one session. Anything that
constructs sessions should produce `EasyConfig` objects or accept user
factories that produce them, rather than bypassing `create_session()`.

Role resolution and resource construction are owned by different modules that
never import each other: `easycat.planning` resolves every pipeline role
statically (`planning/_resolution.py` decides once, `planning/provider_plan.py`
projects the result into the public `ProviderPlan`), while `config/_factory.py`
owns construction. The pure decisions both need — the live-provider predicates, the
noise-reduction switch, and the STT-native-endpointing turn policy — live in the
stdlib-only leaf `easycat._pipeline_decisions`, below both, and an Import Linter
independence contract keeps the two layers from reaching for each other.

## Non-goals

Permanently out of bounds: voice-to-voice realtime APIs, an EasyCat-native tool
API, an EasyCat-native MCP client or tool registry, an EasyCat-native
planner/router, EasyCat-native memory or a prompt compiler, an EasyCat-native
multi-agent abstraction beyond compatibility bridges, and a hosted
observability backend.

### Chained only: why voice-to-voice is out of scope

Chained voice pipelines (STT → agent → TTS) and voice-to-voice realtime
sessions (bidirectional audio streamed through one model) look similar at
30,000 feet and are fundamentally different at every altitude that matters for
a runtime:

| Axis | Chained pipeline | Voice-to-voice realtime |
|---|---|---|
| Audio flow | Discrete turns: user audio → transcript → agent → audio | Continuous bidirectional stream, no turn boundary |
| Latency target | P50 <1.0s, P90 <1.6s (acceptable conversational) | P50 <300ms, P90 <500ms (human-native) |
| State shape | Text history + delivered-audio ledger | Live multimodal session state owned by the model |
| Transcripts | Always available (STT output) | Partial, delayed, or absent |
| Interruption | Cancel TTS queue + patch text history | Session-level cancel signal to live model |
| Tool calls | Between turns | Mid-audio-stream while audio flows both ways |
| Cost model | STT seconds + text tokens + TTS characters | Audio input/output tokens (10–30× per-word cost) |
| Provider landscape | Deepgram, Cartesia, ElevenLabs, OpenAI STT/TTS | OpenAI Realtime, Gemini Live |
| Failure modes | STT errors, VAD false positives, TTS drift | Model hallucinations, WebSocket drops, audio token overruns |
| Debugging primitives | STT cassette replay, TTS cassette replay, turn-by-turn journal | Bidirectional audio cassette replay against live provider |

Serving both with one runtime would force every abstraction to satisfy both the
"discrete turn with clean STT/Agent/TTS boundaries" model and the "continuous
multimodal session" model, so every abstraction would compromise on both. The
`Stage` protocol would grow fused-stage escape hatches, the journal would need
partial deferred records, the interruption contract would need two code paths,
replay would need two fidelity stories, the debugger UI would need two views,
and users would need two mental models. The common-runtime savings are small;
the per-abstraction compromises compound everywhere.

The debug-first thesis is *only* credible if the runtime can answer "what
happened and can I replay it" uniformly. Chained pipelines support that
completely: STT outputs are captured, VAD and Smart Turn decisions are
byte-reproducible, TTS is cassette-replayable, and every stage boundary is
journaled. Voice-to-voice sessions do not: transcripts are provider-decided,
audio is bidirectional and huge, tool calls happen mid-stream without commit
boundaries, replay depends on the live provider API, and "which stage was slow"
collapses into "the model was slow, we don't know why".

Users who want voice-to-voice should use the provider SDK directly (OpenAI
Realtime, Gemini Live). EasyCat's contribution is a debug-first runtime for the
chained pipeline, and that is what it optimizes for end to end.

## Related Pages

- [Developer textbook](development/) — the chapter-by-chapter newcomer path
  through architecture, source ownership, tests, pitfalls, and change recipes.
- [Events reference](reference/events.md) — every public event type and when
  it fires.
- [EasyConfig field reference](reference/easyconfig.md) — every construction field.
- [Session lifecycle](reference/session-lifecycle.md) — start, stop/force,
  and postmortem journal reads.
- [Public API contract](public-api.md) — the stable top-level import surface.
- [Maintainer guide](../CLAUDE.md) — commands, guard recipes, and the short
  orientation map.
- [Observability](observability.md) — journals, bundles, the debugger UI,
  metrics, and traces.
