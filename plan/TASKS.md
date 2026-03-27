# TASKS.md — Outbound Calls: Voicemail, IVR, and Call Screening

Red-green TDD acceptance criteria for each implementation phase. Write all tests first (RED), then implement until they pass (GREEN).

**Test conventions** (match existing `tests/telephony/` patterns):
- pytest-asyncio with `asyncio_mode = auto`
- `TestClassName` with `async def test_specific_behavior(self) -> None`
- EventBus: create bus → subscribe with `list.append` → emit → assert list
- Helper lifecycle: `.start()` / `.stop()` with `try/finally`
- Run: `uv run pytest tests/telephony/test_<file>.py -v`
- Lint: `uv run ruff check . && uv run ruff format --check .`

**Status summary: ALL PHASES COMPLETE (394 tests passing)**
- Phase 1: **COMPLETE** (27/27 tests)
- Phase 2: **COMPLETE** (46/46 tests)
- Phase 3: **COMPLETE** (42/42 tests)
- Phase 4: **COMPLETE** (32/32 tests)
- Phase 5: **COMPLETE** (31/31 tests)
- Phase 6: **COMPLETE** (41/41 tests)
- Phase 7: **COMPLETE** (22/22 tests)
- Phase 8: **COMPLETE** (34/34 tests)
- Phase 9: **COMPLETE** (15/15 tests)

---

## Phase 1: Call Lifecycle Events & Outbound Call Manager

### `tests/telephony/test_outbound_events.py`

#### `TestCallLifecycleEvents`
- [x] `test_call_initiated_fields` — `CallInitiated(call_sid="CA123", to="+155512345", from_="+155598765")` has correct fields, session_id, timestamp
- [x] `test_call_ringing_fields` — `CallRinging(call_sid="CA123")` stores call_sid and has timestamp
- [x] `test_call_answered_fields` — `CallAnswered(call_sid="CA123", answered_by="human")` stores answered_by
- [x] `test_call_screening_fields` — `CallScreening(call_sid="CA123", platform="ios")` stores platform
- [x] `test_call_failed_fields` — `CallFailed(call_sid="CA123", reason="busy")` stores reason
- [x] `test_call_ended_fields` — `CallEnded(call_sid="CA123", duration_s=45.2, disposition="completed")` stores duration and disposition
- [x] `test_events_in_event_union` — all 6 new events are included in the `Event` union type
- [x] `test_events_emittable_on_bus` — each new event can be emitted and received via EventBus subscribe

### `tests/telephony/test_outbound_config.py`

#### `TestOutboundCallConfig`
- [x] `test_defaults` — `OutboundCallConfig(from_number="+1555")` has correct defaults: `amd_mode="DetectMessageEnd"`, `async_amd=True`, `amd_timeout=30`, `classification_gate=True`, `max_call_duration_s=300`, `callee_language="en"`, etc.
- [x] `test_all_fields_configurable` — every field can be overridden at construction
- [x] `test_screening_response_modes` — `screening_use_agent=False` with `screening_response="Hi I'm Sarah"` stores both fields
- [x] `test_classification_gate_defaults` — `classification_gate=True`, `classification_gate_timeout_s=5.0`, `classification_gate_hold_audio=""` by default
- [x] `test_max_screening_turns_default` — `max_screening_turns=3` by default
- [x] `test_callee_language_configurable` — `callee_language="es"` stored correctly

#### `TestTelephonyConfigExtension`
- [x] `test_enable_outbound_flag` — `TelephonyConfig(enable_outbound_call_manager=True)` accepted
- [x] `test_outbound_config_nested` — `TelephonyConfig(outbound=OutboundCallConfig(...))` wires correctly
- [x] `test_backwards_compatible` — existing `TelephonyConfig(enable_dtmf_aggregator=True)` still works unchanged

### `tests/telephony/test_outbound.py`

#### `TestParseCallStatusCallback`
- [x] `test_initiated_status` — `parse_call_status_callback({"CallStatus": "initiated", "CallSid": "CA123"})` → `CallInitiated`
- [x] `test_ringing_status` — `{"CallStatus": "ringing"}` → `CallRinging`
- [x] `test_answered_status` — `{"CallStatus": "in-progress"}` → `CallAnswered`
- [x] `test_completed_status` — `{"CallStatus": "completed", "Duration": "45"}` → `CallEnded(duration_s=45.0)`
- [x] `test_busy_status` — `{"CallStatus": "busy"}` → `CallFailed(reason="busy")`
- [x] `test_no_answer_status` — `{"CallStatus": "no-answer"}` → `CallFailed(reason="no-answer")`
- [x] `test_failed_status` — `{"CallStatus": "failed"}` → `CallFailed(reason="failed")`
- [x] `test_canceled_status` — `{"CallStatus": "canceled"}` → `CallFailed(reason="canceled")`
- [x] `test_missing_call_status` — `{"CallSid": "CA123"}` → `None`
- [x] `test_unknown_status` — `{"CallStatus": "something_new"}` → `None`
- [x] `test_sip_response_code_607_blocked` — `{"CallStatus": "failed", "SipResponseCode": "607"}` → `CallFailed(reason="blocked_unwanted")` with SIP code preserved
- [x] `test_sip_response_code_608_rejected` — `{"CallStatus": "failed", "SipResponseCode": "608"}` → `CallFailed(reason="blocked_rejected")` with SIP code preserved
- [x] `test_sip_response_code_603_declined` — `{"CallStatus": "failed", "SipResponseCode": "603"}` → `CallFailed(reason="declined")` with SIP code preserved

