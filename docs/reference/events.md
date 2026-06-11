# Events Reference

This page lists every public EasyCat event type with its when-emitted
semantics. The catalog below is kept honest by
`tests/test_docs_index.py::test_events_reference_tracks_public_event_types`,
which compares it against the event classes exported from `easycat`.

Run `uv run easycat explain events` for a terminal summary of this page, and
`uv run easycat docs --audience app-builders` for the surrounding route map.

The `EventBus` drives application behavior — it is NOT an observability sink.
For durable, replayable records use `session.journal` or
`export_debug_bundle()`; see [observability](../observability.md) for the
four-layer model.

## Two Event Layers

EasyCat has two event layers, and only one of them is meant for your code:

- **EasyCat-level events** (this catalog) are emitted on the session
  `EventBus`. Subscribe with `session.subscribe_event(STTFinal, handler)` or
  `bus.subscribe(...)`. Every event carries optional `session_id` /
  `turn_id` correlation fields (injected by `Session`) and a monotonic
  `timestamp`.
- **Provider-scoped events** (`STTEvent`, `TTSEvent`) are produced by STT/TTS
  provider async iterators and are internal to provider implementations.
  `Session` maps them to EasyCat-level events: an `STTEvent` with type
  `FINAL` becomes `STTFinal`, a `TTSEvent` with type `AUDIO` becomes
  `TTSAudio`, and so on. Application code should not consume provider-scoped
  events directly.

Handlers may be sync or async; `EventBus.emit()` dispatches inline in
subscription order and also invokes handlers registered for parent classes,
so subscribing to `Event` receives everything (`subscribe_all` does the
same).

## Event Catalog

### Audio

- `AudioIn` — raw audio chunk received from the transport, before noise
  reduction and echo cancellation.
- `AudioOut` — an outbound audio chunk passed EasyCat's last retractable
  transport buffer; what the listener will actually hear. Buffered
  transports may defer emission until the chunk crossed their own clearable
  queue.

### Voice Activity Detection

- `VADStartSpeaking` — the VAD detected the start of user speech.
- `VADStopSpeaking` — the VAD detected the end of user speech.

### Speech-to-Text

- `STTPartial` — a partial (still-changing) transcript from the STT
  provider.
- `STTFinal` — the final transcript for a completed utterance; this is what
  commits a turn to the agent.

### Agent

- `AgentDelta` — a streaming text delta from the agent while it generates a
  reply.
- `AgentFinal` — the agent's final complete response for the turn; carries
  `structured_output` when the agent uses a typed `output_type`.

### Text-to-Speech

- `TTSAudio` — an audio chunk produced by the TTS provider, before transport
  playback.
- `TTSMarkers` — best-effort, provider-native alignment markers from TTS
  (word- or char-level depending on the provider; recorded opaquely).

### Turn Lifecycle

- `TurnStarted` — a new user turn began (VAD triggered or push-to-talk
  opened).
- `TurnEnded` — the user turn ended; speech capture for the turn is
  complete.
- `BotStartedSpeaking` — the bot began playing TTS audio.
- `BotStoppedSpeaking` — the bot finished playing TTS audio.
- `Interruption` — the user barged in while the bot was speaking; pending
  TTS for the turn is cancelled.

### Telephony Lifecycle

- `CallAnswered` — an outbound call was answered (by a human, machine, or
  screener); triggers the configured greeting.
- `CallEnded` — the call terminated, with duration and disposition when
  known.
- `CallFailed` — the call failed (busy, no answer, rejected, or error).

### Supervisor Audio Taps

- `SupervisorListenerAttached` — a passive supervisor listener subscribed to
  session audio.
- `SupervisorListenerDetached` — a passive supervisor listener detached from
  session audio.

### Errors

- `Error` — wraps an exception from a pipeline stage; carries the
  `ErrorStage` (stt/agent/tts/pipeline), the provider name when known, and a
  stable `EASYCAT_Exxx` code when available so journal records correlate
  with `easycat explain`.

## Beyond the Top-Level Exports

The full telephony vocabulary (DTMF aggregation, voicemail detection, call
screening, IVR actions, opt-out detection), reconnect events, transport
diagnostics, and session-action events live in `easycat.events` alongside
bulk-subscription groups such as `STT_EVENTS`, `LIFECYCLE_EVENTS`,
`TELEPHONY_EVENTS`, and `ALL_EVENTS`. Import them from `easycat.events`
directly when you need them; the top-level package exports only the catalog
above.

## Related Pages

- [Architecture](../architecture.md) — where each event is emitted in the
  pipeline.
- [Session lifecycle](session-lifecycle.md) — how events relate to start,
  stop, and postmortem journal reads.
- [Observability](../observability.md) — the journal, bundles, and the
  debugger UI.
