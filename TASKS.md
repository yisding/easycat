# TASKS.md — Outbound Calls: Voicemail, IVR, and Call Screening

Red-green TDD acceptance criteria for each implementation phase. Write all tests first (RED), then implement until they pass (GREEN).

**Test conventions** (match existing `tests/telephony/` patterns):
- pytest-asyncio with `asyncio_mode = auto`
- `TestClassName` with `async def test_specific_behavior(self) -> None`
- EventBus: create bus → subscribe with `list.append` → emit → assert list
- Helper lifecycle: `.start()` / `.stop()` with `try/finally`
- Run: `uv run pytest tests/telephony/test_<file>.py -v`
- Lint: `uv run ruff check . && uv run ruff format --check .`

---

## Phase 1: Call Lifecycle Events & Outbound Call Manager

### `tests/telephony/test_outbound_events.py`

#### `TestCallLifecycleEvents`
- [ ] `test_call_initiated_fields` — `CallInitiated(call_sid="CA123", to="+155512345", from_="+155598765")` has correct fields, session_id, timestamp
- [ ] `test_call_ringing_fields` — `CallRinging(call_sid="CA123")` stores call_sid and has timestamp
- [ ] `test_call_answered_fields` — `CallAnswered(call_sid="CA123", answered_by="human")` stores answered_by
- [ ] `test_call_screening_fields` — `CallScreening(call_sid="CA123", platform="ios")` stores platform
- [ ] `test_call_failed_fields` — `CallFailed(call_sid="CA123", reason="busy")` stores reason
- [ ] `test_call_ended_fields` — `CallEnded(call_sid="CA123", duration_s=45.2, disposition="completed")` stores duration and disposition
- [ ] `test_events_in_event_union` — all 6 new events are included in the `Event` union type
- [ ] `test_events_emittable_on_bus` — each new event can be emitted and received via EventBus subscribe

### `tests/telephony/test_outbound_config.py`

#### `TestOutboundCallConfig`
- [ ] `test_defaults` — `OutboundCallConfig(from_number="+1555")` has correct defaults: `amd_mode="DetectMessageEnd"`, `async_amd=True`, `amd_timeout=30`, etc.
- [ ] `test_all_fields_configurable` — every field can be overridden at construction
- [ ] `test_screening_response_modes` — `screening_use_agent=False` with `screening_response="Hi I'm Sarah"` stores both fields

#### `TestTelephonyConfigExtension`
- [ ] `test_enable_outbound_flag` — `TelephonyConfig(enable_outbound_call_manager=True)` accepted
- [ ] `test_outbound_config_nested` — `TelephonyConfig(outbound=OutboundCallConfig(...))` wires correctly
- [ ] `test_backwards_compatible` — existing `TelephonyConfig(enable_dtmf_aggregator=True)` still works unchanged

### `tests/telephony/test_outbound.py`

#### `TestParseCallStatusCallback`
- [ ] `test_initiated_status` — `parse_call_status_callback({"CallStatus": "initiated", "CallSid": "CA123"})` → `CallInitiated`
- [ ] `test_ringing_status` — `{"CallStatus": "ringing"}` → `CallRinging`
- [ ] `test_answered_status` — `{"CallStatus": "in-progress"}` → `CallAnswered`
- [ ] `test_completed_status` — `{"CallStatus": "completed", "Duration": "45"}` → `CallEnded(duration_s=45.0)`
- [ ] `test_busy_status` — `{"CallStatus": "busy"}` → `CallFailed(reason="busy")`
- [ ] `test_no_answer_status` — `{"CallStatus": "no-answer"}` → `CallFailed(reason="no-answer")`
- [ ] `test_failed_status` — `{"CallStatus": "failed"}` → `CallFailed(reason="failed")`
- [ ] `test_canceled_status` — `{"CallStatus": "canceled"}` → `CallFailed(reason="canceled")`
- [ ] `test_missing_call_status` — `{"CallSid": "CA123"}` → `None`
- [ ] `test_unknown_status` — `{"CallStatus": "something_new"}` → `None`