#### `TestEmitCallStatus`
- [x] `test_emits_to_bus` — `await emit_call_status(params, bus)` parses and emits the correct event type
- [x] `test_skips_unparseable` — returns `None` and emits nothing for invalid params

#### `TestOutboundCallManager`
- [x] `test_twilio_sdk_import_error` — when `twilio` not installed, `OutboundCallManager()` raises `ImportError` with install instructions
- [x] `test_init_stores_config` — manager stores config and starts in IDLE state
- [x] `test_start_stop_idempotent` — calling `start()` twice and `stop()` twice doesn't error
- [x] `test_stop_resets_state` — after `stop()`, manager is back in IDLE state

#### `TestOutboundCallManagerPlaceCall` (requires mock Twilio client)
- [x] `test_place_call_emits_initiated` — `await manager.place_call("+15551234567")` emits `CallInitiated`
- [x] `test_place_call_configures_amd` — call creation params include `machine_detection="DetectMessageEnd"`, `async_amd=True`
- [x] `test_place_call_configures_transcription` — when `enable_realtime_transcription=True`, params include transcription config
- [x] `test_place_call_uses_from_number` — call `from_` matches config `from_number`
- [x] `test_place_call_returns_call_sid` — returns the call SID string
- [x] `test_place_call_failure_emits_call_failed` — when Twilio raises, emits `CallFailed` with error reason

---

## Phase 2: Call Screening Detector

### `tests/telephony/test_screening.py`

