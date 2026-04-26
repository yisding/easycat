# Combined Cleanup Tasks

This plan combines `plan/cleanup-ideas-1.txt`,
`plan/cleanup-ideas-2.txt`, and `plan/cleanup-ideas-3.txt` after checking the
current repository state on 2026-04-25.

The source notes agree on the main direction: EasyCat has enough real runtime,
provider, telephony, and debug code to polish, but the cleanup should reduce
surface area and ownership ambiguity before adding more features. This file is
not a blind union of the three notes. Items that are already implemented,
stale, or not worth doing as written are called out at the end.

## Current-State Findings

- `src/easycat/session/_session.py` is still 2,961 lines and owns lifecycle,
  provider wiring, turn state, STT commit scheduling, TTS playback,
  journaling, telephony helpers, greeting, opt-out, text mode, and bundle
  export. Splitting it remains the highest-leverage cleanup.
- `src/easycat/__init__.py` is 578 lines and lazily exports 195 symbols. The
  lazy-loading mechanism is reasonable; the public symbol count is the problem.
- `EasyCatConfig` and `SessionConfig` are both public today. `EasyCatConfig` is
  the app-facing path; `SessionConfig` is really raw pipeline wiring and should
  not be presented as an equivalent user config.
- The WebRTC queue issue from cleanup idea 2 still exists: `_handle_offer()`
  calls `_reset_audio_queue()`, which swaps the queue object under any active
  `receive_audio()` consumer.
- STT/TTS provider files still duplicate package-version lookup, WebSocket
  receive loops, end/drain behavior, word timestamp parsing, and structured
  provider error emission.
- WebSocket and Twilio transports still have server-level and
  connection-level classes with duplicated message parsing and send paths.
- `stages/` is still mostly journaling wrapper code, at 1,573 lines. The layer
  may be useful, but it needs a smaller generated/wrapped shape and typed
  contracts.
- Several debug/replay criticisms from the source docs are now stale:
  `ReplayRunner` calls `check_provider_versions()`, `RunBundle.from_partial_journal()`
  exists, inline artifacts are loaded back from the manifest, and the debugger
  has `/ws` plus artifact error handling.
- CLI basics are stronger than some notes imply: `easycat` has a journey menu,
  tests cover `--help`, and `python -m easycat.cli` exists. Missing pieces are
  `python -m easycat`, replay/inspect/demo commands, and release-grade CLI
  smoke tests.
- README drift remains real: the top provider list omits Cartesia, TEN VAD is
  described as open-source despite the non-permissive-license note in
  `pyproject.toml`, and the quickstart extra text says it bundles TEN VAD even
  though it does not.

## Principles

- Prefer small PRs with tests over one large rewrite.
- Keep the working pieces: event bus, provider protocols, config presets,
  journal/bundle core, debugger UI, teaching ladder, factory registries, and
  the lazy-loader mechanism.
- Break compatibility where it buys a simpler public model, but avoid churn
  that only renames working internals.
- First remove ambiguity in ownership and contracts, then shrink modules.

## Phase 0: Correctness, Security, And Trust

### 0.1 Fix WebRTC Ingress Queue Ownership

- Replace `_AudioQueueMixin._reset_audio_queue()` use in
  `WebRTCTransport._handle_offer()` with a stable ingress object.
- Either keep one queue and drain stale entries under a generation id, or split
  WebRTC into listener and per-peer endpoint objects so `Session` always owns a
  stable endpoint queue.
- Ensure peer replacement explicitly closes or ends the previous audio
  generation before accepting the new one.
- Add tests for:
  - first offer after `session.start()`;
  - repeated offers while `receive_audio()` is already awaiting;
  - peer replacement while old audio is still queued;
  - ICE failure and browser disconnect;
  - disconnect while `send_audio()` is pending.

### 0.2 Fail Fast On Explicit Backend Strings

- Change `VADConfig.backend` and `NoiseReducerConfig.backend` to `Literal` or
  enum-backed values.
- Add explicit `fallback_policy` fields for cases where fallback is intended.
- Preserve `backend="auto"` as a deliberate fallback mode.
- For `EchoCancellationConfig(enabled=True)`, stop silently returning
  passthrough unless a fallback policy says that is acceptable.
- Add typo and missing-dependency tests for VAD, noise reduction, and AEC.

### 0.3 Make STT Stream Termination Ownership Explicit

- Decide whether `STTBase.end_stream()` or each subclass owns the final
  sentinel and drain behavior.
