# EasyConfig Field Reference

This page is the handwritten reference for every `EasyConfig` field, the
grouped config objects, and the legacy top-level aliases. It is kept honest
by `tests/test_docs_index.py::test_easyconfig_reference_tracks_config_fields`,
which compares each section against the live dataclass fields — the same
pattern that keeps [public-api.md](../public-api.md) in sync with
`easycat.__all__`.

`EasyConfig` is keyword-only. The zero-config path is
`EasyConfig(agent=my_agent)` with `OPENAI_API_KEY` set; everything else below
is opt-in. Build the session with `create_session(config)`.
Run `uv run easycat docs --audience app-builders` for the surrounding route
map, and see the [session lifecycle reference](session-lifecycle.md) for what
happens after construction.

## Construction Fields

Every keyword `EasyConfig(...)` accepts as a real (stored) field:

- `agent` — the conversational brain: an OpenAI Agents SDK / PydanticAI /
  LangChain / LangGraph / LlamaAgents object, a plain
  `async run(text) -> str` object, an `ExternalAgentBridge`, or a remote
  Responses API URL string. Auto-adapted to the right bridge.
- `agent_model` — model identifier for remote Responses API agents; required
  when `agent` is a URL string.
- `remote_agent_api_key` — API key forwarded to a remote Responses API
  agent.
- `agent_runner` — optional `AgentRunnerConfig` tuning the timeout, history,
  and cancellation wrapper applied to plain `async run` agents.
- `wrap_agent` — when `True` (default), plain agents are wrapped in
  `AgentRunner`; set `False` only when passing a fully-constructed bridge.
- `observability` — grouped `ObservabilityConfig` (journal/debug knobs); see
  the [Observability Fields](#observability-fields) section.
- `mcp_servers` — optional list of MCP server URIs (`stdio://`, `sse://`,
  `http://`, `https://`) passed through to agent bridges; frozen per
  session.
- `openai_api_key` — explicit OpenAI key for the default STT/TTS chain;
  falls back to the `OPENAI_API_KEY` environment variable.
- `stt` — speech-to-text selection: a shortcut string such as
  `"deepgram/flux"`, a provider config dataclass, or a live `STTProvider`
  instance. Unset → OpenAI realtime STT.
- `tts` — text-to-speech selection: shortcut string, config dataclass, or
  live `TTSProvider` instance. Unset → OpenAI TTS.
- `audio_processing` — grouped `AudioProcessingConfig` (VAD, noise
  reduction, echo cancellation, smart turn); see
  [Audio Processing Fields](#audio-processing-fields).
- `transport` — where audio comes from and goes to: local microphone
  (default), WebSocket, WebRTC, WebTransport, Twilio, or a live `Transport`
  instance.
- `turn_taking` — `TurnManagerConfig` controlling the turn FSM: VAD vs
  push-to-talk mode, silence timeouts, and pre-roll buffering.
- `timeouts` — `TimeoutConfig` with per-stage deadlines (STT, agent, TTS).
- `telephony` — optional `TelephonyConfig` enabling DTMF aggregation,
  voicemail detection, and outbound call management.
- `strip_markdown` — strip markdown formatting from agent output before
  synthesis.
- `auto_align_tts_output_to_transport` — resample/reformat TTS output to the
  transport's audio format automatically (default `True`).
- `output_processors` — ordered `LLMOutputProcessor` chain applied to agent
  text before TTS (pauses, pronunciation fixes, …).
- `session_actions` — optional `SessionActions` queue agents use to request
  side effects (end call, transfer, …).
- `action_executors` — extra `SessionActionExecutor` implementations for
  custom session actions.
- `session_policy` — grouped `SessionPolicyConfig` (greeting, opt-out, DNC,
  caller-ID exposure); see [Session Policy Fields](#session-policy-fields).
- `record_to` — directory path; when set, every session exports a
  timestamped debug bundle there on stop/shutdown ("always be recording").
  Requires `debug != "off"`.

## Audio Processing Fields

Group under `audio_processing=AudioProcessingConfig(...)`:

- `vad` — `VADConfig` or a live `VADProvider`; backend auto-resolves
  Silero → FunASR → TEN → Krisp unless forced via `VADConfig.backend`.
- `noise_reduction` — `NoiseReducerConfig` or live `NoiseReducer`; backend
  auto-resolves Krisp → RNNoise → passthrough.
- `echo_cancellation` — `EchoCancellationConfig` or live `EchoCanceller`;
  LiveKitAEC when enabled and available, else passthrough.
- `enable_noise_reduction` — opt into noise reduction with default settings
  (default `False`).
- `enable_echo_cancellation` — force AEC on/off; `None` (default) derives a
  transport-aware default (on for the local mic transport).
- `smart_turn` — `SmartTurnConfig` or bool enabling ONNX endpoint detection
  for faster turn handoff.
- `smart_turn_sensitivity` — beginner-facing 0–1 shortcut; higher values end
  turns on lower completion probabilities (implies `smart_turn=True`).

## Observability Fields

Group under `observability=ObservabilityConfig(...)`:

- `debug` — journal mode: `"off"` (no journal), `"light"`, or `"full"`
  (records audio artifacts too).
- `journal_backend` — `"sqlite"` (default), `"sqlite+litestream"`, or
  `"libsql"`.
- `journal_retention` — `"archive"` (default) keeps closed journals;
  `"delete"` removes them.
- `latency_budget` — one or more `LatencyBudget` thresholds; the session
  emits alerts when a turn exceeds them.
- `warmup` — run provider warmup hooks at session start (default `True`).
- `max_session_cost_usd` — hard cost ceiling; the session stops when
  estimated provider spend crosses it.

## Session Policy Fields

Group under `session_policy=SessionPolicyConfig(...)`:

- `greeting` — text spoken once when the call is answered.
- `dnc_list` — do-not-call list the opt-out policy appends to.
- `opt_out_detection` — auto-detect TCPA opt-out phrases in final
  transcripts (default `True`).
- `opt_out_phrases` — override the built-in opt-out phrase list.
- `caller_id_exposure` — how the callee identity reaches the agent:
  `"off"`, `"system_message"`, or `"tools_only"` (default).

## Top-Level Aliases

Every grouped field above is also accepted as a top-level keyword for
convenience — `EasyConfig(debug="full")` is equivalent to
`EasyConfig(observability=ObservabilityConfig(debug="full"))`, and the same
holds for the audio-processing and session-policy names. The aliases are
`InitVar`s: they forward into the grouped object at construction time and
read/write through to it afterwards. Prefer the grouped form in new code;
the aliases exist so the common one-knob cases stay one-liners.

## Related Pages

- [Architecture](../architecture.md) — how the configured pieces fit
  together at runtime.
- [Session lifecycle](session-lifecycle.md) — start/stop semantics for the
  session `create_session` returns.
- [Events reference](events.md) — what the wired session emits.
- [Public API contract](../public-api.md) — the import surface these names
  come from.
