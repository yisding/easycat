# EasyConfig Field Reference

This page is the handwritten reference for every `EasyConfig` field. It is kept honest
by `tests/docs/test_route_contracts.py::test_easyconfig_reference_tracks_config_fields`,
which compares each section against the live dataclass fields — the same
pattern that keeps [public-api.md](../public-api.md) in sync with
`easycat.__all__`.

`EasyConfig` is keyword-only. The zero-config path is
`EasyConfig(agent=my_agent)` with `OPENAI_API_KEY` set; everything else below
is opt-in. Build the session with `create_session(config)`.
Run `uv run easycat docs --audience app-builders` for the surrounding route
map, and see the [session lifecycle reference](session-lifecycle.md) for what
happens after construction.

For the app-first path, `VoiceApp` wraps this surface: it resolves the matching
`EasyConfig` preset (`EasyConfig.mic()` / `.browser()` / `.phone()`) per
deployment mode — the `websocket` mode has no dedicated preset, so it builds a
bare `EasyConfig` bound to the per-connection transport — then builds and runs
the session through `create_session`. The
high-level `VoiceApp(agent=..., stt=..., tts=...)` fields below are forwarded
into the chosen preset. See the [public API contract](../public-api.md) for the
`from easycat import VoiceApp` entry point.

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
  and cancellation wrapper applied to plain `async run` agents. Preemptive
  generation is opt-in; enable it only for replayable, side-effect-free
  agents because an unconfirmed transcript may be cancelled and retried.
- `wrap_agent` — when `True` (default), plain agents are wrapped in
  `AgentRunner`; set `False` only when passing a fully-constructed bridge.
- `mcp_servers` — optional list of MCP server URIs (`stdio://`, `sse://`,
  `http://`, `https://`) passed through to agent bridges; frozen per
  session.
- `debug` — journal mode: `"off"` (no journal), `"light"` (default), or
  `"full"`. The default keeps the journal and audio artifacts in memory so
  per-frame capture does not touch disk on the live audio loop. Use `"full"`
  for a crash-survivable on-disk journal and artifacts, or `"off"` to disable
  recording.
- `journal_backend` — `"sqlite"` (default), `"sqlite+litestream"`, or
  `"libsql"`. The in-process `"sqlite+litestream"` backend starts one
  `litestream replicate` subprocess per session; for multi-call production
  servers, prefer `"sqlite"` plus the sidecar pattern in
  [docker.md](../deployment/docker.md#litestream-and-libsql-replicas-in-a-container).
- `journal_retention` — `"archive"` (default) keeps closed journals;
  `"delete"` removes them.
- `warmup` — run provider warmup hooks at session start (default `True`).
- `debugger_autolaunch` — opt in to auto-opening the local debugger UI in an
  interactive terminal (default `False`). The
  `EASYCAT_DEBUGGER_AUTOLAUNCH` env var also enables it.
- `capture_aec_reference` — opt in to journaling the echo canceller's far-end
  reference frames (default `False`). The `EASYCAT_CAPTURE_AEC_REFERENCE`
  env var also enables it.
- `emergency_export` — opt in to a best-effort debug-bundle export on abnormal
  process exit (default `False`). The `EASYCAT_EMERGENCY_EXPORT` env var also
  enables it.
- `openai_api_key` — explicit OpenAI key for the default STT/TTS chain;
  falls back to the `OPENAI_API_KEY` environment variable.
- `stt` — speech-to-text selection: a shortcut string such as
  `"deepgram/flux"`, a provider config dataclass, or a live `STTProvider`
  instance. Unset → OpenAI realtime STT.
- `tts` — text-to-speech selection: shortcut string, config dataclass, or
  live `TTSProvider` instance. Unset → OpenAI TTS.
- `vad` — `VADConfig` or a live `VADProvider`; backend auto-resolves
  Silero → FunASR → TEN → Krisp unless forced via `VADConfig.backend`.
- `noise_reduction` — `NoiseReducerConfig` or live `NoiseReducer`; backend
  auto-resolves Krisp → RNNoise → passthrough.
- `echo_cancellation` — `EchoCancellationConfig` or live `EchoCanceller`;
  LiveKitAEC when enabled and available, else passthrough.
- `enable_noise_reduction` — opt into noise reduction with default settings
  (default `False`).
- `enable_echo_cancellation` — force AEC on/off; `None` derives a
  transport-aware default.
- `smart_turn` — `SmartTurnConfig` or bool enabling semantic endpoint
  detection. When unset, it defaults on for local-microphone transports and
  off for server, browser, and telephony transports.
- `smart_turn_sensitivity` — beginner-facing 0–1 shortcut; higher values end
  turns on lower completion probabilities (implies `smart_turn=True`).
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
- `greeting` — text spoken once when the call is answered.
- `dnc_list` — do-not-call store checked before placing outbound calls.
- `caller_id_exposure` — how the callee identity reaches the agent:
  `"off"`, `"system_message"`, or `"tools_only"` (default).
- `record_to` — directory path; when set, every session exports a
  timestamped debug bundle there on stop/shutdown ("always be recording").
  Requires `debug != "off"`.

## Related Pages

- [Architecture](../architecture.md) — how the configured pieces fit
  together at runtime.
- [Session lifecycle](session-lifecycle.md) — start/stop semantics for the
  session `create_session` returns.
- [Events reference](events.md) — what the wired session emits.
- [Public API contract](../public-api.md) — the import surface these names
  come from.