#### `TestEmitCallStatus`
- [ ] `test_emits_to_bus` — `await emit_call_status(params, bus)` parses and emits the correct event type
- [ ] `test_skips_unparseable` — returns `None` and emits nothing for invalid params

#### `TestOutboundCallManager`
- [ ] `test_twilio_sdk_import_error` — when `twilio` not installed, `OutboundCallManager()` raises `ImportError` with install instructions
- [ ] `test_init_stores_config` — manager stores config and starts in IDLE state
- [ ] `test_start_stop_idempotent` — calling `start()` twice and `stop()` twice doesn't error
- [ ] `test_stop_resets_state` — after `stop()`, manager is back in IDLE state

#### `TestOutboundCallManagerPlaceCall` (requires mock Twilio client)
- [ ] `test_place_call_emits_initiated` — `await manager.place_call("+15551234567")` emits `CallInitiated`
- [ ] `test_place_call_configures_amd` — call creation params include `machine_detection="DetectMessageEnd"`, `async_amd=True`
- [ ] `test_place_call_configures_transcription` — when `enable_realtime_transcription=True`, params include transcription config
- [ ] `test_place_call_uses_from_number` — call `from_` matches config `from_number`
- [ ] `test_place_call_returns_call_sid` — returns the call SID string
- [ ] `test_place_call_failure_emits_call_failed` — when Twilio raises, emits `CallFailed` with error reason

---

## Phase 2: Call Screening Detector

### `tests/telephony/test_screening.py`

#### `TestScreeningPatterns`
- [ ] `test_ios_pattern_record_name` — `"Please record your name and reason for calling"` → matches iOS
- [ ] `test_ios_pattern_see_if_available` — `"Let me see if this person is available"` → matches iOS
- [ ] `test_android_pattern_screening_service` — `"The person you're calling is using a screening service"` → matches Android
- [ ] `test_android_pattern_say_name` — `"Go ahead and say your name and why you're calling"` → matches Android
- [ ] `test_carrier_pattern_caller_id` — `"The person you're calling has caller ID screening"` → matches carrier
- [ ] `test_no_match_normal_speech` — `"Hello, this is John"` → no match
- [ ] `test_no_match_voicemail_greeting` — `"Hi you've reached John, leave a message"` → no match
- [ ] `test_partial_match_sufficient` — `"record your name"` (substring of iOS prompt) → matches iOS
- [ ] `test_case_insensitive` — `"USING A SCREENING SERVICE"` → matches Android
- [ ] `test_custom_patterns` — user-provided patterns override or extend defaults

#### `TestCallScreeningDetector`
- [ ] `test_detects_ios_screening_from_stt_partial` — emit `STTPartial(text="please record your name and reason for calling")` → emits `CallScreening(platform="ios")`
- [ ] `test_detects_android_screening_from_stt_partial` — Android transcript → `CallScreening(platform="android")`
- [ ] `test_detects_carrier_screening` — carrier transcript → `CallScreening(platform="carrier")`
- [ ] `test_no_false_positive_on_human_greeting` — `STTPartial(text="Hi how are you")` → no event emitted
- [ ] `test_no_false_positive_on_voicemail` — `STTPartial(text="leave a message after the beep")` → no event
- [ ] `test_emits_only_once` — two screening partials → only one `CallScreening` event
- [ ] `test_uses_stt_partial_not_final` — detection triggers on `STTPartial`, doesn't wait for `STTFinal`
- [ ] `test_start_stop_lifecycle` — `start()` subscribes, `stop()` unsubscribes and resets
- [ ] `test_reset_allows_re_detection` — after `reset()`, can detect again
- [ ] `test_disabled_when_config_false` — `enable_screening_detection=False` → no subscriptions, no detection

