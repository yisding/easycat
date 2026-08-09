# Events Reference

This page lists every public EasyCat event type with its when-emitted
semantics. The catalog below is kept honest by
`tests/docs/test_route_contracts.py::test_events_reference_tracks_public_event_types`,
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

`Session.subscribe_event(EventType, handler)` links the event class to the
handler parameter for type checkers. For example, an `STTFinal` subscription
infers `event` as `STTFinal` in a lambda and rejects a callback annotated for an
unrelated event type. Keep the returned subscription and call its idempotent
`unsubscribe()` method when the listener's owner shuts down.

## Event Catalog

### Audio

- `AudioIn` — raw audio chunk received from the transport, before noise
  reduction and echo cancellation.
- `AudioOut` — an outbound audio chunk passed EasyCat's last retractable
  transport buffer; what the listener will actually hear. Buffered
  transports may defer emission until the chunk crossed their own clearable
  queue.
- `TransportAudioDelivered` — a transport-level delivery acknowledgement
  emitted when a chunk crosses a clearable outbound buffer. The session uses
  it to establish ownership before publishing `AudioOut`; most applications
  should subscribe to `AudioOut`.

### Voice Activity Detection

- `VADStartSpeaking` — the VAD detected the start of user speech.
- `VADStopSpeaking` — the VAD detected the end of user speech.

### Speech-to-Text

- `STTPartial` — a partial (still-changing) transcript from the STT
  provider.
- `STTFinal` — the final transcript for a completed utterance; this is what
  commits a turn to the agent.

### Agent

- `AgentRequestStarted` — the runtime confirmed and took the turn for agent
  output. This is the confirmation/take timestamp, not always the start of
  model work: with preemptive generation, the model request may already be in
  flight.
- `AgentDelta` — a streaming text update from the agent while it generates a
  reply. Ordinary events append `text`. Indexed bridge streams set
  `part_index`; when `replacement` is true, consumers replace that complete
  part instead of appending it.
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

### Playback And Interruptions

- `Interruption` — the user barged in while the bot was speaking; pending
  TTS for the turn is cancelled.
- `PlaybackMarkAck` — a transport acknowledged that playback reached a
  previously queued mark (for example a Twilio Media Stream mark).

### Agent Tools

- `ToolCallStarted` — an agent tool invocation began; carries its tool name
  and call id.
- `ToolCallDelta` — a streaming argument/output delta arrived for an
  in-progress tool call.
- `ToolCallResult` — the tool invocation completed and produced its recorded
  result.

### Provider Reconnection

- `ReconnectAttempt` — a reconnecting provider is starting the numbered
  retry attempt.
- `ReconnectSuccess` — the provider re-established its connection.
- `ReconnectFailure` — the reconnect sequence exhausted or failed; carries
  the provider's error text.

### Transport Diagnostics

- `TransportDegraded` — a transport dropped data or tore down abnormally;
  carries a transport-owned reason code, bounded detail, and a `fatal` flag.

### Telephony Lifecycle

- `DTMF` — one DTMF digit was detected.
- `DTMFAggregated` — the DTMF aggregator completed a multi-digit sequence.
- `VoicemailDetected` — voicemail/answering-machine detection resolved to
  `human`, `machine`, or `unknown`.
- `CallInitiated` — EasyCat placed an outbound call; carries the call id and
  destination/source numbers.
- `CallRinging` — the outbound call entered the remote-ringing state.
- `CallAnswered` — an outbound call was answered (by a human, machine, or
  screener); triggers the configured greeting.
- `CallScreening` — a platform or carrier call screener was detected.
- `ScreeningResponse` — the screening detector requested the configured
  static or agent-generated response.
- `ScreeningTimedOut` — screening exhausted its maximum turns without
  resolving.
- `IVRAction` — the IVR navigator chose a DTMF, speak, wait, hangup, hold, or
  human-detected action.
- `CallStateChanged` — the outbound call controller moved between two call
  states.
- `CallEnded` — the call terminated, with duration and disposition when
  known.
- `CallFailed` — the call failed (busy, no answer, rejected, or error).

### Supervisor Audio Taps

- `SupervisorListenerAttached` — a passive supervisor listener subscribed to
  session audio.
- `SupervisorListenerDetached` — a passive supervisor listener detached from
  session audio.

### Session Actions

- `SessionActionRequested` — an agent-requested session action left the
  queue and is about to be matched to an executor.
- `SessionActionStarted` — a matching session-action executor began running.
- `SessionActionCompleted` — the executor completed successfully and
  returned a `SessionActionResult`.
- `SessionActionFailed` — the action had no supporting executor or its
  executor raised.

### Errors

- `Error` — wraps an exception from a pipeline stage; carries the
  `ErrorStage` (stt/agent/tts/pipeline), the provider name when known, and a
  stable `EASYCAT_Exxx` code when available so journal records correlate
  with `easycat explain`.

## Event Groups And Provider Events

Every event in the catalog is available from both `easycat` and
`easycat.events`. Bulk-subscription groups such as `STT_EVENTS`,
`LIFECYCLE_EVENTS`, `TELEPHONY_EVENTS`, `TRANSPORT_EVENTS`, and `ALL_EVENTS`
remain in `easycat.events`, along with provider-scoped `STTEvent` and
`TTSEvent` types used by provider implementations.

## Related Pages

- [Architecture](../architecture.md) — where each event is emitted in the
  pipeline.
- [Session lifecycle](session-lifecycle.md) — how events relate to start,
  stop, and postmortem journal reads.
- [Observability](../observability.md) — the journal, bundles, and the
  debugger UI.
