# WS6: Telephony Features (DTMF + Voicemail Detection)

**Feature:** #8 (Telephony MVP: Twilio + DTMF + Voicemail)
**Depends on:** WS1 (event model for dtmf/voicemail events)
**Parallel with:** WS2, WS3, WS4, WS5, WS7, WS8
**Integration point:** WS5 provides the Twilio Media Streams transport; WS6 builds telephony-specific features on top. These can develop in parallel — WS6 can mock the transport layer.

## Goal

Implement DTMF input/output, digit aggregation, and voicemail/answering machine detection. These are telephony-specific features that layer on top of the transport and session model.

## Deliverables

### DTMF Input

- Parse DTMF events from Twilio Media Streams WebSocket `dtmf` messages
- Emit `dtmf(digit)` events into the session
- Optional fallback: TwiML `<Gather>` for non-stream / legacy call flows

### DTMF Output

- Send tones via TwiML `<Play digits="...">`
- Optional: `<Dial><Number sendDigits="...">` for dialing extensions / IVR sequences

### DTMF Aggregator

- Collect digit sequences with configurable:
  - Timeout (e.g., 2s idle between digits)
  - Terminators (`#`, `*`)
  - Max length
- Emit `dtmf.aggregated(sequence)` event suitable for agent tool use (e.g., "enter account number")

### Voicemail / Answering Machine Detection

#### Twilio AMD (Primary, Outbound Calls)

- Consume Twilio Answering Machine Detection results
- Map to `voicemail.detected(human|machine|unknown)` event

#### Heuristic Fallback (Any Audio)

- Detect voicemail based on:
  - "Greeting-like" long monologues without pauses
  - Beep detection (tone/energy heuristics)
- Emit `voicemail.detected(human|machine|unknown)`

#### Policy Actions

- Support configurable response to voicemail detection:
  - Hang up
  - Leave message
  - Transfer to agent tool

## Testing Strategy

- DTMF: unit tests with mock Twilio WebSocket messages containing DTMF digits
- DTMF aggregator: test timeout, terminator, and max-length scenarios
- Voicemail: test with recorded audio containing voicemail greetings and beeps
- Twilio AMD: test with mock AMD results

## Acceptance Criteria

- [ ] DTMF digits parsed from Twilio Media Streams messages
- [ ] `dtmf(digit)` events emitted correctly
- [ ] DTMF aggregator collects sequences with timeout, terminators, max length
- [ ] `dtmf.aggregated(sequence)` event emitted with complete digit string
- [ ] DTMF output sends tones via TwiML
- [ ] Twilio AMD results mapped to `voicemail.detected` events
- [ ] Heuristic voicemail detection identifies long monologues and beeps
- [ ] Policy actions (hang up, leave message, transfer) are configurable
