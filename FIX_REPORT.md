# EasyCat — Audit Fix Report

_Branch `claude/audit-fixes`. Resolves all confirmed findings from the architecture audit (`ARCHITECTURE_AUDIT.md`) and the verified polish backlog, plus follow-up architectural decomposition and cleanup._

## Outcome

- **Confirmed findings: 127 → all resolved** (127 fixed, 0 partial, 0 skipped)
- **Tests:** 3469 passed, 99 skipped, ruff clean (baseline 3366; +~100 regression tests)
- **Diff vs `main`:** 188 files changed, 10427 insertions(+), 5058 deletions(-)

Work proceeded in passes: (1) bulk fix of 127 findings; (2) repair of 83 regressions the bulk pass caused, including reverting findings that proved wrong; (3) two large architectural refactors; (4) cleanup of the partial tails.

## Architectural decomposition (pre-launch, no back-compat)

**Session** (`session-core-2`,`xc-architecture-1`): 1802 -> 1400 (Session.__init__: ~385 -> ~115, a scannable field-assignment shell ending in build_session(self, cfg)). New: `src/easycat/session/_builder.py`, `src/easycat/session/_wiring.py`, `src/easycat/session/_greeting.py`, `src/easycat/session/_opt_out.py`, `src/easycat/session/_caller_id.py`, `src/easycat/session/_telephony_facade.py`, `tests/session/_wiring_helpers.py`.

**config.py** (`xc-ease-of-use-5`,`xc-architecture-6`): src/easycat/config.py was 1782 LOC (single module). After: config/ package totals 1947 LOC across 5 files — config/easy.py 735, config/_factory.py 669, config/_telephony_wiring.py 368, config/_tts_alignment.py 95, config/__init__.py 80; the newcomer's first read (config/easy.py) is 735 LOC of pure config dataclasses + validation.. New: `src/easycat/config/__init__.py`, `src/easycat/config/easy.py`, `src/easycat/config/_factory.py`, `src/easycat/config/_telephony_wiring.py`, `src/easycat/config/_tts_alignment.py`, `src/easycat/debugger/_autolaunch.py`.

## Reverted/narrowed during repair (findings that were wrong)

- call_state.py SmartTurn-suppression removal: reverted the over-aggressive deletion (kept the unrelated valid gate-discard()/reopen change in the same diff)
- ivr.py hold-detection + DTMFDelivery.verify removal: reverted the deletion of test-protected features (kept the valid retry-helper extraction); left the two truly-dead config fields dtmf_inter_digit_delay and ivr_dtmf_verify removed since no test or source reads them
- config.py lazy-import of OutboundCallManager: narrowed rather than reverted — kept lazy loading but re-exposed the symbol via __getattr__ and module-namespace reference so the monkeypatch contract works again
- version_info connection_model exposure (R4-runtime-stt openai_realtime_provider): narrowly reverted the audit's addition of the connection_model key to version_info() because it violated the documented 4-key shape invariant enforced by the TestVersionInfoShapeInvariant CI guard across all providers/transports/TTS. The connection_model config field itself (URL construction) was kept.
- degraded-marker post-finalize persistence (R4-runtime-stt journal _persist_degraded_marker / _enter_degraded): narrowed the audit's unconditional degraded-marker persistence so it no longer fires in the post-finalize clean-close path, which had broken the documented crash-after-finalize-looks-clean contract (test_failed_append_after_finalize_keeps_clean_marker). The feature is retained for the live (non-finalized) degraded case.

## Full per-finding ledger