- Standardize close idempotency across OpenAI batch, OpenAI Realtime,
  Deepgram, ElevenLabs, and Cartesia STT.
- Add provider contract tests that late final transcripts are emitted before
  the stream closes.
- Add cancellation tests for each streaming provider receive loop.

### 0.4 Harden Transport Defaults

- Change library server defaults to loopback where possible:
  `WebSocketTransportConfig.host`, `WebRTCTransportConfig.host`, and debugger
  surfaces should default to `127.0.0.1`.
- For surfaces that commonly need remote access, require explicit
  `allow_remote=True` or equivalent.
- Add a shared `ConnectionPolicy` covering origin, path, query/session token,
  max payload size, compression, ping timeout, close timeout, and close reason.
- Disable compression by default for audio WebSockets.
- For WebRTC, avoid wildcard CORS around TURN credentials and default to
  same-origin unless configured.
- For Twilio Media Streams, add a signed one-time stream token in TwiML and
  validate it on connect.

### 0.5 Fix Documentation Drift

- Update README provider lists to include Cartesia for STT and TTS.
- Correct TEN VAD language: it is optional and has a non-permissive license;
  do not describe it as open-source.
- Correct quickstart extra docs: `quickstart` includes local audio, OpenAI,
  RNNoise dependencies, NumPy, and ONNX Runtime, but not `ten-vad`.
- Ensure teaching docs and README consistently describe the current
  ZIP/NDJSON bundle format.
- Add tests for README snippets or at least a smoke job for the documented
  first-success path.

### 0.6 Repo Hygiene

- Add `.codex/` and `.pipecat-bench/` to `.gitignore` if these are local
  workspaces rather than source.
- Decide whether `plan/` is release packaging input. If not, move planning
  docs to `rfcs/`, `.github/workstreams/`, or exclude them from package builds.
- Add a wheel/sdist contents denylist for caches, local artifacts, plan drafts,
  `.easycat/`, and generated template junk.

## Phase 1: Public API, Config, And Onboarding

### 1.1 Choose One App-Facing Config

- Make one public app config the canonical path. Preferred name:
  `VoiceAgentConfig`, because it is clearer than generic `Config` and less
  package-redundant than `EasyCatConfig`.
- Rename `EasyCatConfig` to `VoiceAgentConfig` across docs, examples, and
  tests. If a transition alias is kept before launch, keep it documented as
  temporary.
- Rename `SessionConfig` to `_SessionConfig` or `PipelineConfig` and move it
  to an advanced/internal module.
- Add `Session.from_providers(...)` for users who truly need raw provider
  wiring, rather than asking them to construct `SessionConfig`.
- Replace `record_to` monkey-patching of `session.stop` and
  `session.shutdown` with a real lifecycle hook owned by `Session`.

### 1.2 Curate The Top-Level Namespace

- Keep the lazy-loader mechanism.
- Cut `easycat.__all__` from 195 symbols to a documented target of at most 70.
- Keep top-level exports focused on:
  - `VoiceAgentConfig`;
  - `create_session`;
  - `create_text_session`;
  - `run`;
  - `Session`;
  - stable provider protocols;
  - core event bus/events;
  - `RunBundle` or an `inspect` entry point if debug bundles are public.
- Move rare telephony events, action internals, stage internals, provider
  config dataclasses, debug internals, and test helpers to submodules.
- Add a golden `__all__` snapshot test and a short public API contract doc.

### 1.3 Keep `run()`, But Make Feedback Explicit

- Keep `run(config)` as the best first-run helper.
- Remove duplicate signal-handling code by sharing one internal signal helper.
- Add an explicit feedback option, for example `run(config, feedback="auto")`,
  where `"auto"` keeps the current TTY behavior.
- Move `quick.speak()` out of the top-level namespace. Either delete it if it
  remains unused, or move it to `easycat.recipes`.
- Keep `transcribe_file()` as a recipe/teaching helper, not as core API.

### 1.4 CLI Product Shape

- Add `src/easycat/__main__.py` so `python -m easycat` works.
- Add `easycat inspect <bundle>` as a user-friendly alias around
  `bundles show` or the debugger.
- Add `easycat replay <bundle>` once replay behavior is productized.
- Add `easycat demo` only if it runs a real deterministic demo without API
  keys, or keep it out of help until it exists.
- Keep the journey menu, but ensure it only lists implemented commands.

### 1.5 Examples And Teaching

- Keep three golden root examples: local mic, browser WebRTC, and Twilio.
- Move provider/framework variants into `examples/providers/`,
  `examples/agents/`, and `examples/advanced/`.
