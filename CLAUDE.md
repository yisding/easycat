# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EasyCat is a Python voice bot framework that plugs into OpenAI Agents SDK or PydanticAI. It handles the full audio pipeline: noise reduction → VAD → STT → agent → TTS, with pluggable providers at each stage.

## Commands

```bash
uv sync --group dev              # Install project + dev tools
uv run pytest                    # Run full test suite
uv run pytest tests/stt/test_stt_openai.py              # Run one test file
uv run pytest tests/test_metrics.py::TestLatencyStats::test_record_and_stats  # Run one test
uv run ruff check .              # Lint
uv run ruff format .             # Format
uv run python examples/ws_server.py  # Run an example
```

## Architecture

**Pipeline flow:** Transport (audio in) → NoiseReducer → EchoCanceller → VAD → STT → [SmartTurn] → Agent → TTS → Transport (audio out). The `EchoCanceller` also consumes TTS output as reference audio (fed in by `session/_audio_router.py`) so it can subtract the bot's own playback from the captured mic signal.

**Key modules:**
- `session/` — Package containing the core orchestrator. Key files:
  - `_session.py` — `Session` class. Owns the public lifecycle API (start/stop/cancel_turn/reset_state/send_text/etc.) and turn-pointer state. `Session.__init__` is now a short field-assignment shell that ends with a single `build_session(self, cfg)` call — a newcomer can scan the constructor in under a minute.
  - `_builder.py` — `build_session(session, cfg) -> SessionComponents`. Owns ALL collaborator construction that used to be inlined in `__init__`: the 7 stages, the shared `RunContext`, the `no-turn` `TurnContext`, the journal sink, the outbound audio queue, and every collaborator (AudioRouter/STTCommitter/TTSScheduler/CancelOrchestrator/TurnRunner/GreetingController/OptOutPolicy), plus their deferred event-bus subscriptions and TurnManager bindings. Returns a frozen `SessionComponents` bundle the constructor unpacks onto private fields.
  - `_wiring.py` — `SessionWiringContext`, a typed frozen dataclass of late-binding getters/setters (current_turn, is_running, enable_* flags, provider/agent getters, emit, drain_session_actions, caller_id_system_message, stop, …) built once from the live Session and passed to every collaborator constructor in place of ~40 inline lambdas. Also holds `_SessionTurnHandle` (the `TurnHandle` adapter). Imports `Session` only under `TYPE_CHECKING`.
  - `_greeting.py` — `GreetingController`. Subscribes itself to `CallAnswered` and speaks the configured greeting once (warm-transfer re-answer ignored) via the bypass synth path.
  - `_opt_out.py` — `OptOutPolicy`. Subscribes to `STTFinal`; on a TCPA opt-out phrase it adds the caller to the DNC list, emits `OptOutDetected`, and enqueues `EndCallAction` (or falls back to scheduling `Session.stop()`). `Session.dnc_list` delegates here.
  - `_caller_id.py` — `CallerIdState`. Holds the caller/callee identity + exposure policy and renders the caller-ID system message. `Session.call_identity` / `caller_id_exposure` delegate here; `private_identity` is the raw value used by `config.py` and opt-out.
  - `_telephony_facade.py` — `TelephonyFacade` exposed as `session.telephony`. Wraps the helper list with `.get(type)` plus typed accessors (`outbound_call_manager`, `outbound_call_state_machine`, `number_health_monitor`, `call_disposition_tracker`). `session.get_helper` delegates to it.
  - `_streaming.py` — `consume_agent_stream()` translates agent stream events into TTS payloads on sentence boundaries.
  - `_turn_runner.py` — Drives a single turn end-to-end (agent run → streaming → TTS scheduling), holding the logic that used to be inlined in `_session.py`.
  - `_audio_router.py` — Routes captured audio through noise reduction / echo cancellation and feeds TTS output back as AEC reference audio.
  - `_tts_scheduler.py` — `TTSScheduler.prepare()` builds and normalizes TTS payload text (the former `_tts_helpers.py` job) and schedules synthesis/playback.
  - `_stt_committer.py` — Commits finalized STT transcripts into the turn lifecycle.
  - `interruption.py` — Audio-byte estimation for barge-in: maps TTS output back to what the user heard.
  - `text.py` — Sentence splitting, markdown checking, speech energy detection, and spoken-text timeline normalization (`_text_for_estimation_timeline`). The audio-byte→text estimation itself (`_estimate_text_spoken`) lives in `interruption.py`.
  - `_types.py` — `SessionConfig`, `TurnState`, `Agent` protocol.