#### `TestScreeningResponseStatic`
- [ ] `test_static_response_emitted` — when screening detected + `screening_response="Hi, this is Sarah"`, emits `ScreeningResponse(text="Hi, this is Sarah", mode="static")`
- [ ] `test_empty_static_response_skipped` — when `screening_response=""`, no `ScreeningResponse` emitted

#### `TestScreeningResponseAgent`
- [ ] `test_agent_response_requested` — when `screening_use_agent=True`, emits `ScreeningResponse(mode="agent")` with context
- [ ] `test_agent_timeout_falls_back_to_static` — when agent doesn't respond within 3s, emits `ScreeningResponse(mode="static")` with fallback text

#### `TestScreeningStateMachine`
- [ ] `test_initial_state_waiting` — detector starts in `WAITING` state
- [ ] `test_screening_detected_transitions` — after screening detected → `SCREENING_DETECTED` state
- [ ] `test_responding_state` — after response initiated → `RESPONDING` state
- [ ] `test_human_answered_outcome` — after screening, `STTFinal` with conversational text → `HUMAN_ANSWERED`
- [ ] `test_voicemail_outcome` — after screening, `VoicemailDetected` → `VOICEMAIL`
- [ ] `test_declined_outcome` — after screening, call ends without answer → `DECLINED`
- [ ] `test_state_exposed_as_property` — `detector.state` returns current state enum

---

## Phase 3: Outbound Call State Machine

### `tests/telephony/test_call_state.py`

#### `TestOutboundCallStates`
- [ ] `test_all_states_exist` — `OutboundCallState` enum has: INITIATING, RINGING, ANSWERED, CLASSIFYING, HUMAN, SCREENING, VOICEMAIL, IVR, UNKNOWN, ENDED
- [ ] `test_state_is_terminal` — `HUMAN`, `VOICEMAIL`, `IVR`, `UNKNOWN`, `ENDED` are terminal classification states

#### `TestOutboundCallStateMachine`
- [ ] `test_initial_state` — starts in `INITIATING`
- [ ] `test_initiated_to_ringing` — `CallRinging` event → `RINGING`
- [ ] `test_ringing_to_answered` — `CallAnswered` event → `ANSWERED` → immediately `CLASSIFYING`
- [ ] `test_ringing_to_failed` — `CallFailed(reason="busy")` → `ENDED`
- [ ] `test_classify_human_from_amd` — `VoicemailDetected(result="human")` during CLASSIFYING → `HUMAN`
- [ ] `test_classify_voicemail_from_amd` — `VoicemailDetected(result="machine")` during CLASSIFYING → `VOICEMAIL`
- [ ] `test_classify_screening` — `CallScreening(platform="ios")` during CLASSIFYING → `SCREENING`
- [ ] `test_screening_to_human` — in SCREENING state, conversational `STTFinal` → `HUMAN`
- [ ] `test_screening_to_voicemail` — in SCREENING state, `VoicemailDetected` → `VOICEMAIL`
- [ ] `test_screening_to_declined` — in SCREENING state, `CallEnded` → `ENDED`
- [ ] `test_classify_timeout_to_unknown` — no classification within N seconds → `UNKNOWN`
- [ ] `test_unknown_fallback_lets_agent_handle` — in `UNKNOWN` state, normal pipeline runs
- [ ] `test_call_ended_from_any_state` — `CallEnded` transitions to `ENDED` from any state
- [ ] `test_state_change_emits_event` — each state transition emits a `CallStateChanged(old, new)` event
- [ ] `test_start_stop_lifecycle` — `start()` subscribes to all relevant events, `stop()` cleans up
- [ ] `test_idempotent_start_stop` — double start/stop doesn't error

#### `TestCallStateMachineWithExistingHelpers`
- [ ] `test_integrates_with_voicemail_detector` — VoicemailDetector's `VoicemailDetected` consumed by state machine
- [ ] `test_integrates_with_voicemail_policy` — after VOICEMAIL classification, VoicemailPolicyHandler acts
- [ ] `test_integrates_with_dtmf_aggregator` — DTMF events still work alongside state machine
- [ ] `test_does_not_interfere_with_existing_helpers` — existing DTMF + voicemail tests still pass with state machine active

