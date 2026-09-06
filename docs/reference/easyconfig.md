# EasyConfig Field Reference

Configuration, provider-selection, and credential failures share the public
`EasyCatError` boundary. Catch that base when one recovery path should handle
all construction failures; catch `EasyConfigError` when invalid field values
need separate treatment. `EasyConfigError` remains a `ValueError` for existing
callers and carries code `EASYCAT_E105`.

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

`create_session(config)` treats the mutable dataclass as a specification: it
copies build-normalized fields and per-session collections before allocating
runtime collaborators. Reusing one descriptor-based config therefore does not
share turn-manager state or rewrite the caller's object. A caller-supplied live
provider, transport, or agent instance is intentionally not cloned; inject a
factory/config descriptor when each session needs a separately owned client.

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
- `journal_capacity` — maximum records retained by the bounded in-memory
  `"light"` journal (default `10_000`). Evictions are counted on
  `session.journal.dropped_records` and carried into exported bundle metadata.
  Persistent `"full"` backends ignore this value.
- `journal_redaction` — `"secrets"` (default) preserves replay-relevant
  transcripts and other customer content while scrubbing credentials;
  `"pii"` also redacts phone numbers, URLs, request IDs, home paths, prompts,
  transcripts, and provider text before the journal is written.
- `slow_handler_threshold_s` — elapsed time before an inline event handler
  produces a warning. Defaults to `0.005` seconds; set `None` to disable.
- `handler_error_policy` — `"continue"` (default) logs and counts event-handler
  exceptions before dispatching later handlers; `"raise"` propagates the first
  exception to the emitter.
- `journal_retention` — `"archive"` (default) keeps closed journals;
  `"delete"` removes them.
- `data_dir` — optional storage root. With `debug="full"` it contains the
  session's persistent journal and artifacts; `debug="light"` keeps those
  resources in memory. Emergency exports and other explicit bundle writes use
  this root in either mode. Unset falls back to `EASYCAT_DATA_DIR` or
  `.easycat`.
- `warmup` — run provider warmup hooks at session start (default `True`).
- `debugger_autolaunch` — opt in to auto-opening the local debugger UI in an
  interactive terminal (default `False`). The
  `EASYCAT_DEBUGGER_AUTOLAUNCH` env var also enables it.
- `capture_audio` — persist live STT, TTS, VAD, transport, and AEC audio
  artifacts when `True` (default). Pass `False` to keep journal events and
  transcripts without audio blobs, or a zero-argument predicate for a dynamic
  consent policy. `session.set_audio_capture_enabled(False)` can pause capture;
  pass `True` to resume subject to the predicate, or `None` to clear the runtime
  override. Pre-consent buffered audio is never persisted after capture starts.
- `capture_aec_reference` — opt in to journaling the echo canceller's far-end
  reference frames (default `False`). The `EASYCAT_CAPTURE_AEC_REFERENCE`
  env var also enables it.
- `emergency_export` — opt in to a best-effort debug-bundle export on abnormal
  process exit (default `False`). The `EASYCAT_EMERGENCY_EXPORT` env var also
  enables it.
- `openai_api_key` — explicit OpenAI key for the default STT/TTS chain;
  falls back to the `OPENAI_API_KEY` environment variable.
- `stt` — speech-to-text selection: a shortcut string such as
  `"deepgram/flux"`, a provider config dataclass, a named
  `STTProviderConfig`, or a live `STTProvider` instance. Unset → OpenAI
  realtime STT.
- `tts` — text-to-speech selection: shortcut string, config dataclass, named
  `TTSProviderConfig`, or live `TTSProvider` instance. Unset → OpenAI TTS.
- `vad` — a registered shortcut string, `VADConfig`, or a live
  `VADProvider`; backend auto-resolves Silero → FunASR → TEN → Krisp unless
  forced via `VADConfig.backend`.
- `noise_reduction` — a registered shortcut string, `NoiseReducerConfig`, or a
  live `NoiseReducer`; backend auto-resolves Krisp → RNNoise → passthrough.
- `echo_cancellation` — a registered shortcut string,
  `EchoCancellationConfig`, or a live `EchoCanceller`; LiveKitAEC when enabled
  and available, else passthrough.
- `enable_noise_reduction` — opt into noise reduction with default settings
  (default `False`).
- `enable_echo_cancellation` — force AEC on/off; `None` derives a
  transport-aware default. Local transport and the `EasyConfig.browser()`
  WebRTC preset can provide a playback-clocked far-end reference and enable
  server-side AEC automatically. WebSocket and WebTransport leave it off by
  default because the server cannot observe browser playout timing; use the browser's
  `getUserMedia({audio: {echoCancellation: true}})` constraint. Explicit
  server-side opt-in remains available and records
  `transport_degraded.reason="aec_reference_degraded"` in the session journal.
- `smart_turn` — `SmartTurnConfig` or bool enabling semantic endpoint
  detection. When unset, it defaults on for local-microphone transports and
  off for server, browser, and telephony transports.
  When the STT provider declares `native_endpointing` and nothing overrides it
  (smart turn, push-to-talk, or the voicemail detector), turns come from STT
  FINAL events and no VAD stage is built. `easycat plan` and `/health/ready`
  report that role as `off`, so its install extra is not a blocking gap.
- `smart_turn_sensitivity` — beginner-facing 0–1 shortcut; higher values end
  turns on lower completion probabilities. When `smart_turn` is unset, supplying
  sensitivity enables smart turn; combining it with explicit
  `smart_turn=False` raises `EasyConfigError`.
- `transport` — where audio comes from and goes to: local microphone
  (default), WebSocket, WebRTC, WebTransport, Twilio, or a live `Transport`
  instance.
- `turn_taking` — `TurnManagerConfig` controlling the turn FSM: VAD vs
  push-to-talk mode, silence timeouts, and pre-roll buffering.
- `timeouts` — `TimeoutConfig` with per-stage deadlines (STT, agent, TTS).
- `telephony` — optional `TelephonyConfig` enabling DTMF aggregation,
  voicemail detection, and outbound call management. Twilio and Telnyx are
  both first-class: `EasyConfig.phone(provider="telnyx", ...)` builds a
  Telnyx Call Control transport (L16 @ 16 kHz by default), and
  `TelephonyConfig.telnyx_actions` configures the session action executor for
  native transfer/send-DTMF/hangup commands and SMS. Outbound calls select the provider via
  `OutboundCallConfig(provider=...)` (`"twilio"` default; `"telnyx"` uses
  `telnyx_connection_id` / `telnyx_webhook_url`).
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
- `on_agent_failure` — optional fallback text or
  `Callable[[Exception], str]`. When an agent error or timeout delivers no
  response audio, EasyCat speaks the resolved text through the normal
  cancellable TTS path. The default `None` preserves silent failure behavior.
- `session_id` — optional caller-supplied runtime session id; unset generates
  a `session-...` id.
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