- Add `examples/README.md` with purpose, required extras, env vars, and
  expected command for every example.
- Expand `tests/test_examples.py` beyond import checks for the golden examples
  using fake providers/transports.
- Keep `docs/teaching/` prominent in README; it is a real asset.
- Keep planning docs separate from teaching docs. The teaching chapters now
  exist through 15 under `docs/teaching/`; do not recreate them from `plan/`.

## Phase 2: Runtime Ownership And Session Split

### 2.1 Extract A Journaling Sink First

- Move `_subscribe_journal_sink`, `_append_journal_record`, and
  `_store_journal_artifact` out of `Session` into a
  `session/journal_sink.py` component.
- Have `Session` emit events and call narrow sink methods instead of owning
  event-to-journal translation inline.
- Preserve post-stop journal inspection semantics:
  `session.journal.read()` and `session.export_debug_bundle(...)` must still
  work after `stop()` and `shutdown()`.
- Add focused tests for event subscription, artifact storage, markdown
  stripping records, queue-drop records, and post-shutdown read-only behavior.

### 2.2 Add Runtime Capability Protocols

- Add runtime-checkable protocols for capabilities currently detected through
  concrete classes or private attributes:
  - playback acknowledgements;
  - clear-audio support;
  - transport delivery reporting;
  - identity sink binding;
  - passthrough/no-op providers;
  - health checking;
  - closeability.
- Replace direct `isinstance(..., NoopSTT)`, `PassthroughAEC`, and concrete
  transport checks in `Session`.
- Keep `stubs.py` until runtime defaults are redesigned; do not move it
  wholesale to tests while `Session` and `create_text_session()` depend on it.

### 2.3 Introduce `RuntimeScope`

- Create a single owner for background work, backed by `TaskGroup` or an
  equivalent compatibility wrapper plus `AsyncExitStack`.
- Every runtime task should have a name, owner component, cancellation policy,
  close deadline, and journal lifecycle records.
- Migrate raw `asyncio.create_task()` calls from Session, STT consumption, TTS
  playback, segment commits, greetings, heartbeat, and health checkers.
- Collapse duplicated `stop()` and `shutdown()` cleanup into one lifecycle
  owner with graceful versus forceful policies.

### 2.4 Create One Turn State Object

- Introduce `ActiveTurn` as the single source for turn id, generation,
  cancel token, STT segments, playback accounting, and interruption metadata.
- Remove duplication between `TurnManager._cancel_token`, `Session._turn`,
  `Session.turn_state`, and `_turn_generation`.
- Have `TurnManager` return or emit `ActiveTurn` creation/end events rather
  than calling back into `Session`.
- Move barge-in from callback style to command/event style.
- Keep playback-mark gating behavior covered by tests before and after the
  migration.

### 2.5 Move STT Consumption Out Of Session

- Extract an `STTConsumer` or `TurnRunner` that owns:
  - `start_stream()`, `send_audio()`, `commit_segment()`, and `end_stream()`;
  - final futures;
  - pause-to-segment-commit scheduling;
  - final transcript aggregation;
  - late event handling and stream cancellation.
- Move the segment commit scheduling currently in Session into this owner or
  into the turn actor.
- Add tests for VAD pause, smart-turn pause, provider `commit_segment=False`,
  late finals, and shutdown while a commit is sleeping.

### 2.6 Move TTS Playback Out Of Session

- Extract a `TTSProducer` or `PlaybackWorker` that owns:
  - agent-stream to TTS payload preparation;
  - sentence boundaries;
  - output processors;
  - TTS queueing;
  - playback suppression;
  - transport send accounting;
  - playback marks and clear-audio behavior.
- Move `tts_synthesizer.py` under `session/` or make it the backing
  implementation of this component.
- Ensure greeting playback is serialized with turn lifecycle or cancelled on
  `TurnStarted`, so a user speaking during the greeting cannot produce
  implicit audio conflicts.

### 2.7 Split Text Mode

- Move text-mode behavior into a `TextSession` component or concrete class.
- Keep `create_text_session()` as public API.
- Stop exporting `session/_text.py` internals; rename helper modules to match
  what they contain.

## Phase 3: Providers, Transports, And Contracts

### 3.1 Deduplicate Provider Helpers

- Add `easycat/_version.py` or `easycat/providers/_helpers.py` with shared
  package-version lookup.
