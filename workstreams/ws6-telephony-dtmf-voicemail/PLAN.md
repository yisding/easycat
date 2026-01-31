# WS6: Telephony Features (DTMF + Voicemail) — Task Plan

> **Depends on:** WS1 Tasks 1.1–1.2 (event model for dtmf/voicemail events).
> **Integration point:** WS5 provides the Twilio transport, but WS6 can develop against mocked transport messages.
> DTMF and Voicemail detection are independent and can be built in parallel.

## Phase 1: DTMF Input

### Task 6.1: DTMF event consumer for Twilio Media Streams ✅
- **Handoff from WS5:** `TwilioTransport` (WS5) parses Twilio `dtmf` WebSocket messages and emits `dtmf(digit)` events into the Session event bus. WS6 subscribes to these events for aggregation and processing.
- If WS5 is not yet available, implement parsing against mocked Twilio WebSocket messages for standalone development
- Unit tests with sample Twilio DTMF WebSocket messages
- **Impl:** `src/easycat/telephony/dtmf.py` — `parse_twilio_dtmf_message()`, `emit_twilio_dtmf()`
- **Tests:** `tests/test_dtmf.py` — `TestParseTwilioDtmfMessage` (12 tests), `TestEmitTwilioDtmf` (2 tests)

### Task 6.2: TwiML Gather fallback for DTMF input ✅
- Support `<Gather>` digit collection for non-stream / legacy call flows
- Parse Gather webhook callback with collected digits
- Emit equivalent `dtmf(digit)` events
- Unit tests with mock webhook payloads
- **Impl:** `src/easycat/telephony/twiml.py` — `parse_gather_webhook()`, `emit_gather_digits()`, `twiml_gather()`
- **Tests:** `tests/test_twiml.py` — `TestParseGatherWebhook` (9 tests), `TestEmitGatherDigits` (1 test), `TestTwimlGather` (5 tests)

## Phase 2: DTMF Aggregation

### Task 6.3: DTMF aggregator ✅
- Implement `DTMFAggregator` that collects individual digit events into sequences
- Configurable parameters:
  - `timeout_ms` — idle time before emitting (e.g., 2000ms)
  - `terminators` — characters that trigger immediate emit (e.g., `#`, `*`)
  - `max_length` — maximum digit count before auto-emit
- On trigger (timeout, terminator, or max length), emit `dtmf.aggregated(sequence)` event
- Reset state after each emit
- Unit tests:
  - Digits followed by timeout -> aggregated event
  - Digits followed by `#` -> immediate aggregated event
  - Max length reached -> immediate aggregated event
  - No digits -> no event
- **Impl:** `src/easycat/telephony/dtmf.py` — `DTMFAggregator`, `DTMFAggregatorConfig`
- **Tests:** `tests/test_dtmf.py` — `TestDTMFAggregator` (8 tests)

## Phase 3: DTMF Output

### Task 6.4: DTMF tone output via TwiML ✅
- Helper to generate `<Play digits="...">` TwiML for DTMF tone playback
- Helper to generate `<Dial><Number sendDigits="...">` for IVR/extension dialing
- These return TwiML fragments that the telephony layer can inject
- Unit tests verifying generated TwiML is correct
- **Impl:** `src/easycat/telephony/twiml.py` — `twiml_play_digits()`, `twiml_dial_send_digits()`
- **Tests:** `tests/test_twiml.py` — `TestTwimlPlayDigits` (4 tests), `TestTwimlDialSendDigits` (4 tests), `TestTwimlHangup` (1 test)

## Phase 4: Voicemail / Answering Machine Detection

### Task 6.5: Twilio AMD result consumer ✅
- **AMD delivery mechanism:** Twilio AMD results arrive via HTTP status callbacks, not the Media Streams WebSocket. Implement a framework-agnostic HTTP callback handler function:
  - Accepts the Twilio webhook payload (POST form data)
  - Parses the `AnsweredBy` field
  - Emits `voicemail.detected` into the Session event bus
