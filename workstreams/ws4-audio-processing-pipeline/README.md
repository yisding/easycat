# WS4: Audio Processing Pipeline (VAD + Noise Reduction + Turn-Taking)

**Features:** #5 (VAD), #6 (Noise Reduction), #7 (Turn-Taking + Barge-In)
**Depends on:** WS1 (VADProvider/NoiseReducer interfaces, audio format utilities)
**Parallel with:** WS2, WS3, WS5, WS6, WS7, WS8

## Goal

Implement the audio processing stages that sit between raw audio input and STT: noise reduction, voice activity detection, and turn-taking logic. These are tightly coupled in the pipeline and share configuration concerns, so they belong in one workstream.

## Deliverables

### Noise Reduction

#### Krisp Integration (Commercial/Primary)

- Integrate Krisp noise cancellation / voice isolation SDK
- Process audio chunks through Krisp before VAD and STT
- Handle SDK initialization, licensing

#### RNNoise Fallback (Open Source)

- Integrate RNNoise for noise suppression
- Local processing, no external dependencies
- Auto-fallback: if Krisp is not configured/licensed, use RNNoise transparently
- **Sample rate:** RNNoise expects 48 kHz float32 input. WS1's resampler must support 16k↔48k conversion. Confirm the exact framing and input requirements before implementing.

#### Configuration

- Noise reduction runs before VAD and STT by default
- Make pipeline placement configurable

### Voice Activity Detection

#### Krisp VAD (Commercial/Primary)

- Integrate Krisp VIVA VAD as the premium/default option
- Low false-positive rate in noisy environments

#### Silero VAD Fallback (Open Source)

- Integrate Silero VAD (local, PyTorch-based)
- Auto-fallback: if Krisp is not configured, use Silero transparently

#### VAD Configuration

- Min speech duration
- Min silence duration
- Sensitivity / threshold
- Pre-roll / post-roll buffering (avoid clipping)

### Turn-Taking

#### VAD-Based Turn Management

- Turn start: triggered by `vad.start_speaking`; emit `turn.started`
- End-of-turn: silence-based timeout (configurable); emit `turn.ended` (Session then calls `end_stream()` on STT)
- Optional push-to-talk / manual end-of-turn mode for testing
- **Pre-roll audio:** TurnManager must consume raw `audio_in` frames (not just VAD events) so it can buffer and prepend N ms of pre-roll audio into the STT capture buffer when speech starts
- **Responsibility boundary:** TurnManager emits `turn.ended`, not `stt.final`. The Session handles calling `end_stream()` on the STT provider, which then produces its final transcript via the `events()` iterator.

#### Barge-In / Interruption

- If bot is speaking and VAD detects user speech:
  - Immediately stop local playback / outbound audio stream
  - Cancel current TTS request and ongoing agent streaming (via WS1 cancel token)
  - Begin next user turn capture
- Emit `interruption` event for observability + `turn.started` for the new turn

## Testing Strategy

- Unit tests for each VAD and noise reduction implementation with recorded audio
- Test turn-taking state machine transitions
- Test barge-in scenario: bot speaking + user interrupts -> playback stops, new turn starts
- Test auto-fallback: Krisp not configured -> Silero/RNNoise used automatically

## Acceptance Criteria

- [ ] Krisp noise reduction processes audio chunks (when configured)
- [ ] RNNoise fallback works when Krisp is absent
- [ ] Krisp VAD detects speech start/stop with configurable thresholds
- [ ] Silero VAD fallback works when Krisp is absent
- [ ] Turn-taking correctly identifies turn boundaries from VAD events
- [ ] Barge-in stops playback and cancels TTS when user interrupts
- [ ] Pre-roll buffering captures audio before VAD triggers (TurnManager sees raw audio frames)
- [ ] TurnManager emits `turn.ended` (not `stt.final`) — Session handles STT finalization
- [ ] Push-to-talk mode works for testing scenarios