#### `TestCallStateMachineTimeBounds`
- [ ] `test_classification_timeout_configurable` — `classification_timeout_s=5.0` respected
- [ ] `test_short_timeout_fast_fallback` — 1s timeout → falls back to UNKNOWN quickly
- [ ] `test_timeout_cancels_on_classification` — if classified before timeout, timer is cancelled

---

## Phase 4: IVR Navigator

### `tests/telephony/test_ivr.py`

#### `TestIVRNavigatorConfig`
- [ ] `test_defaults` — `IVRNavigatorConfig()` has sensible defaults (max_depth=10, prompt_timeout_s=15)
- [ ] `test_configurable_max_depth` — `max_depth=5` stored correctly

#### `TestIVRNavigator`
- [ ] `test_start_stop_lifecycle` — subscribes on start, unsubscribes on stop
- [ ] `test_receives_stt_final_during_ivr_state` — when active, `STTFinal(text="press 1 for sales")` is captured
- [ ] `test_ignores_stt_when_not_active` — before `activate()`, STTFinal events are ignored
- [ ] `test_activate_deactivate` — `activate()` begins IVR mode, `deactivate()` ends it

#### `TestIVRAgentDecision`
- [ ] `test_agent_returns_dtmf_action` — mock agent returns `{"action": "dtmf", "digits": "1"}` → emits `IVRAction(type="dtmf", digits="1")`
- [ ] `test_agent_returns_speak_action` — mock agent returns `{"action": "speak", "text": "billing"}` → emits `IVRAction(type="speak", text="billing")`
- [ ] `test_agent_returns_wait_action` — mock agent returns `{"action": "wait"}` → no immediate action, waits for next prompt
- [ ] `test_agent_returns_hangup_action` — mock agent returns `{"action": "hangup"}` → emits `IVRAction(type="hangup")`
- [ ] `test_agent_timeout_retries_prompt` — if agent doesn't respond in time, re-sends the IVR prompt to agent
- [ ] `test_agent_receives_full_context` — agent input includes menu depth, navigation history, current prompt text

#### `TestIVRNavigation`
- [ ] `test_single_level_navigation` — IVR prompt → agent says press 1 → DTMF sent → done
- [ ] `test_multi_level_navigation` — IVR prompt → press 1 → second prompt → press 3 → done
- [ ] `test_menu_depth_tracked` — after two navigations, `navigator.menu_depth == 2`
- [ ] `test_navigation_history_stored` — history contains list of (prompt, action) tuples
- [ ] `test_max_depth_exceeded` — after max_depth navigations, emits `IVRAction(type="hangup")` or falls back
- [ ] `test_ivr_timeout_reprompt` — if no new STTFinal within `prompt_timeout_s`, emits timeout event

#### `TestIVRDetection`
- [ ] `test_detects_ivr_prompt_with_numbers` — `"Press 1 for sales, 2 for support"` classified as IVR
- [ ] `test_detects_speech_ivr` — `"Say billing or sales"` classified as IVR
- [ ] `test_human_speech_not_ivr` — `"Hello, how can I help you?"` not classified as IVR
- [ ] `test_hold_music_detection` — extended silence after IVR prompt → in hold state
- [ ] `test_transfer_to_human_detected` — after IVR, new greeting-style speech → human detected

---

## Phase 5: Enhanced Voicemail Handling

### `tests/telephony/test_voicemail_enhanced.py`