- Add shared word timestamp parsing tolerant of both `word` and `text` keys.
- Add shared auth/header helpers for Bearer, token, `xi-api-key`, and
  `X-API-Key` variants.
- Add shared provider error emission that maps exceptions and provider error
  messages into structured `ProviderError` events.

### 3.2 Add `WebSocketSTTBase`

- Build a shared base/mixin for streaming WebSocket STT providers that owns:
  - receive loop;
  - JSON parsing;
  - byte-frame ignore policy;
  - close/drain timeout;
  - cancellation;
  - provider error emission;
  - common segment commit flow where supported.
- Keep provider subclasses focused on URL construction, start payloads, audio
  payload encoding, and message-specific transcript extraction.
- Add contract tests for Deepgram, ElevenLabs, Cartesia, and OpenAI Realtime
  using fake WebSocket servers.

### 3.3 Normalize Provider Configs

- Standardize public provider config field names:
  - `model`, not `model_id` in user-facing configs;
  - `base_url`, not mixed `base_url`/`ws_url`;
  - explicit `sample_rate` and `audio_format` policy.
- Delete `tts/factory.py` model-field workaround after config names converge.
- Add a shared provider config base only if it materially reduces duplication;
  avoid inheritance if small helper functions are enough.

### 3.4 Add Provider Specs And Capabilities

- Replace scattered STT/TTS factory maps and duck-typed version/event-bus
  injection with provider specs.
- Each `ProviderSpec` should define name, role, config type, factory, required
  extra, dependency check, audio contracts, streaming support, commit support,
  SSML/pause/marker/pronunciation support, and error mapping.
- Add contract tests that every registered provider reports capabilities and
  version info in a stable shape.

### 3.5 Normalize Audio And TTS Input Policy

- Centralize PCM16 mono validation, frame-size checks, and resampling helpers.
- Replace `supports_ssml: bool` with a `TTSInputPolicy` describing plain text,
  native SSML, stripped SSML, pauses, markers, and pronunciation support.
- Use capabilities to decide when transport output formats should drive TTS
  sample-rate selection.

### 3.6 Split Listeners From Endpoints

- Make per-client/per-call transport endpoints the session-level shape.
- Rename server-owning transports to listener classes:
  `WebSocketListener`, `TwilioMediaListener`, and `WebRTCListener` if WebRTC is
  reworked the same way.
- Keep `WebSocketConnectionTransport` and `TwilioConnectionTransport` as the
  canonical endpoint implementations, or merge them with the server classes
  using `.listen(...)` and `.from_connection(...)` constructors.
- Add lifecycle states: `NEW`, `LISTENING`, `CONNECTING`, `CONNECTED`,
  `DRAINING`, `CLOSED`, `FAILED`.
- Add transport observability for queue depth, dropped frames, send latency,
  clear latency, playback mark ACK latency, reconnect attempts, WebRTC ICE
  state, RTCP stats, Twilio sequence gaps, resampling cost, and close reason.

## Phase 4: Agent Bridges

### 4.1 Extract Atomic Interruption Ordering

- Add a shared bridge base or mixin that implements the four-step ordering:
  plan, record committed state, apply mutation, record success/failure.
- Bridges should override only planning and mutation application.
- Cover OpenAI Agents, PydanticAI, Responses API, and generic workflow bridges.

### 4.2 Replace `auto_adapt_agent` Ladder With A Registry

- Add bridge adapter registrations as `(predicate, factory)` pairs.
- Keep special validation for URL agents and unsupported realtime-shaped
  objects.
- Stop mutating `AgentRunner._agent` and `_is_bridge` from the factory.
- Avoid relying on private attributes and parameter-name inspection except in
  the explicit generic-workflow adapter.

### 4.3 Clarify History Ownership

- Choose one history model:
  - Session owns canonical turn history and passes it via `AgentTurnInput`, or
  - each bridge owns framework-native history and exposes explicit snapshot and
    post-processing hooks.
- Do not keep the current implicit mixture of dict lists, PydanticAI model
  responses, `previous_response_id`, and `AgentRunner` history.
- Split `replace_last_assistant_text` and `append_interruption_note` into an
  optional history post-processor protocol so shallow/stateless bridges can be
  honest no-ops.

### 4.4 Formalize MCP And Cancellation

- Replace private `_mcp_servers`, `_model`, and `_api_key` mutation with public
  bridge initialization/configuration APIs.
- Define one cancellation contract for mid-turn and end-of-turn interruption.
- Document which bridge capabilities support streaming text, tool deltas,
  history rewriting, interruption, MCP, snapshots, and closeability.

