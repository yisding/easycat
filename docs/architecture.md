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
- **Event bus injection** — Deepgram and ElevenLabs providers require an
  `EventBus` injected at construction (they emit provider-scoped events).
  OpenAI providers do not.
- **Noop stubs** (`stubs.py`) — `NoopSTT`, `NoopTTS`, `NoopVAD`,
  `NoopTransport` for test isolation.

## Related Pages

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
