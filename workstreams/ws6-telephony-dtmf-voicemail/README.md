# WS6: Telephony Features (DTMF + Voicemail Detection)

**Feature:** #8 (Telephony MVP: Twilio + DTMF + Voicemail)
**Depends on:** WS1 (event model for dtmf/voicemail events)
**Parallel with:** WS2, WS3, WS4, WS5, WS7, WS8
**Integration point:** WS5 provides the Twilio Media Streams transport; WS6 builds telephony-specific features on top. These can develop in parallel — WS6 can mock the transport layer.

## Goal

Implement DTMF input/output, digit aggregation, and voicemail/answering machine detection. These are telephony-specific features that layer on top of the transport and session model.

## Deliverables

### DTMF Input

- Consume DTMF events emitted by `TwilioTransport` (WS5) into the Session event bus — WS5 handles parsing Twilio WebSocket `dtmf` messages and emitting them; WS6 subscribes to those events
- Emit `dtmf(digit)` events into the session (or consume them from WS5's emission)
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
- **Note:** AMD results are typically delivered via Twilio HTTP status callbacks, not the Media Streams WebSocket. Define a framework-agnostic HTTP callback handler (a function that accepts the webhook payload and emits the event) — this can live alongside WS5's Twilio WebSocket server or be mounted in the user's web framework.
- Map to `voicemail.detected(human|machine|unknown)` event

#### Heuristic Fallback (Any Audio)

- Detect voicemail based on:
  - "Greeting-like" long monologues without pauses
  - Beep detection (tone/energy heuristics)
- Emit `voicemail.detected(human|machine|unknown)`

#### Policy Actions

- Support configurable response to voicemail detection with concrete mechanisms:
  - **Hang up** — return TwiML `<Hangup>` or call the Twilio REST API (`calls/{sid}/update` with `status=completed`) to end the call
  - **Leave message** — coordinate with TTS to speak a message after beep detection signals the recording prompt; requires: wait for `voicemail.detected(machine)` → wait for beep → trigger agent/TTS to speak the message → hang up
  - **Transfer** — invoke an agent tool to transfer the call (e.g., via TwiML `<Dial>` or Twilio REST API)

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