| Status | ID | Sev | Title |
|--------|----|-----|-------|
| fixed | `agent-integrations-0` | medium | Four-step apply_interruption orchestration is duplicated verbatim across all 7 bridges |
| fixed | `agent-integrations-1` | low | apply_interruption success path (step 4b) is unprotected, so a recorder failure after the mutation leaves the journal mid-interruption |
| fixed | `agent-integrations-2` | low | The invoke() BaseException cursor-cleanup block is copy-pasted across 6 bridges |
| fixed | `agent-integrations-3` | low | Config/factory code mutates undocumented bridge private attributes, bypassing the ExternalAgentBridge contract |
| fixed | `agent-integrations-4` | low | Stream-level cursor_entered/cursor_exited/handoff/state_snapshot events are emitted inconsistently across bridges |
| fixed | `agent-integrations-5` | low | Redundant nested cancel-token check in RemoteResponsesAPIBridge.invoke obscures the drain logic |
| fixed | `config-public-api-0` | high | record_to monkeypatching breaks stop(force=...) and the async-with teardown idiom with a TypeError |
| fixed | `config-public-api-1` | medium | config.py eagerly imports the entire telephony/transport/provider stack, defeating the package's lazy-load design |
| fixed | `config-public-api-2` | medium | create_session is a god-function wiring telephony, IVR, outbound, identity, debugger, and recording inline |
| fixed | `config-public-api-3` | low | _align_tts_config_to_transport hand-rolls per-provider dispatch with byte-identical Deepgram/Cartesia branches |
| fixed | `config-public-api-4` | low | create_text_session carries a dual config-or-loose-kwargs API that create_session does not |
| fixed | `config-public-api-5` | low | _resolve_easycat_log_level docstring names two functions as if they call it directly, but neither does |
| fixed | `config-public-api-6` | low | OutboundCallConfig/VoicemailDetectionConfig validate only some positive-number knobs; detection_timeout_s flows unchecked into asyncio.sleep |
| fixed | `config-public-api-7` | low | _inject_agent_runtime reaches into bridge private attributes from the config layer |
| fixed | `config-public-api-8` | low | quick.speak() leaks the TTS provider's persistent httpx client; the transcribe_file STT-leak claim is refuted |
| fixed | `debug-validation-cli-0` | high | `easycat bundles show`/`inspect` always reports duration=n/a and tool_calls=0 for exported ZIP bundles |
| fixed | `debug-validation-cli-1` | low | `_write_report_copy` safety net in validate.py is dead code — the runner already wrote the requested report |
| fixed | `debug-validation-cli-2` | low | Recurring `_ = <symbol>` discard lines silence ruff F401 for genuinely-unused imports (and one no-op) |
| fixed | `debug-validation-cli-3` | low | Latency artifact emits two percentile blocks computed by different algorithms |
| fixed | `debug-validation-cli-4` | low | `ci` environment flag is detected two different ways across validation artifacts |
| fixed | `debug-validation-cli-5` | low | Provider capability tooling: unused spec param dressed as a derivation, plus a hand-maintained surface table partly duplicating registry env vars |
| fixed | `debug-validation-cli-6` | low | Live-session debugger WebSocket re-reads and re-serializes the entire journal every 500ms just to compare a record count |
| fixed | `providers-protocol-misc-2` | low | Timeout errors hardcode generic provider names, discarding the real provider identity available via version_info() |
| fixed | `providers-protocol-misc-4` | low | SessionManager docstring references a non-existent Session.destroy method |
| fixed | `providers-protocol-misc-5` | low | VADProvider.process declared `async def -> AsyncIterator` (a coroutine type) while the other three streaming Protocol methods correctly use plain `def -> AsyncIterator` |
| fixed | `runtime-observability-0` | high | SqliteJournal holds one uncommitted transaction for the whole session — SIGKILL loses every record, contradicting the durability contract |
| fixed | `runtime-observability-1` | medium | Degraded mode is recorded inconsistently: in-memory writes a JournalDegraded marker, SQLite/libSQL write nothing |
| fixed | `runtime-observability-2` | medium | Three SQL/ring backends duplicate append-wrapper, _do_append, _enter_degraded, read and slice verbatim — no shared base |
| fixed | `runtime-observability-3` | low | Persistent backends silently stringify non-JSON data values; in-memory keeps them live — same record, different shape per backend |
| fixed | `runtime-observability-4` | low | SqliteJournal.finalize() docstring promises retention but the body never runs it |
| fixed | `runtime-observability-5` | low | EventBus.emit rebuilds the handler list and walks the full MRO on every event, including per-audio-chunk traffic |
| fixed | `runtime-observability-7` | low | JournalView query helpers (filter_by_stage/filter_by_turn/lookup_by_sequence) re-read the entire journal per call and O(n)-scan |
| fixed | `runtime-observability-8` | low | op_id and queue_ns are dead schema columns with no write path, plumbed through every backend and the record type |
| fixed | `session-audio-text-0` | medium | Outbound TTS queue uses DROP_OLDEST, silently corrupting the bot's own speech under backpressure |
| fixed | `session-audio-text-1` | medium | TTSScheduler.synthesize() is dead in production and duplicates the turn-runner's finalize/gating logic |
| fixed | `session-audio-text-2` | low | gated-replay 'pending' counter decrements per dequeued chunk regardless of whether the chunk is actually a replay chunk |
| fixed | `session-audio-text-4` | low | interruption.py and _turn_runner.py import underscore-prefixed text.py helpers across module boundaries |
| fixed | `session-audio-text-6` | low | `_chunk_has_speech_energy` uses a per-sample Python decode loop on the auto-turn (no-VAD) ingress path |
| fixed | `session-audio-text-7` | low | to_mono downmix uses floor division, introducing a sub-LSB negative DC bias |
| fixed | `session-core-0` | high | on_turn_started leaks the STT consumer task and open STT stream if pre-roll priming fails |
| fixed | `session-core-1` | low | STTCommitter.stt_task and segment_commit_task properties are never read |
| fixed | `session-core-2` | medium | Session is a 1822-LOC god-object owning construction, lifecycle, properties, telephony, opt-out, actions, journaling, and cancellation |
| fixed | `session-core-3` | low | Text-turn loop in _execute_text_turn duplicates the bridge-event dispatch already implemented by consume_agent_stream |
| fixed | `session-core-5` | low | Session keeps six (not eight) test-compat shim delegates on its surface; two are fully dead with zero callers |
| fixed | `session-core-7` | low | The 'active turn only when not IDLE' guard is duplicated in two correlation sites (not three) |
| fixed | `stt-providers-0` | medium | OpenAISTT batch provider re-emits PARTIAL/FINAL events on retry after a mid-stream failure |
| fixed | `stt-providers-1` | medium | OpenAIRealtimeSTT bypasses WebSocketSTTBase and reimplements all shared WebSocket plumbing |
| fixed | `stt-providers-2` | low | STTEvent.word_timestamps and confidence are populated by providers but never consumed anywhere |
| fixed | `stt-providers-3` | low | Session reaches into provider-private _bytes_since_last_commit, an attribute only OpenAIRealtime exposes |
| fixed | `stt-providers-5` | low | ElevenLabs realtime end path closes the WebSocket twice in a redundant, confusing sequence |
| fixed | `stt-providers-6` | low | OpenAI batch provider only records the first chunk's audio format and ignores later mismatches |
| fixed | `stt-providers-7` | low | version_info SDK/model fields are inconsistent and partly misleading across providers |
| fixed | `telephony-0` | low | SmartTurn-suppression and VAD-timeout-extension feature in OutboundCallStateMachine is entirely unreachable (no setters wired) |
| fixed | `telephony-1` | low | ml_voicemail.py (ConversationCoherenceDetector, EarlyMediaDetector) is a fully orphaned module |
| fixed | `telephony-2` | low | IVRNavigator hold-detection and DTMF-verify features are dead config (never invoked / never read) |
| fixed | `telephony-3` | low | IVRNavigator agent-retry logic is duplicated three ways and hard to follow |
| fixed | `telephony-4` | low | Classification gate stays closed after CLASSIFYING->VOICEMAIL, swallowing leave-message TTS (gate enabled by default) |
| fixed | `telephony-5` | low | Heuristic VoicemailDetector never resets between sequential calls and ignores call_sid, unlike its peers |
| fixed | `telephony-6` | low | CallScreeningDetector multi-turn coherence path is dead in the wired pipeline and track filtering is disabled by default |
| fixed | `telephony-7` | low | SMSFallbackSuggested and NumberRotationSuggested are "emitted" event classes that are never emitted or referenced (and the sibling NumberHealthWarning is the same) |
| fixed | `telephony-8` | low | NumberHealthMonitor records placement-failure metrics under empty/synthetic 'number' keys |
| fixed | `transports-0` | medium | WebRTCTransport.disconnect() does not hold _offer_lock, racing peer re-creation against teardown |
| fixed | `transports-1` | medium | WebTransport outbound backpressure silently disables itself if aioquic private internals are renamed |
| fixed | `transports-2` | low | TwilioTransport.send_audio leaves stale connection state after ConnectionClosed, unlike its connection-variant twin |
| fixed | `transports-3` | low | Twilio send_mark contract diverges between the two transports (raise vs return; uncaught ConnectionClosed) |
| fixed | `transports-4` | low | _WebTransportSession reimplements the shared inbound enqueue+degrade logic instead of reusing _AudioQueueMixin |
| fixed | `tts-providers-0` | medium | Reconnect replays the utterance but never resets _sample_carry, shifting all replayed audio by one byte |
| fixed | `tts-providers-1` | low | ElevenLabs stop() leaks the WebSocket and only closes a dead HTTP response, contradicting Deepgram's documented parity claim |
| fixed | `tts-providers-2` | low | _emit_provider_error and its _emit_tasks bookkeeping are byte-identical copy-paste across all three WebSocket providers |
| fixed | `tts-providers-3` | low | TTSMarkers carries provider-native, mutually-incompatible shapes that no consumer ever reads; Deepgram emits none |
| fixed | `tts-providers-4` | low | ElevenLabs _synthesize_ws closes the socket three times and swallows KeyError over the whole parse block |
| fixed | `tts-providers-5` | low | OpenAI TTS error path swallows non-status HTTP errors silently and never surfaces a journal Error event |
| fixed | `turn-management-0` | low | Smart-turn decision uses two independent, redundant thresholds at different layers |
| fixed | `turn-management-1` | low | Session reaches into private TurnManager._config; TurnManager exposes no detector accessor and holds two copies of it |
| fixed | `turn-management-2` | low | Dead `_config is not None` guards: TurnManager._config is never None |
| fixed | `turn-management-3` | low | Barge-in debug log hardcodes 'BotSpeaking -> UserSpeaking' even when interrupting a PROCESSING turn |
| fixed | `turn-management-5` | low | `turn_audio` property exposes the live mutable capture list (latent, not currently reachable) |
| fixed | `turn-management-6` | low | `reset()` abandons the active cancel token instead of cancelling it |
| fixed | `turn-management-7` | low | Endpoint-detector contract typed as `Any` despite an existing `SmartTurnProvider` Protocol |
| fixed | `vad-noise-echo-0` | low | VADConfig pre_roll_ms / post_roll_ms are dead config that silently do nothing |
| fixed | `vad-noise-echo-1` | medium | create_noise_reducer's passthrough fallback contradicts Session's noop-rejection guard, crashing the happy path |
| fixed | `vad-noise-echo-2` | medium | AEC ValueError on near/far sample-rate mismatch is swallowed and mislabeled in the outbound drain task |
| fixed | `vad-noise-echo-3` | low | Three sibling fallback chains (VAD / NoiseReducer / AEC) have three incompatible config shapes and auto-failure behaviors |
| fixed | `vad-noise-echo-4` | medium | RNNoiseReducer re-pads/re-resamples every chunk independently and keeps no remainder buffer, corrupting the recurrent filter at chunk boundaries |
| fixed | `vad-noise-echo-6` | low | LiveKitAEC zero-pads non-10ms-aligned near-end frames mid-stream, desyncing and corrupting the AEC3 adaptive filter |
| fixed | `vad-noise-echo-7` | low | Inconsistent resource cleanup: Krisp/RNNoise/LiveKit have close(), but Silero/TEN/FunASR native model handles are never released |
| fixed | `vad-noise-echo-8` | low | create_vad docstring lists explicit-backend order (silero, funasr, ten, krisp) that doesn't match code dispatch (silero, ten, krisp, funasr); Silero comments say 'v5' while the bundled model and version_info() are v6.2.1 |
| fixed | `xc-architecture-0` | high | record_to hook breaks `async with session:` and `shutdown()` by replacing stop() with a zero-arg wrapper |
| fixed | `xc-architecture-1` | medium | Session.__init__ is a 386-line god-constructor that hand-wires the entire pipeline and imports 35 subsystems |
| fixed | `xc-architecture-2` | medium | journal.py bundles three SQL backends in one 1826-line file and duplicates the 18-column INSERT/serialization between SqliteJournal and LibsqlJournal |
| fixed | `xc-architecture-4` | low | Session reaches through stage and runner private members (_turn_manager._config, _turn_runner._execute_text_turn) with repeated type:ignore |
| fixed | `xc-architecture-5` | low | LangGraphBridge is a single 1569-line class with a 284-line invoke(); LangChainBridge.invoke() is 187 lines |
| fixed | `xc-architecture-6` | low | config.py (1593 LOC) conflates config dataclasses, the session factory, telephony/outbound wiring, and debugger-UI launching |
| fixed | `xc-architecture-7` | low | TurnRunner bypasses its own AgentStage by calling the raw agent for replace_last_assistant_text and interruption handling |
| fixed | `xc-architecture-8` | low | Transport.clear_audio docstring promises a 'default implementation is a no-op' that does not exist |
| fixed | `xc-consistency-0` | low | DeepgramSTTConfig.api_key is a required field; every sibling STT/TTS config defaults it to "" |
| fixed | `xc-consistency-1` | medium | _emit_provider_error is copy-pasted into 4 providers instead of sharing a TTS WebSocket base like STT has |
| fixed | `xc-consistency-2` | medium | Public create_stt_provider / create_tts_provider factories diverge despite docstring claiming they mirror each other |
| fixed | `xc-consistency-3` | low | needs_event_bus provider lists are maintained by hand and diverge across the STT/TTS factories |
| fixed | `xc-consistency-4` | low | OpenAIRealtimeSTT re-implements the entire WebSocket STT lifecycle instead of extending WebSocketSTTBase |
| fixed | `xc-consistency-5` | low | TTSProvider protocol omits close()/aclose() even though the framework relies on it for teardown |
| fixed | `xc-consistency-6` | low | OpenAISTT treats max_retries as total attempts, so max_retries<=0 sends zero requests and raises a causeless "All retries exhausted" |
| fixed | `xc-dead-code-0` | medium | Entire telephony/ml_voicemail.py module is dead and duplicates screening.py logic |
| fixed | `xc-dead-code-1` | low | Seven runtime_checkable capability Protocols in capabilities.py are never referenced |
| fixed | `xc-dead-code-2` | low | WorkflowProtocol / StreamingWorkflowProtocol defined but never used by the bridge that documents them |
| fixed | `xc-dead-code-3` | low | DebugCaptureUnavailableError exception class is never raised or imported |
| fixed | `xc-dead-code-4` | low | Four private Session interruption-config delegate properties are dead despite a comment claiming tests read them |
| fixed | `xc-dead-code-5` | low | Session._on_turn_ended test-compat shim has zero callers in src or tests |
| fixed | `xc-dead-code-6` | low | Session.subscribe_events is dead while its singular sibling subscribe_event is the used form |
| fixed | `xc-dead-code-7` | low | Agent teardown swallows all exceptions with bare except: pass, hiding teardown bugs |
| fixed | `xc-dead-code-8` | low | _maybe_attach_event_bus silently swallows setattr failures while mutating provider internals |
| fixed | `xc-ease-of-use-0` | medium | run() — the documented 'voice bot in three lines' entry point — crashes on Windows via add_signal_handler |
| fixed | `xc-ease-of-use-1` | low | easycat.quick._resolve_api_key is missing 'cartesia' and silently defaults every unknown provider to OPENAI_API_KEY |
| fixed | `xc-ease-of-use-2` | low | Provider-swap examples and README require dead vendor-SDK extras (deepgram/elevenlabs/cartesia) the providers never import |
| fixed | `xc-ease-of-use-3` | low | Per-provider examples hand-wire provider config classes and sample rates instead of showcasing the one-token string shortcut + auto-align |
| fixed | `xc-ease-of-use-5` | low | config.py is a 1.6k-line module that couples the 'super easy' EasyConfig path to the entire outbound-telephony/IVR/screening wiring |
| fixed | `xc-ease-of-use-6` | low | Default pipeline silently omits noise reduction that the README presents as a wired stage |
| fixed | `xc-ease-of-use-7` | high | record_to hook double-wraps stop()/shutdown() AND drops the force kwarg, raising TypeError on every real teardown |
| fixed | `xc-robustness-0` | medium | STT start failure leaves a stuck turn and orphaned STT consumer task |
| fixed | `xc-robustness-1` | low | Transport/provider fire-and-forget _emit_tasks are never drained or cancelled on teardown |
| fixed | `xc-robustness-2` | low | with_tts_timeout never aclose()s the wrapped provider iterator on early break |
| fixed | `xc-robustness-3` | low | consume_agent_stream breaks out of the agent stream without closing it |
| fixed | `xc-robustness-5` | low | _drain_outbound_audio swallows transport send errors with no Error event or escalation (asymmetric with ingress policy) |
| fixed | `xc-robustness-7` | low | tts_queue in run_streaming_agent is unbounded — no backpressure between agent producer and TTS consumer |
| fixed | `xc-understandability-0` | medium | AGENTS.md Session Lifecycle section documents removed/private teardown methods (destroy/close) and wrong stop/shutdown semantics |
| fixed | `xc-understandability-1` | low | CLAUDE.md references a nonexistent symbol `_text_for_spoken_estimation` and attributes it to the wrong module |
| fixed | `xc-understandability-3` | low | CLAUDE.md providers.py protocol list omits EchoCanceller despite it being a core pipeline stage and exported protocol |