### 4.5 Fix Agent Event Translation

- Extract a small `EventTranslator` base for OpenAI Agents, PydanticAI, and
  Responses API translators.
- Keep `AgentBridgeEvent.tool_name`, but make PydanticAI tool deltas/results
  retain names by tracking pending tool calls.
- Add tests for interleaved tool calls so journals remain readable.

## Phase 5: Debug, Replay, Events, And Stages

### 5.1 Keep The Journal Core, But Type The Records

- Add `record_schema_version` to `JournalRecord`, separate from bundle format
  version.
- Move toward a discriminated record union for stage, event, framework,
  control-signal, error, and replay records.
- Store control-signal fields as first-class record fields, not only nested
  under `data`.
- Add bundle schema validation tests.

### 5.2 Share Journal Filtering And Serialization

- Extract a `JournalSlice` helper used by both `JournalView` and `RunBundle`.
- Move parallel journal serialization/deserialization paths into one
  `runtime/serialization.py` module.
- Keep backward-compatible loading only where it is cheap and explicit.

### 5.3 Reduce Stage Boilerplate

- Decide between:
  - a generated `JournalingStageWrapper` around providers, or
  - a smaller typed stage layer with `Stage[In, Out]` and typed ports.
- Do not keep eight handwritten wrappers that only repeat
  `snapshot_state`, `execute`, `handle_upstream`, and replay boilerplate.
- If stages remain public, update `Stage.replay()` docs and make replay
  behavior real for the stages that claim it.
- Remove or inline `runtime/nondeterministic.py` if it remains a single
  constant after the stage cleanup.

### 5.4 Be Precise About Replay Claims

- Keep artifact replay and audio replay claims.
- Add byte-determinism tests for ARTIFACT plus fast replay.
- Either implement live agent replay as a real workflow or stop promising it
  as shipped behavior.
- Keep debugger manifests honest: live sessions may support export but not
  replay; offline bundles may support replay.

### 5.5 Event System Cleanup

- Split `events.py` only if the public API is first narrowed:
  `events.core`, `events.provider`, `events.telephony`, `events.actions`,
  `events.errors`.
- Add typed event union tests for the stable event contract.
- Add EventBus subscription tokens.
- Add dispatch policies: inline, task, queue, and best-effort.
- Track handler latency and failures so user callbacks cannot silently stall
  audio-critical paths.

### 5.6 Product Namespace For Debugging

- Rename or alias `easycat.debug` and `easycat.debugger` into one product
  namespace such as `easycat.inspect`.
- Keep low-level runtime code under `easycat.runtime`.
- Keep UI server code separate from bundle format/loading code.

## Phase 6: Telephony

### 6.1 Be Explicit That Telephony Is Twilio-First

- Short-term: own the Twilio focus. Rename generic names that are Twilio-only,
  for example `OutboundCallManager` to `TwilioOutboundCallManager`, unless a
  real provider abstraction is added in the same workstream.
- Longer-term: if other carriers are in scope, introduce a
  `TelephonyProvider` protocol and move Twilio-specific behavior behind it.

### 6.2 Split Call Classification Modules

- Move shared voicemail/screening heuristics into:
  - `voicemail_classification.py`;
  - `voicemail_state_machine.py`;
  - `coherence.py`.
- Delete or rename `ml_voicemail.py`; it is heuristic code, not ML.
- Extract `ClassificationGate` from `call_state.py`.
- Move `TWILIO_AMD_MAP` out of `voicemail.py` so outbound calling does not
  depend on voicemail internals.

### 6.3 Turn Compliance Into Policy

- Replace outbound `ValueError` blocking with structured
  `CallPreflightResult`.
- Emit `CallBlocked` with rule id, jurisdiction, local time, DNC source, and
  audit metadata.
- Replace the small built-in area-code timezone map with a
  `PhoneMetadataProvider` hook for E.164 normalization, timezone, consent, DNC,
  and quiet-hours policy.
- Add enforceable AI disclosure events: `DisclosureRequired`,
  `DisclosureSpoken`, and a gate that prevents outbound agent speech until the
  disclosure is queued or explicitly waived.

### 6.4 Tighten Twilio Stream Handling

- Validate DTMF using the existing parser instead of emitting raw digits.
- Validate stream id and active stream state before accepting media/control
  frames.
- Track Twilio sequence and timestamp gaps as metrics.
- Add tests for invalid stream ids, stale stream ids, invalid DTMF, sequence
  gaps, and stop/start races.