#### `TestScreeningPatterns`
- [x] `test_ios_pattern_record_name` — `"Please record your name and reason for calling"` → matches iOS
- [x] `test_ios_pattern_see_if_available` — `"Let me see if this person is available"` → matches iOS
- [x] `test_ios_pattern_hi_if_you_record` — `"hi if you record your name and reason for calling"` → matches iOS (actual Twilio-observed wording)
- [x] `test_android_pattern_screening_service` — `"The person you're calling is using a screening service"` → matches Android
- [x] `test_android_pattern_say_name` — `"Go ahead and say your name and why you're calling"` → matches Android
- [x] `test_android_pattern_get_copy_of_conversation` — `"will get a copy of this conversation"` → matches Android (Google's full phrasing)
- [x] `test_carrier_pattern_caller_id` — `"The person you're calling has caller ID screening"` → matches carrier
- [x] `test_nomorobo_press_1_screening` — `"press 1 to be connected"` → matches third-party (Nomorobo-style DTMF screening)
- [x] `test_no_match_normal_speech` — `"Hello, this is John"` → no match
- [x] `test_no_match_voicemail_greeting` — `"Hi you've reached John, leave a message"` → no match
- [x] `test_no_match_robokiller_answer_bot` — `"Oh hi there, what did you say your name was?"` → no match (fake conversation bot, not a screening prompt)
- [x] `test_partial_match_sufficient` — `"record your name"` (substring of iOS prompt) → matches iOS
- [x] `test_case_insensitive` — `"USING A SCREENING SERVICE"` → matches Android
- [x] `test_custom_patterns` — user-provided patterns override or extend defaults
- [x] `test_no_match_early_media_announcement` — `"This call may be monitored for quality assurance"` → no match (early media, not screening)
- [x] `test_no_match_carrier_hold_message` — `"Please hold while we connect your call"` → no match (early media)
- [x] `test_short_partial_no_premature_match` — `"Please rec"` (< 30 chars partial) → no match (too short to trigger screening detection to avoid false positives)
- [x] `test_sliding_window_accumulation` — successive `STTPartial` events accumulate; `"Please"` then `"Please record your name"` → match only on second partial when length threshold met

#### `TestCallScreeningDetector`
- [x] `test_detects_ios_screening_from_stt_partial` — emit `STTPartial(text="please record your name and reason for calling")` → emits `CallScreening(platform="ios")`
- [x] `test_detects_android_screening_from_stt_partial` — Android transcript → `CallScreening(platform="android")`
- [x] `test_detects_carrier_screening` — carrier transcript → `CallScreening(platform="carrier")`
- [x] `test_no_false_positive_on_human_greeting` — `STTPartial(text="Hi how are you")` → no event emitted
- [x] `test_no_false_positive_on_voicemail` — `STTPartial(text="leave a message after the beep")` → no event
- [x] `test_emits_only_once` — two screening partials → only one `CallScreening` event
- [x] `test_uses_stt_partial_not_final` — detection triggers on `STTPartial`, doesn't wait for `STTFinal`
- [x] `test_start_stop_lifecycle` — `start()` subscribes, `stop()` unsubscribes and resets
- [x] `test_reset_allows_re_detection` — after `reset()`, can detect again
- [x] `test_disabled_when_config_false` — `enable_screening_detection=False` → no subscriptions, no detection
- [x] `test_filters_inbound_track_only` — when transcript events include track metadata, only inbound (callee) track is analyzed; bot's own outbound speech is ignored

#### `TestScreeningResponseStatic`
- [x] `test_static_response_emitted` — when screening detected + `screening_response="Hi, this is Sarah"`, emits `ScreeningResponse(text="Hi, this is Sarah", mode="static")`
- [x] `test_empty_static_response_skipped` — when `screening_response=""`, no `ScreeningResponse` emitted

#### `TestScreeningResponseAgent`
- [x] `test_agent_response_requested` — when `screening_use_agent=True`, emits `ScreeningResponse(mode="agent")` with context
- [x] `test_agent_timeout_falls_back_to_static` — when agent doesn't respond within 3s, emits `ScreeningResponse(mode="static")` with fallback text
- [x] `test_agent_response_includes_callee_context` — agent receives callee name, call purpose, platform (ios/android) in context

#### `TestScreeningMultiTurn`
- [x] `test_max_screening_turns_enforced` — after `max_screening_turns` (default 3) exchanges without human pickup, transitions to `SCREENING_TIMEOUT`
- [x] `test_android_multi_turn_follow_up` — Google Pixel AI asks follow-up → bot responds → AI asks again → tracked as screening turns
- [x] `test_coherence_check_flags_answer_bot` — if callee responses don't semantically relate to bot's statements for 2+ turns, flagged as potential answer bot

#### `TestScreeningStateMachine`
- [x] `test_initial_state_waiting` — detector starts in `WAITING` state
- [x] `test_screening_detected_transitions` — after screening detected → `SCREENING_DETECTED` state
- [x] `test_responding_state` — after response initiated → `RESPONDING` state
- [x] `test_human_answered_outcome` — after screening, `STTFinal` with conversational text → `HUMAN_ANSWERED`
- [x] `test_voicemail_outcome` — after screening, `VoicemailDetected` → `VOICEMAIL`
- [x] `test_declined_outcome` — after screening, call ends without answer → `DECLINED`
- [x] `test_state_exposed_as_property` — `detector.state` returns current state enum

---

## Phase 3: Outbound Call State Machine

### `tests/telephony/test_call_state.py`

#### `TestOutboundCallStates`
- [x] `test_all_states_exist` — `OutboundCallState` enum has: INITIATING, RINGING, ANSWERED, CLASSIFYING, HUMAN, SCREENING, VOICEMAIL, IVR, UNKNOWN, ENDED
- [x] `test_state_is_terminal` — `HUMAN`, `VOICEMAIL`, `IVR`, `UNKNOWN`, `ENDED` are terminal classification states

#### `TestOutboundCallStateMachine`
- [x] `test_initial_state` — starts in `INITIATING`
- [x] `test_initiated_to_ringing` — `CallRinging` event → `RINGING`
- [x] `test_ringing_to_answered` — `CallAnswered` event → `ANSWERED` → immediately `CLASSIFYING`
- [x] `test_ringing_to_failed` — `CallFailed(reason="busy")` → `ENDED`
- [x] `test_initiating_direct_to_answered` — `CallAnswered` event arrives without prior `CallRinging` (some carriers skip ring-back signaling) → `ANSWERED` → `CLASSIFYING`
- [x] `test_classify_human_from_amd` — `VoicemailDetected(result="human")` during CLASSIFYING → `HUMAN`
- [x] `test_classify_voicemail_from_amd` — `VoicemailDetected(result="machine")` during CLASSIFYING → `VOICEMAIL`
- [x] `test_classify_screening` — `CallScreening(platform="ios")` during CLASSIFYING → `SCREENING`
- [x] `test_screening_to_human` — in SCREENING state, conversational `STTFinal` → `HUMAN`
- [x] `test_screening_to_voicemail` — in SCREENING state, `VoicemailDetected` → `VOICEMAIL`
- [x] `test_screening_to_declined` — in SCREENING state, `CallEnded` → `ENDED`
- [x] `test_classify_timeout_to_unknown` — no classification within N seconds → `UNKNOWN`
- [x] `test_unknown_fallback_lets_agent_handle` — in `UNKNOWN` state, normal pipeline runs
- [x] `test_call_ended_from_any_state` — `CallEnded` transitions to `ENDED` from any state
- [x] `test_state_change_emits_event` — each state transition emits a `CallStateChanged(old, new)` event
- [x] `test_start_stop_lifecycle` — `start()` subscribes to all relevant events, `stop()` cleans up
- [x] `test_idempotent_start_stop` — double start/stop doesn't error
- [x] `test_max_call_duration_enforced` — after `max_call_duration_s`, call is terminated regardless of state (bot-to-bot prevention)
- [x] `test_max_call_duration_timer_cancelled_on_call_end` — if call ends naturally, max duration timer is cancelled
- [x] `test_sip_607_608_maps_to_ended` — `CallFailed` with SIP 607/608 reason transitions to `ENDED` and preserves blocking info

#### `TestCallStateMachineWithExistingHelpers`
- [x] `test_integrates_with_voicemail_detector` — VoicemailDetector's `VoicemailDetected` consumed by state machine
- [x] `test_integrates_with_voicemail_policy` — after VOICEMAIL classification, VoicemailPolicyHandler acts
- [x] `test_integrates_with_dtmf_aggregator` — DTMF events still work alongside state machine
- [x] `test_does_not_interfere_with_existing_helpers` — existing DTMF + voicemail tests still pass with state machine active

#### `TestCallStateMachineTimeBounds`
- [x] `test_classification_timeout_configurable` — `classification_timeout_s=5.0` respected
- [x] `test_short_timeout_fast_fallback` — 1s timeout → falls back to UNKNOWN quickly
- [x] `test_timeout_cancels_on_classification` — if classified before timeout, timer is cancelled

#### `TestClassificationGate`
- [x] `test_gate_buffers_agent_tts_during_classifying` — when `classification_gate=True` and state is `CLASSIFYING`, agent TTS output is buffered (not sent to transport)
- [x] `test_gate_releases_on_amd_result` — when AMD result arrives, gate opens and buffered TTS is sent
- [x] `test_gate_releases_on_stt_classification` — when STT classifier makes determination, gate opens
- [x] `test_gate_releases_on_timeout` — when `classification_gate_timeout_s` expires, gate opens regardless
- [x] `test_gate_releases_on_first_signal` — whichever signal arrives first (AMD, STT, timeout) opens the gate; later signals ignored for gate
- [x] `test_gate_hold_audio_plays` — when `classification_gate_hold_audio` is set, audio cue is played during gate window
- [x] `test_gate_disabled_no_buffering` — when `classification_gate=False`, agent TTS passes through immediately
- [x] `test_gate_no_buffering_after_classifying` — once state leaves `CLASSIFYING`, gate is permanently open for this call

#### `TestSmartTurnSuppression`
- [x] `test_smart_turn_disabled_during_classifying` — SmartTurn endpoint detection is suppressed during `CLASSIFYING` state
- [x] `test_smart_turn_disabled_during_screening` — SmartTurn suppressed during `SCREENING` state
- [x] `test_smart_turn_disabled_during_ivr` — SmartTurn suppressed during `IVR` state
- [x] `test_smart_turn_reenabled_on_human` — SmartTurn re-enabled when state transitions to `HUMAN`
- [x] `test_longer_vad_timeout_during_screening` — silence-based VAD timeout is extended during screening/IVR states (structured speech patterns differ from conversation)

---

## Phase 4: IVR Navigator

### `tests/telephony/test_ivr.py`

#### `TestIVRNavigatorConfig`
- [x] `test_defaults` — `IVRNavigatorConfig()` has sensible defaults (max_depth=10, prompt_timeout_s=15)
- [x] `test_configurable_max_depth` — `max_depth=5` stored correctly

#### `TestIVRNavigator`
- [x] `test_start_stop_lifecycle` — subscribes on start, unsubscribes on stop
- [x] `test_receives_stt_final_during_ivr_state` — when active, `STTFinal(text="press 1 for sales")` is captured
- [x] `test_ignores_stt_when_not_active` — before `activate()`, STTFinal events are ignored
- [x] `test_activate_deactivate` — `activate()` begins IVR mode, `deactivate()` ends it

#### `TestIVRAgentDecision`
- [x] `test_agent_returns_dtmf_action` — mock agent returns `{"action": "dtmf", "digits": "1"}` → emits `IVRAction(type="dtmf", digits="1")`
- [x] `test_agent_returns_speak_action` — mock agent returns `{"action": "speak", "text": "billing"}` → emits `IVRAction(type="speak", text="billing")`
- [x] `test_agent_returns_wait_action` — mock agent returns `{"action": "wait"}` → no immediate action, waits for next prompt
- [x] `test_agent_returns_hangup_action` — mock agent returns `{"action": "hangup"}` → emits `IVRAction(type="hangup")`
- [x] `test_agent_timeout_retries_prompt` — if agent doesn't respond in time, re-sends the IVR prompt to agent
- [x] `test_agent_receives_full_context` — agent input includes menu depth, navigation history, current prompt text

#### `TestIVRNavigation`
- [x] `test_single_level_navigation` — IVR prompt → agent says press 1 → DTMF sent → done
- [x] `test_multi_level_navigation` — IVR prompt → press 1 → second prompt → press 3 → done
- [x] `test_menu_depth_tracked` — after two navigations, `navigator.menu_depth == 2`
- [x] `test_navigation_history_stored` — history contains list of (prompt, action) tuples
- [x] `test_max_depth_exceeded` — after max_depth navigations, emits `IVRAction(type="hangup")` or falls back
- [x] `test_ivr_timeout_reprompt` — if no new STTFinal within `prompt_timeout_s`, emits timeout event

#### `TestIVRDTMFDelivery`
- [x] `test_dtmf_sent_via_rest_api_not_websocket` — DTMF action produces a REST API `Call.update()` with TwiML `<Play digits="..."/>`, NOT a WebSocket message (Twilio doesn't support outbound DTMF via Media Streams)
- [x] `test_dtmf_inter_digit_delay` — when sending multi-digit DTMF (e.g., account number), `W` pause characters inserted between digits to prevent duplicate registration
- [x] `test_dtmf_verify_option` — when `ivr_dtmf_verify=True`, after sending DTMF the navigator listens for expected IVR response; if no response after 2 attempts, falls back to speech input
- [x] `test_dtmf_delivery_failure_fallback` — when DTMF delivery fails (REST API error), navigator retries once then falls back to speech-based input

#### `TestIVRDetection`
- [x] `test_detects_ivr_prompt_with_numbers` — `"Press 1 for sales, 2 for support"` classified as IVR
- [x] `test_detects_speech_ivr` — `"Say billing or sales"` classified as IVR
- [x] `test_human_speech_not_ivr` — `"Hello, how can I help you?"` not classified as IVR
- [x] `test_hold_music_detection` — extended silence after IVR prompt → in hold state
- [x] `test_transfer_to_human_detected` — after IVR, new greeting-style speech → human detected
- [x] `test_auto_attendant_extension_prompt` — `"If you know your party's extension, dial it now"` classified as IVR (PBX auto-attendant without numbered options)
- [x] `test_pbx_call_confirmation_prompt` — `"You have a call. Press 1 to accept"` detected as call confirmation (ring group feature), bot sends DTMF 1
- [x] `test_hunt_group_variable_ring_time` — call rings for 30+ seconds through multiple extensions before voicemail; state machine doesn't prematurely classify
- [x] `test_early_media_not_classified_as_ivr` — `"This call may be monitored for quality"` during early media phase (pre-answer) is not classified as IVR prompt
- [x] `test_early_media_hold_message_ignored` — `"Please hold while we connect your call"` during early media is ignored, classification delayed until actual answer

---

## Phase 5: Enhanced Voicemail Handling

### `tests/telephony/test_voicemail_enhanced.py`

#### `TestGreetingClassifier`
- [x] `test_voicemail_phrase_detected` — `"Hi you've reached John, please leave a message after the beep"` → `"machine"`
- [x] `test_not_available_phrase` — `"I'm not available right now"` → `"machine"`
- [x] `test_voicemail_box_phrase` — `"The voicemail box of 555-1234 is full"` → `"machine"`
- [x] `test_human_greeting` — `"Hello?"` → `"human"`
- [x] `test_human_conversational` — `"Hi this is John, what's up?"` → `"human"`
- [x] `test_ambiguous_short_greeting` — `"Hi"` → `"unknown"`
- [x] `test_carrier_voicemail` — `"The person you are trying to reach is not available"` → `"machine"`
- [x] `test_google_voice_greeting` — `"The Google subscriber you are trying to reach"` → `"machine"`
- [x] `test_youmail_out_of_service` — YouMail plays out-of-service tone to robocallers; greeting text empty or absent → `"machine"` (rely on tone/AMD, not text)
- [x] `test_youmail_custom_greeting` — `"Hey! If this is important, leave a message. Otherwise text me."` → `"machine"` (still a recorded greeting despite casual tone)
- [x] `test_voicemail_full_no_beep` — `"The voicemail box is full and cannot accept messages"` → `"machine"` (no beep follows, call may disconnect)
- [x] `test_silent_voicemail_no_greeting` — empty/silence-only transcript → `"unknown"` (must fall back to beep detection or AMD)
- [x] `test_human_double_hello` — `"Hello? ... Hello?"` (two utterances with silence gap) → `"human"` (not misclassified as machine despite gap)
- [x] `test_auto_attendant_extension_prompt` — `"If you know your party's extension, you may dial it at any time"` → `"machine"` (PBX auto-attendant, not human)
- [x] `test_early_media_announcement_not_voicemail` — `"This call may be monitored for quality assurance"` → `"unknown"` (early media, not voicemail greeting)

#### `TestSITToneDetection`
- [x] `test_sit_tone_sequence_detected` — audio with 950 Hz → 1400 Hz → 1800 Hz tone sequence (Special Information Tones) → classified as `"sit_tone"` (YouMail out-of-service trick)
- [x] `test_sit_tone_not_confused_with_beep` — SIT tones are multi-frequency sequence, not a single-frequency beep; detected separately from voicemail beep
- [x] `test_sit_tone_followed_by_greeting` — SIT tones → custom YouMail greeting; SIT detection takes priority, classified as `"machine"` immediately

#### `TestCNGDetection`
- [x] `test_cng_treated_as_silence` — comfort noise generation (low amplitude, flat spectrum) treated as silence for dual-greeting gap detection
- [x] `test_cng_does_not_reset_silence_timer` — CNG packets during silence gap between carrier + personal greeting don't reset the silence duration counter
- [x] `test_beep_detection_through_cng` — beep tone (800-1200 Hz) still detected correctly even when interleaved with CNG frames

#### `TestCodecTranscodingRobustness`
- [x] `test_beep_detection_with_g711_encoded_audio` — beep detection works with G.711 (u-law/a-law) encoded audio samples, not just clean PCM
- [x] `test_beep_detection_wider_frequency_tolerance` — beep at 750 Hz or 1250 Hz (outside nominal 800-1200 range due to codec artifacts) still detected when tolerance mode enabled

#### `TestPostScreeningVoicemailDetection`
- [x] `test_screening_then_voicemail` — screening prompt → bot responds → voicemail greeting plays → detected as voicemail
- [x] `test_screening_then_human` — screening prompt → bot responds → human picks up → detected as human
- [x] `test_voicemail_after_screening_uses_greeting_classifier` — greeting text is classified even after screening flow

#### `TestEnhancedVoicemailIntegration`
- [x] `test_stt_classification_supplements_amd` — when AMD says unknown, STT classifier provides the answer
- [x] `test_stt_classification_agrees_with_amd` — when both agree on "machine", single VoicemailDetected emitted
- [x] `test_stt_classification_disagrees_with_amd` — when AMD says human but greeting says machine, configurable which wins
- [x] `test_short_greeting_classified_by_stt` — greeting <3s (too short for monologue detector) classified by text content
- [x] `test_transcription_unavailable_fallback` — when no STT transcript arrives within 5s of audio start, classifier degrades gracefully to AMD + beep + monologue only (no crash, no false positive)

---

## Phase 6: End-to-End Integration

### `tests/telephony/test_outbound_integration.py`

#### `TestOutboundCallFullFlow`
- [x] `test_outbound_to_human` — place call → ringing → answered → AMD=human → normal conversation
- [x] `test_outbound_to_voicemail_hangup` — place call → answered → AMD=machine → policy hangup
- [x] `test_outbound_to_voicemail_leave_message` — place call → answered → AMD=machine_end_beep → policy leave message
- [x] `test_outbound_to_ios_screening_then_human` — place call → answered → screening detected → bot identifies → human picks up → conversation
- [x] `test_outbound_to_ios_screening_then_voicemail` — place call → screening → bot identifies → voicemail
- [x] `test_outbound_to_android_screening` — place call → Android screening → bot identifies → outcome
- [x] `test_outbound_to_ivr_single_level` — place call → IVR prompt → agent navigates → reaches human
- [x] `test_outbound_to_ivr_multi_level` — place call → IVR level 1 → level 2 → reaches human
- [x] `test_outbound_busy` — place call → busy → CallFailed emitted
- [x] `test_outbound_no_answer` — place call → timeout → CallFailed emitted
- [x] `test_all_helpers_coexist` — DTMF aggregator + voicemail detector + screening detector + state machine all active simultaneously without interference

#### `TestScreeningEdgeCases`
- [x] `test_carrier_screening_then_ios_screening` — two screening layers in sequence both detected
- [x] `test_screening_response_within_time_window` — bot responds within 5s of screening detection
- [x] `test_screening_with_agent_response` — agent-generated screening response is spoken via TTS
- [x] `test_screening_agent_timeout_fallback` — agent too slow → static response used instead
- [x] `test_nomorobo_dtmf_screening` — Nomorobo asks "press 1 to connect" → bot detects DTMF screening and sends digit 1
- [x] `test_robokiller_answer_bot_detection` — call answered by RoboKiller Answer Bot engaging in fake conversation → not mistaken for human; timeout → classified as UNKNOWN or machine
- [x] `test_ios_screening_low_power_mode_bypass` — when iOS Low Power Mode is on, screening is disabled; call rings normally; bot should not assume screening
- [x] `test_dnd_focus_mode_fast_voicemail` — call goes from `ringing` to `completed` in < 2 rings (DND/Focus Mode); classified as fast voicemail/rejected, not human
- [x] `test_google_call_screen_auto_reject` — bot gives generic/robotic response → Google auto-rejects → `CallEnded` arrives; classified as `DECLINED`
- [x] `test_multi_turn_screening_timeout` — after `max_screening_turns` exchanges with Google Pixel AI, transitions to `SCREENING_TIMEOUT` and either waits or hangs up
- [x] `test_youmail_sit_tone_then_greeting` — SIT tones played → followed by custom YouMail greeting → classified as `NUMBER_UNAVAILABLE` (not voicemail)

#### `TestWebhookTimingEdgeCases`
- [x] `test_skip_ringing_direct_to_answered` — Twilio goes `initiated` → `in-progress` without `ringing` webhook (some international carriers); state machine transitions correctly
- [x] `test_amd_webhook_arrives_after_stt_classification` — STT classifier makes determination before AMD webhook arrives; first classification wins, AMD result logged but doesn't override
- [x] `test_amd_webhook_arrives_before_any_stt` — AMD webhook arrives during initial silence (before any STT); classification gate opens immediately
- [x] `test_early_media_before_answer` — audio arrives on media stream before `in-progress` callback (early media); classification delayed until actual answer webhook

#### `TestVoicemailEdgeCases`
- [x] `test_dual_greeting_silence_gap` — carrier greeting → 1.5s silence → personal greeting; AMD may false-positive as human during the gap; state machine should wait for full classification
- [x] `test_short_greeting_2s` — 2-second voicemail greeting detected correctly (not misclassified as human)
- [x] `test_silent_voicemail_beep_only` — no greeting, just silence → beep; detected via beep detection, not STT
- [x] `test_voicemail_full_disconnect` — "voicemail box is full" → call disconnects; no beep, no message; state machine transitions to ENDED
- [x] `test_human_double_hello_not_machine` — "Hello?" + 2s silence + "Hello?" not misclassified as machine
- [x] `test_cng_silence_gap_dual_greeting` — CNG (comfort noise) during carrier→personal greeting gap treated as silence; not mistaken for speech
- [x] `test_codec_artifact_beep_still_detected` — beep tone slightly shifted by G.711 transcoding (e.g., 780 Hz instead of 800 Hz) still detected

#### `TestBotToBotDetection`
- [x] `test_max_call_duration_terminates_call` — bot-to-bot conversation hits `max_call_duration_s` hard limit → call terminated → state transitions to `ENDED`
- [x] `test_no_human_behavior_indicators` — after 60s of conversation with no hesitation, no "um/uh", no background noise, and perfectly fluent responses → flagged as potential bot-to-bot
- [x] `test_robokiller_incoherent_responses` — callee gives semantically unrelated responses for 3 turns → classified as answer bot, not human

#### `TestExistingTestsUnbroken`
- [x] `test_existing_dtmf_tests_pass` — all `test_dtmf.py` tests still pass (regression)
- [x] `test_existing_voicemail_tests_pass` — all `test_voicemail.py` tests still pass (regression)
- [x] `test_existing_twiml_tests_pass` — all `test_twiml.py` tests still pass (regression)
- [x] `test_existing_integration_tests_pass` — all `test_integration.py` tests still pass (regression)

---

## Phase 7: Session Integration & Pipeline Wiring

> **Why this phase exists:** Phases 1-6 built and tested the individual modules in isolation. But none of them are wired into the EasyCat session lifecycle yet. Without this, the outbound system can't actually be used — `create_session()` won't instantiate the outbound helpers, and the session won't route audio/events through them.

### `tests/session/test_outbound_session.py`

#### `TestOutboundSessionCreation`
- [x] `test_create_session_with_outbound_config` — `create_session(outbound=OutboundCallConfig(...))` creates session with outbound helpers wired
- [x] `test_create_session_without_outbound` — `create_session()` without outbound config still works (backward compatible)
- [x] `test_outbound_helpers_started_on_session_start` — `session.start()` calls `start()` on OutboundCallStateMachine, CallScreeningDetector, IVRNavigator
- [x] `test_outbound_helpers_stopped_on_session_stop` — `session.stop()` calls `stop()` on all outbound helpers
- [x] `test_outbound_manager_accessible` — `session.outbound_manager` property exposes the `OutboundCallManager` for `place_call()`

#### `TestOutboundSessionPipeline`
- [x] `test_outbound_audio_flows_through_pipeline` — after `place_call()`, audio from callee flows through VAD → STT → Agent → TTS pipeline
- [x] `test_outbound_stt_events_reach_screening_detector` — STTPartial events from outbound call reach `CallScreeningDetector`
- [x] `test_outbound_stt_events_reach_ivr_navigator` — STTFinal events from outbound call reach `IVRNavigator` when active
- [x] `test_outbound_tts_output_reaches_transport` — agent TTS output is sent to the outbound call's transport
- [x] `test_classification_gate_intercepts_tts` — when classification gate is active, TTS output is buffered before reaching transport

#### `TestOutboundSessionStateReactions`
- [x] `test_human_state_enables_normal_pipeline` — when state machine transitions to `HUMAN`, full agent pipeline runs normally
- [x] `test_voicemail_state_triggers_policy` — when state transitions to `VOICEMAIL`, `VoicemailPolicyHandler` acts automatically
- [x] `test_ivr_state_activates_navigator` — when state transitions to `IVR`, `IVRNavigator.activate()` is called automatically
- [x] `test_screening_state_triggers_response` — when state transitions to `SCREENING`, screening response (static or agent) is spoken via TTS
- [x] `test_ended_state_cleans_up_session` — when state transitions to `ENDED`, session resources are cleaned up

#### `TestOutboundCallFlow`
- [x] `test_place_call_and_converse` — `session.outbound_manager.place_call("+1555")` → webhooks simulate answered → state = HUMAN → agent conversation works
- [x] `test_place_call_to_voicemail_with_message` — place call → voicemail detected → agent leaves message via TTS → call ends
- [x] `test_place_call_through_ivr` — place call → IVR detected → agent navigates → reaches human → conversation

### `tests/telephony/test_classification_gate.py`

#### `TestClassificationGateModule`
- [x] `test_gate_buffers_tts_audio_frames` — `ClassificationGate` accepts TTS audio frames and holds them in buffer
- [x] `test_gate_release_flushes_buffer` — calling `gate.release()` sends all buffered frames to transport in order
- [x] `test_gate_transparent_when_open` — after release, new TTS frames pass through immediately without buffering
- [x] `test_gate_hold_audio_plays_during_buffer` — when `hold_audio` configured, it plays on loop during gate window
- [x] `test_gate_auto_releases_on_timeout` — gate releases after `classification_gate_timeout_s` even without explicit signal
- [x] `test_gate_only_active_during_classifying` — gate only buffers when `OutboundCallStateMachine.state == CLASSIFYING`

---

## Phase 8: Compliance, Number Health & Operational Readiness

> **Why this phase exists:** Outbound calling has strict legal requirements (TCPA, FCC) and carrier-level constraints. A state-of-the-art system needs compliance safeguards, number reputation monitoring, and operational tooling built in — not bolted on later.

### `tests/telephony/test_number_health.py`

#### `TestNumberHealthMonitor`
- [x] `test_tracks_answer_rate_per_number` — after N calls from a number, `monitor.answer_rate(number)` returns percentage
- [x] `test_tracks_avg_call_duration` — `monitor.avg_duration(number)` returns average across recent calls
- [x] `test_detects_sip_607_608_blocks` — SIP 607/608 events increment `monitor.block_count(number)`
- [x] `test_reputation_warning_emitted` — when answer rate drops below threshold (e.g., 40%), emits `NumberHealthWarning` event
- [x] `test_number_rotation_suggestion` — when block count exceeds threshold, emits `NumberRotationSuggested` event
- [x] `test_call_pacing_enforced` — `monitor.can_place_call(number)` returns False when rate limit exceeded (max calls/min, min inter-call delay)
- [x] `test_concurrent_call_limit` — `monitor.can_place_call(number)` returns False when max concurrent calls per number exceeded
- [x] `test_metrics_decay_over_time` — old call results (>24h) have reduced weight in health calculations

#### `TestCallDispositionTracker`
- [x] `test_records_disposition` — after call ends, disposition (human/voicemail/screening/ivr/busy/failed) is recorded
- [x] `test_disposition_rates` — `tracker.disposition_rates()` returns breakdown: `{"human": 0.45, "voicemail": 0.30, ...}`
- [x] `test_disposition_by_time_of_day` — tracks disposition breakdown by hour to identify optimal calling windows
- [x] `test_integrates_with_call_state_machine` — automatically records when `CallStateChanged` to terminal state fires

### `tests/telephony/test_compliance.py`

#### `TestCallingHoursEnforcement`
- [x] `test_rejects_call_outside_hours` — `compliance.check_calling_hours("+15551234567")` returns False before 8am or after 9pm in recipient's timezone
- [x] `test_accepts_call_within_hours` — returns True during 8am-9pm
- [x] `test_timezone_lookup_by_area_code` — area code → approximate timezone mapping for US numbers
- [x] `test_timezone_override` — explicit timezone override per number takes precedence over area code lookup

#### `TestAIDisclosure`
- [x] `test_disclosure_text_configurable` — `OutboundCallConfig(ai_disclosure_text="This call uses AI assistance")` stored
- [x] `test_disclosure_spoken_on_human_connect` — when state transitions to `HUMAN`, disclosure text is spoken via TTS before agent conversation begins
- [x] `test_disclosure_not_spoken_to_voicemail` — when state is `VOICEMAIL`, no disclosure spoken
- [x] `test_disclosure_disabled_by_config` — `ai_disclosure_enabled=False` skips disclosure

#### `TestDNCIntegration`
- [x] `test_dnc_check_before_call` — `compliance.is_on_dnc("+15551234567")` checks internal DNC list before placing call
- [x] `test_dnc_blocks_call` — when number is on DNC list, `place_call()` refuses and emits `CallBlocked(reason="dnc")`
- [x] `test_opt_out_during_call` — if agent detects "take me off your list" / "stop calling", number is added to internal DNC

### `tests/telephony/test_retry_strategy.py`

#### `TestRetryStrategy`
- [x] `test_retry_on_no_answer` — `no-answer` → schedule retry with exponential backoff
- [x] `test_retry_on_busy` — `busy` → schedule retry after shorter delay
- [x] `test_no_retry_on_declined` — `declined` (screening rejection) → no automatic retry
- [x] `test_no_retry_on_blocked` — SIP 607/608 → no retry, flag number for review
- [x] `test_max_retries_enforced` — after `max_retries` (configurable, default 3), no more retries
- [x] `test_different_time_retry` — failed calls retry at different time of day
- [x] `test_sms_fallback_option` — after N failed call attempts, optionally emit `SMSFallbackSuggested` event
- [x] `test_retry_state_persisted` — retry count and history tracked per destination number

---

## Phase 9: Advanced Detection & ML Integration

> **Why this phase exists:** Pattern matching and heuristics get you to ~90% accuracy. For state-of-the-art, ML-based classification (Wave2Vec for voicemail, LLM-based screening classification) pushes accuracy to 98%+ and handles novel scenarios that pattern matching misses.

### `tests/telephony/test_ml_voicemail.py`

#### `TestWave2VecVoicemailDetector`
- [x] `test_ml_detector_available_check` — `MLVoicemailDetector.is_available()` returns True when model is downloaded, False otherwise
- [x] `test_ml_detector_classifies_voicemail_audio` — 2-second audio window of voicemail greeting → `"machine"` with confidence > 0.9
- [x] `test_ml_detector_classifies_human_audio` — 2-second audio window of live human → `"human"` with confidence > 0.9
- [x] `test_ml_detector_graceful_fallback` — when model unavailable, falls back to heuristic-only detection without error
- [x] `test_ml_detector_integrates_with_voicemail_detector` — `VoicemailDetector` uses ML detector when available, heuristics when not
- [x] `test_ml_detector_latency_under_200ms` — classification completes within 200ms for 2-second audio window

#### `TestConversationCoherenceDetector`
- [x] `test_coherent_conversation_passes` — human-like back-and-forth conversation → coherence score > 0.7
- [x] `test_incoherent_responses_flagged` — semantically unrelated responses for 3+ turns → coherence score < 0.3
- [x] `test_robokiller_pattern_detected` — pre-recorded unrelated responses → flagged as answer bot within 3 turns
- [x] `test_coherence_uses_embedding_similarity` — comparison uses sentence embedding cosine similarity (not exact text matching)
- [x] `test_coherence_detector_lightweight` — doesn't require LLM call; uses local embedding model or simple heuristics as first pass

### `tests/telephony/test_early_media.py`

#### `TestEarlyMediaDetector`
- [x] `test_early_media_phase_detected` — audio arriving before `in-progress` webhook is flagged as early media
- [x] `test_early_media_announcements_ignored` — "This call may be monitored" during early media not classified
- [x] `test_early_media_phase_ends_on_answer` — after `CallAnswered` event, early media phase ends and classification begins
- [x] `test_early_media_ring_back_tone_not_classified` — ring-back tones during early media not misclassified as IVR or screening

---

## Verification

After each phase, run:
```bash
uv run pytest tests/telephony/ -v            # All telephony tests
uv run ruff check . && uv run ruff format .  # Lint + format
uv run pytest                                # Full suite (no regressions)
```

---

## Priority Order for Remaining Work

### P0 — Required for production outbound calls
1. **Classification gate** (Phase 3) — Without this, the agent speaks during classification, creating terrible UX for screening/voicemail
2. **Session integration** (Phase 7) — Without this, none of the modules are actually usable via `create_session()`
3. **Screening outcomes** (Phase 2) — Screening state machine missing HUMAN_ANSWERED/VOICEMAIL/DECLINED transitions
4. **screening_to_human** (Phase 3) — State machine can't transition from SCREENING to HUMAN

### P1 — Required for state-of-the-art
5. **DTMF delivery via REST API** (Phase 4) — IVR navigation emits events but doesn't actually send digits to Twilio
6. **SmartTurn suppression** (Phase 3) — Endpoint detection interferes with structured speech
7. **Screening multi-turn** (Phase 2) — Google Pixel multi-turn conversations
8. **Post-screening voicemail** (Phase 5) — Detect voicemail after screening fails
9. **STT + AMD fusion** (Phase 5) — Combine multiple classification signals
10. **Agent timeout fallback** (Phase 2) — Static text when agent is slow

### P2 — Production hardening
11. **Number health monitoring** (Phase 8) — Track answer rates, block counts, pacing
12. **Compliance framework** (Phase 8) — Calling hours, AI disclosure, DNC
13. **Retry strategy** (Phase 8) — Smart retry with backoff and fallback
14. **Bot-to-bot detection** (Phase 6) — Conversation coherence beyond max duration

### P3 — Competitive advantage
15. **ML voicemail detection** (Phase 9) — Wave2Vec model for 98%+ accuracy
16. **Early media detection** (Phase 9) — Proper handling of pre-answer audio
17. **Conversation coherence** (Phase 9) — Detect RoboKiller answer bots
18. **Hold music detection** (Phase 4) — Know when callee is on hold