- `config.py` — `EasyConfig` (simplified, auto-wires OpenAI providers) and `SessionConfig` (advanced, explicit providers). `create_session()` factory builds a wired Session.
- `events.py` — `EventBus` pub/sub with sync/async handlers. Two event layers: provider-scoped (`STTEvent`, `TTSEvent`) emitted by providers, mapped to EasyCat-level events (`STTFinal`, `TTSAudio`, `TurnStarted`, etc.) by Session.
- `providers.py` — `@runtime_checkable` Protocol definitions for all provider interfaces (`STTProvider`, `TTSProvider`, `VADProvider`, `Transport`, `NoiseReducer`, `EchoCanceller`). Providers use duck typing, not inheritance.
- `turn_manager.py` — 5-state FSM (IDLE → USER_SPEAKING → USER_PAUSED → PROCESSING → BOT_SPEAKING) with pre-roll buffering and interruption detection. Supports VAD (automatic) and PUSH_TO_TALK turn modes.
- `runtime/` — Journal-based debug-first runtime. `ExecutionJournal` records events, spans, and metrics. `JournalView` provides query access. The journal is the single source of truth for all observability.
- `stages/` — Pipeline stages wrapping providers with a uniform `execute` / `snapshot_state` / `handle_upstream` surface and optional journal recording. `Stage` protocol defined in `stages/base.py`.
- `debug/` — `RunBundle` for serializing/loading complete session recordings. `load_bundle()` for test fixtures.
- `smart_turn.py` — Optional ONNX-based endpoint detection that classifies whether a user has finished speaking, enabling faster turn transitions without waiting for silence timeout.
- `_turn_context.py` (package root) — `TurnContext` per-turn state (timing, playback tracking, cancel token; created fresh each turn) and the `TurnHandle` protocol. Lives at the root as a leaf (depends only on `cancel.py`) so both `session/` and the lower `stages/` layer import it downward — preserving the `Session → Stages → Providers` direction without an import cycle.

**Provider subpackages** (`stt/`, `tts/`, `transports/`, `telephony/`): one provider per file, each implementing the corresponding Protocol. Base classes (`STTBase`, `TTSBase`, `_ServerTransportBase`) provide shared plumbing.

**Agent bridges** (`integrations/agents/`): `ExternalAgentBridge` protocol (single contract between Session and agents) with implementations `OpenAIAgentsBridge`, `PydanticAIBridge`, `GenericWorkflowBridge`, `RemoteResponsesAPIBridge`, `LlamaAgentsBridge`, `LangChainBridge`, and `LangGraphBridge`. `AgentRunner` (in `integrations/agents/_agent_runner.py`) implements `ExternalAgentBridge` by wrapping a simple `async run(text) -> str` object — used for basic agents that need timeout/cancellation/history. `auto_adapt_agent()` in `_factory.py` detects known framework objects and returns the right bridge.

**Dual-backend fallback:** VAD (`create_vad` auto: Silero → FunASR → TEN → Krisp; raises if none resolve), noise reduction (`create_noise_reducer` auto: Krisp → RNNoise → passthrough), and echo cancellation (`create_echo_canceller` from `EchoCancellationConfig`: LiveKitAEC when enabled and available, else `PassthroughAEC`; `EasyConfig` derives a transport-aware default via `enable_echo_cancellation`). VAD and noise reduction can each be forced to a single backend via `VADConfig.backend` / `NoiseReducerConfig.backend`.

## Key Patterns

- **Protocol over inheritance** — all providers defined as `typing.Protocol` in `providers.py`
- **Async-first** — all I/O is async; providers are async iterators
- **Cooperative cancellation** — `CancelToken` (not exceptions) for turn/TTS cancellation
- **Factory functions** — `create_session()`, `create_vad()`, `create_noise_reducer()`
- **Provider registries** — `stt/factory.py` has a central `_PROVIDER_TO_CONFIG` dict and `tts/factory.py` has a central `_PROVIDERS` dict, each mapping a provider name to its `(provider class, config class)` pair. To add a new STT/TTS provider: add an entry to the registry and a corresponding config dataclass.
- **Event bus injection** — Deepgram and ElevenLabs providers require an `EventBus` injected at construction (they emit provider-scoped events). OpenAI providers do not.
- **Noop stubs** (`stubs.py`) — `NoopSTT`, `NoopTTS`, `NoopVAD`, `NoopTransport` for test isolation

## Session Lifecycle

- `await session.stop()` is the single public teardown verb: `force=False` (default) drains in-flight work gracefully, `force=True` cancels it first. `async with session:` is the preferred idiom (it calls `stop(force=True)` on exit); `session.shutdown()` remains as a thin alias for `stop(force=True)`
- Backend teardown (SQLite/Litestream/libSQL/artifact stores) and the journal clean-close marker are handled internally by `stop()` via the private `Session._destroy()` / `Session._close()` primitives — these are not public entry points
- After a clean `stop()`, `session.journal.read()` and `session.export_debug_bundle(...)` must still work through the preserved read-only postmortem view

## Style

- Python ≥3.11, typing-first
- 4-space indent, 99-char line limit (ruff)
- Ruff rules: E, F, I, W, UP
- Commit format: `<scope>: <imperative summary>` (e.g., `stt: normalize partial transcript events`)

## Testing

- pytest with pytest-asyncio (`asyncio_mode = auto`)
- `@pytest.mark.integration` for live API tests (skipped without credentials)
- Tests mirror source structure: `tests/stt/`, `tests/tts/`, `tests/session/`, etc.
