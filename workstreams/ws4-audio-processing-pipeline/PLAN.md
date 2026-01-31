# WS4: Audio Processing Pipeline — Task Plan

> **Depends on:** WS1 Tasks 1.1–1.3 (interfaces and audio types), Task 1.6 (chunk sizing).
> Noise reduction, VAD, and turn-taking are developed here together because they are tightly coupled in the pipeline.

## Phase 1: Noise Reduction

### Task 4.1: RNNoise integration (open-source fallback)
- Integrate RNNoise for noise suppression
- Implement `RNNoiseReducer(NoiseReducer)`: `process(chunk) -> chunk`
- **Sample rate:** RNNoise expects 48 kHz float32 input. Before implementing, confirm the exact framing and input requirements from the RNNoise documentation/source.
- Internal conversion pipeline: PCM16 (at pipeline rate, e.g., 16 kHz) → resample to 48 kHz → convert to float32 → RNNoise → convert back to PCM16 → resample to pipeline rate
- **Depends on:** WS1 Task 1.4 must support 16k↔48k resampling (not just 8k↔16k)
- Initialize and release RNNoise state properly
- Unit tests: process noisy audio, verify output is valid audio (basic SNR improvement check or just format correctness)
- Unit test: verify the resample round-trip preserves audio quality

### Task 4.2: Krisp noise reduction integration (commercial)
- Implement `KrispNoiseReducer(NoiseReducer)`
- Integrate with Krisp SDK (voice isolation / noise cancellation)
- Handle SDK initialization, licensing checks
- If Krisp is not configured or license is missing, raise a clear error (auto-fallback is handled at the factory level)
- Unit tests with mocked Krisp SDK

### Task 4.3: Noise reducer factory with auto-fallback
- Implement `create_noise_reducer(config) -> NoiseReducer`
- If Krisp is configured and available, use `KrispNoiseReducer`
- Otherwise, automatically fall back to `RNNoiseReducer`
- No application code changes needed for fallback — it's transparent
- Test: verify fallback triggers when Krisp is absent

## Phase 2: Voice Activity Detection

### Task 4.4: Silero VAD integration (open-source fallback)
- Implement `SileroVAD(VADProvider)`
- Load Silero VAD model (PyTorch / ONNX)
- Process audio chunks, emit `vad.start_speaking` and `vad.stop_speaking` events
- Configurable: min speech duration, min silence duration, sensitivity/threshold
- Handle pre-roll buffering: when speech starts, include N ms of audio before the trigger point
- Handle post-roll buffering: when speech stops, include N ms of audio after silence detected
- Unit tests: speech audio -> start event; silence -> stop event; short noise bursts -> no event (below min duration)

### Task 4.5: Krisp VAD integration (commercial)
- Implement `KrispVAD(VADProvider)`
- Integrate Krisp VIVA VAD SDK
- Same event interface and configuration as Silero
- Handle SDK initialization, licensing
- Unit tests with mocked Krisp SDK

### Task 4.6: VAD factory with auto-fallback
- Implement `create_vad(config) -> VADProvider`
- If Krisp is configured, use `KrispVAD`; otherwise fall back to `SileroVAD`
- Transparent to application code
- Test: verify fallback behavior

## Phase 3: Turn-Taking

### Task 4.7: Turn-taking state machine
- Implement `TurnManager` that consumes **both VAD events and raw `audio_in` frames** and manages turn state:
  - **Idle** — waiting for speech
  - **UserSpeaking** — VAD detected speech, capturing audio
  - **UserPaused** — silence detected, waiting for end-of-turn timeout
  - **Processing** — user turn complete, waiting for agent + TTS
  - **BotSpeaking** — TTS audio playing back
- **Audio frame consumption:** TurnManager receives raw audio frames (via subscription to `audio_in` or a direct feed) so it can:
  - Maintain a rolling pre-roll buffer (N ms of recent audio before VAD trigger)
  - Prepend pre-roll frames into the STT capture stream when speech starts
  - Without raw audio access, pre-roll buffering is impossible since VAD events alone don't carry the audio data
- Transitions:
  - `vad.start_speaking` -> Idle to UserSpeaking (emit `turn.started`, flush pre-roll buffer to STT)
  - `vad.stop_speaking` -> UserSpeaking to UserPaused
  - Silence timeout expires -> UserPaused to Processing (emit `turn.ended`; **Session** then calls `end_stream()` on STT, which produces `stt.final` via its event iterator)
  - Speech resumes before timeout -> UserPaused back to UserSpeaking
  - Agent + TTS complete -> Processing to BotSpeaking (emit `bot.started_speaking`)
  - TTS playback complete -> BotSpeaking to Idle (emit `bot.stopped_speaking`)
- **Responsibility boundary:** TurnManager emits `turn.ended`, not `stt.final`. STT providers produce their final transcript only after their `end_stream()` is called. This avoids blurring responsibility and prevents breakage of STT providers that only produce a final transcript after the API call completes (e.g., OpenAI STT).
- Configurable: end-of-turn silence timeout (e.g., 500ms–2000ms)

### Task 4.8: Push-to-talk / manual end-of-turn mode
- Alternative turn mode for testing and specific use cases
- `end_turn()` method to manually signal end of user turn (bypasses VAD timeout)
- Toggle between VAD mode and push-to-talk mode via session config
- Unit tests for manual turn ending

### Task 4.9: Barge-in / interruption handling
- If state is **BotSpeaking** and `vad.start_speaking` fires:
  - Trigger WS1's cancel token for the current turn (cancels TTS playback, agent streaming, and queued outbound audio)
  - Transition to **UserSpeaking** and begin capturing the new turn
- Emit `interruption` event (defined in WS1 event model) for observability (metrics can count interruptions)
- Emit `turn.started` for the new user turn
- Test: simulate bot speaking + user interrupt -> verify playback stops, agent streaming stops, new turn starts

## Phase 4: Integration

### Task 4.10: Pipeline integration test
- Wire noise reduction -> VAD -> turn-taking in sequence
- Feed recorded audio (speech + silence + noise) through the pipeline
- Verify: noise reduction runs first, VAD receives cleaned audio, turn boundaries are detected correctly
- Verify: barge-in scenario works end-to-end