### 6.5 Productize Supervisor Audio

- Add auth for supervisor listeners.
- Add per-listener drop counters and observability.
- Add recording consent and redaction hooks.
- Add audit events.

## Phase 7: Packaging, CI, And Release Bar

### 7.1 Project Metadata

- Add a root `LICENSE`.
- Add `license`, `authors`, classifiers, keywords, and project URLs to
  `pyproject.toml`.
- Add `twine check` or equivalent metadata validation in CI.

### 7.2 CI Matrix And Type Checking

- Test Python 3.11 because `requires-python` is `>=3.11`.
- Keep 3.12 and 3.14, and add 3.13 if CI time allows.
- Add pyright or basedpyright because the package ships `py.typed`.
- Add `pyright --verifytypes easycat` only after the public API is curated.

### 7.3 Packaging Smoke Tests

- Build wheel and sdist in CI.
- Install the wheel in a clean virtualenv.
- Run:
  - `easycat --help`;
  - `easycat init`;
  - `python -m easycat`;
  - import of documented top-level API only.
- Add a package contents denylist for caches, local workspaces, generated
  artifacts, stale templates, and unreleased planning docs.

### 7.4 Test Reliability

- Add `pytest-timeout`.
- Add leaked-task checks for async tests.
- Prefer event-driven waits over sleeps and polling.
- Replace bind-close-reuse free-port helpers with
  `unused_tcp_port_factory` where possible, or keep sockets bound until server
  startup.

### 7.5 Provider And Performance Testing

- Split protocol tests from live provider tests.
- Use deterministic PCM fixtures for protocol and latency-shape CI.
- Keep live STT/TTS/Twilio tests as manual or scheduled canaries with explicit
  secrets:
  `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`,
  `CARTESIA_API_KEY`, and Twilio secrets.
- Convert benchmark scripts into commands such as `perf run` and
  `perf compare --baseline`.
- Store benchmark results as CI artifacts; gate only stable microbenchmarks in
  PR CI.

## Suggested PR Sequence

1. Documentation and hygiene: README drift, `.gitignore`, root LICENSE,
   package metadata.
2. WebRTC queue correctness with regression tests.
3. Backend validation and AEC/noise/VAD fallback policy.
4. Provider DRY helpers: package version, word timestamps, provider errors.
5. `WebSocketSTTBase` for streaming STT providers.
6. Public API snapshot and top-level export cull.
7. `VoiceAgentConfig` rename plus internal `_SessionConfig`/provider wiring
   path.
8. `Session` journaling sink extraction.
9. Runtime capability protocols and removal of concrete-class checks.
10. RuntimeScope and the first Session component extractions.

## Do Not Implement As Written

- Do not replace the lazy-loader mechanism. Keep it; reduce `__all__`.
- Do not rename `EasyCatConfig` to generic `Config`. Use a domain name such as
  `VoiceAgentConfig`, or keep `EasyCatConfig` if the team decides the churn is
  not worth it.
- Do not move `stubs.py` entirely to tests while `Session` and
  `create_text_session()` use no-op defaults. First replace concrete checks
  with capabilities and decide whether no-op providers are part of public
  testing support.
- Do not implement `check_provider_versions()` wiring; it is already called by
  `ReplayRunner`.
- Do not implement `RunBundle.from_partial_journal()` from scratch; it exists
  and has tests.
- Do not fix inline artifact loading as if missing; `RunBundle.load()` already
  reconstructs inline base64 artifacts.
- Do not add debugger `/ws` as if missing; it exists.
- Do not add missing-artifact API errors as if missing; debugger artifact
  routes return invalid-ref and not-found errors.
- Do not wire `health_check.py` into Session as if absent; Session already
  starts `PeriodicHealthChecker` for providers that expose `health_check`.
- Do not wire `VADProvider.configure()` as if never called; the VAD factory
  calls it when constructing VAD backends.
- Do not add a smart-turn factory or main-pipeline integration as if absent;
  `create_smart_turn()` and `EasyCatConfig.smart_turn` already feed the turn
  manager endpoint detector.
- Do not add top-level CLI journey menu work as a first task; it already has
  tests. Focus CLI work on missing commands and `python -m easycat`.
- Do not spend cleanup budget adding doc comments to `session_manager.py` and
  `supervisor.py`; both already have clear module docstrings.
- Do not treat `docs/teaching/14` and `docs/teaching/15` as missing; they now
  exist under `docs/teaching/`.