#### `TestGreetingClassifier`
- [ ] `test_voicemail_phrase_detected` — `"Hi you've reached John, please leave a message after the beep"` → `"machine"`
- [ ] `test_not_available_phrase` — `"I'm not available right now"` → `"machine"`
- [ ] `test_voicemail_box_phrase` — `"The voicemail box of 555-1234 is full"` → `"machine"`
- [ ] `test_human_greeting` — `"Hello?"` → `"human"`
- [ ] `test_human_conversational` — `"Hi this is John, what's up?"` → `"human"`
- [ ] `test_ambiguous_short_greeting` — `"Hi"` → `"unknown"`
- [ ] `test_carrier_voicemail` — `"The person you are trying to reach is not available"` → `"machine"`
- [ ] `test_google_voice_greeting` — `"The Google subscriber you are trying to reach"` → `"machine"`

#### `TestPostScreeningVoicemailDetection`
- [ ] `test_screening_then_voicemail` — screening prompt → bot responds → voicemail greeting plays → detected as voicemail
- [ ] `test_screening_then_human` — screening prompt → bot responds → human picks up → detected as human
- [ ] `test_voicemail_after_screening_uses_greeting_classifier` — greeting text is classified even after screening flow

#### `TestEnhancedVoicemailIntegration`
- [ ] `test_stt_classification_supplements_amd` — when AMD says unknown, STT classifier provides the answer
- [ ] `test_stt_classification_agrees_with_amd` — when both agree on "machine", single VoicemailDetected emitted
- [ ] `test_stt_classification_disagrees_with_amd` — when AMD says human but greeting says machine, configurable which wins
- [ ] `test_short_greeting_classified_by_stt` — greeting <3s (too short for monologue detector) classified by text content

---

## Phase 6: End-to-End Integration

### `tests/telephony/test_outbound_integration.py`

#### `TestOutboundCallFullFlow`
- [ ] `test_outbound_to_human` — place call → ringing → answered → AMD=human → normal conversation
- [ ] `test_outbound_to_voicemail_hangup` — place call → answered → AMD=machine → policy hangup
- [ ] `test_outbound_to_voicemail_leave_message` — place call → answered → AMD=machine_end_beep → policy leave message
- [ ] `test_outbound_to_ios_screening_then_human` — place call → answered → screening detected → bot identifies → human picks up → conversation
- [ ] `test_outbound_to_ios_screening_then_voicemail` — place call → screening → bot identifies → voicemail
- [ ] `test_outbound_to_android_screening` — place call → Android screening → bot identifies → outcome
- [ ] `test_outbound_to_ivr_single_level` — place call → IVR prompt → agent navigates → reaches human
- [ ] `test_outbound_to_ivr_multi_level` — place call → IVR level 1 → level 2 → reaches human
- [ ] `test_outbound_busy` — place call → busy → CallFailed emitted
- [ ] `test_outbound_no_answer` — place call → timeout → CallFailed emitted
- [ ] `test_all_helpers_coexist` — DTMF aggregator + voicemail detector + screening detector + state machine all active simultaneously without interference

#### `TestScreeningEdgeCases`
- [ ] `test_carrier_screening_then_ios_screening` — two screening layers in sequence both detected
- [ ] `test_screening_response_within_time_window` — bot responds within 5s of screening detection
- [ ] `test_screening_with_agent_response` — agent-generated screening response is spoken via TTS
- [ ] `test_screening_agent_timeout_fallback` — agent too slow → static response used instead

#### `TestExistingTestsUnbroken`
- [ ] `test_existing_dtmf_tests_pass` — all `test_dtmf.py` tests still pass (regression)
- [ ] `test_existing_voicemail_tests_pass` — all `test_voicemail.py` tests still pass (regression)
- [ ] `test_existing_twiml_tests_pass` — all `test_twiml.py` tests still pass (regression)
- [ ] `test_existing_integration_tests_pass` — all `test_integration.py` tests still pass (regression)

---

## Verification

After each phase, run:
```bash
uv run pytest tests/telephony/ -v            # All telephony tests
uv run ruff check . && uv run ruff format .  # Lint + format
uv run pytest                                # Full suite (no regressions)
```