- This handler can be mounted alongside WS5's Twilio WebSocket server, or in any user-provided web framework (FastAPI, Flask, etc.)
- Map Twilio's result (`human`, `machine_start`, `machine_end_beep`, `machine_end_silence`, `machine_end_other`, `fax`) to `voicemail.detected(human|machine|unknown)`
- Unit tests with each AMD result type (mock HTTP payloads)
- **Impl:** `src/easycat/telephony/voicemail.py` — `parse_twilio_amd_webhook()`, `emit_twilio_amd()`
- **Tests:** `tests/test_voicemail.py` — `TestParseTwilioAmdWebhook` (12 tests), `TestEmitTwilioAmd` (2 tests)

### Task 6.6: Heuristic voicemail detection (audio-based) ✅
- Implement `VoicemailDetector` that analyzes audio for voicemail patterns:
  - **Long monologue detection:** continuous speech without pauses exceeding a threshold (e.g., >8 seconds of uninterrupted speech suggests a recorded greeting)
  - **Beep detection:** detect a single sustained tone (simple energy + zero-crossing frequency heuristic) indicating the end of a voicemail greeting
- Uses VAD events from the session to track speech/silence patterns
- Emit `voicemail.detected(human|machine|unknown)` when a determination is made
- Config: monologue threshold, beep frequency range, beep min duration
- Unit tests with generated voicemail tones and simulated VAD events
- **Impl:** `src/easycat/telephony/voicemail.py` — `VoicemailDetector`, `VoicemailDetectorConfig`, `BeepDetectorConfig`
- **Tests:** `tests/test_voicemail.py` — `TestVoicemailDetectorMonologue` (4 tests), `TestVoicemailDetectorBeep` (4 tests)

### Task 6.7: Voicemail policy handler ✅
- On `voicemail.detected`, apply a configurable policy with concrete mechanisms:
  - **hang_up** — end the call via TwiML `<Hangup>` or Twilio REST API (`calls/{sid}/update` with `status=completed`)
  - **leave_message** — wait for beep detection (from Task 6.6) to confirm the voicemail recording has started, then trigger agent/TTS to speak a pre-configured message, then hang up. Requires coordination with TTS playback and beep detection finalization.
  - **transfer** — invoke an agent tool to transfer the call (e.g., generate TwiML `<Dial>` or use Twilio REST API)
- Policy is set in session config
- Unit tests for each policy action (mock Twilio API calls, verify TwiML generation, verify TTS trigger)
- **Impl:** `src/easycat/telephony/voicemail.py` — `VoicemailPolicyHandler`, `VoicemailPolicyConfig`, `VoicemailPolicy`
- **Tests:** `tests/test_voicemail.py` — `TestVoicemailPolicyHandler` (6 tests)

## Phase 5: Integration

### Task 6.8: End-to-end telephony scenario tests ✅
- Test full DTMF flow: Twilio sends digit messages -> parsed -> aggregated -> event emitted
- Test full voicemail flow: outbound call -> AMD detects machine -> policy executes
- Test combined: call connects -> DTMF prompt -> user enters digits -> agent processes
- These can use mocked Twilio messages (no live Twilio needed)
- **Tests:** `tests/test_telephony_integration.py` — 5 test classes, 11 end-to-end scenarios

## Summary

| Task | Status | Tests |
|------|--------|-------|
| 6.1 DTMF consumer | ✅ Done | 14 |
| 6.2 Gather fallback | ✅ Done | 15 |
| 6.3 DTMF aggregator | ✅ Done | 8 |
| 6.4 DTMF output | ✅ Done | 9 |
| 6.5 AMD consumer | ✅ Done | 14 |
| 6.6 Heuristic detection | ✅ Done | 8 |
| 6.7 Policy handler | ✅ Done | 6 |
| 6.8 Integration tests | ✅ Done | 11 |
| **Total** | **8/8** | **88** |
