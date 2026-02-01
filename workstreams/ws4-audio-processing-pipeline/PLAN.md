# WS4: Audio Processing Pipeline — Task Plan

> **Depends on:** WS1 Tasks 1.1–1.3 (interfaces and audio types), Task 1.6 (chunk sizing).
> Noise reduction, VAD, and turn-taking are developed here together because they are tightly coupled in the pipeline.

## Phase 1: Noise Reduction

### Task 4.1: RNNoise integration (open-source fallback) ✅
- [x] Integrate RNNoise for noise suppression
- [x] Implement `RNNoiseReducer(NoiseReducer)`: `process(chunk) -> chunk`
- [x] **Sample rate:** RNNoise expects 48 kHz float32 input — confirmed 480-sample frames (10 ms at 48 kHz)
- [x] Internal conversion pipeline: PCM16 (at pipeline rate, e.g., 16 kHz) → resample to 48 kHz → convert to float32 → RNNoise → convert back to PCM16 → resample to pipeline rate
- [x] **Depends on:** WS1 Task 1.4 supports 16k↔48k resampling ✅
- [x] Initialize and release RNNoise state properly (via `rnnoise_create`/`rnnoise_destroy` with ctypes)
- [x] Unit tests: mocked RNNoise C library, format correctness verification
- [x] Unit test: verify the resample round-trip preserves audio quality
- **Implementation:** `src/easycat/noise_reduction.py` — `RNNoiseReducer` class
- **Tests:** `tests/audio/test_noise_reduction.py` — `test_rnnoise_*`, `test_resample_roundtrip_quality`

### Task 4.2: Krisp noise reduction integration (commercial) ✅
- [x] Implement `KrispNoiseReducer(NoiseReducer)`
- [x] Integrate with Krisp SDK (voice isolation / noise cancellation)
- [x] Handle SDK initialization, licensing checks
- [x] If Krisp is not configured or license is missing, raise a clear error (auto-fallback is handled at the factory level)
- [x] Unit tests with mocked Krisp SDK
- **Implementation:** `src/easycat/noise_reduction.py` — `KrispNoiseReducer` class
- **Tests:** `tests/audio/test_noise_reduction.py` — `test_krisp_*`

### Task 4.3: Noise reducer factory with auto-fallback ✅
- [x] Implement `create_noise_reducer(config) -> NoiseReducer`
- [x] If Krisp is configured and available, use `KrispNoiseReducer`
- [x] Otherwise, automatically fall back to `RNNoiseReducer`
- [x] If neither is available, fall back to `PassthroughNoiseReducer` (no-op)
- [x] No application code changes needed for fallback — it's transparent
- [x] Test: verify fallback triggers when Krisp is absent
- **Implementation:** `src/easycat/noise_reduction.py` — `create_noise_reducer()`, `NoiseReducerConfig`, `PassthroughNoiseReducer`
- **Tests:** `tests/audio/test_noise_reduction.py` — `test_factory_*`

## Phase 2: Voice Activity Detection

### Task 4.4: Silero VAD integration (open-source fallback) ✅
- [x] Implement `SileroVAD(VADProvider)`
- [x] Load Silero VAD model (PyTorch via `torch.hub.load`)
- [x] Process audio chunks, emit `VADStartSpeaking` and `VADStopSpeaking` events
- [x] Configurable: min speech duration, min silence duration, sensitivity/threshold
- [x] Resamples to 16 kHz internally (Silero expects 16 kHz, 512-sample frames)
- [x] Accumulation buffer for sub-frame chunks
- [x] Unit tests: speech audio -> start event; short noise bursts -> no event (below min duration)
- **Implementation:** `src/easycat/vad.py` — `SileroVAD` class
- **Tests:** `tests/vad/test_vad.py` — `test_silero_*`

### Task 4.5: Krisp VAD integration (commercial) ✅
- [x] Implement `KrispVAD(VADProvider)`
- [x] Integrate Krisp VIVA VAD SDK
- [x] Same event interface and configuration as Silero
- [x] Handle SDK initialization, licensing
- [x] Unit tests with mocked Krisp SDK
- **Implementation:** `src/easycat/vad.py` — `KrispVAD` class
- **Tests:** `tests/vad/test_vad.py` — `test_krisp_vad_*`

### Task 4.6: VAD factory with auto-fallback ✅
- [x] Implement `create_vad(config) -> VADProvider`
- [x] If Krisp is configured, use `KrispVAD`; otherwise fall back to `SileroVAD`
- [x] Transparent to application code
- [x] Factory applies configuration (thresholds, sensitivity) to created VAD
- [x] Test: verify fallback behavior
- **Implementation:** `src/easycat/vad.py` — `create_vad()`, `VADConfig`
- **Tests:** `tests/vad/test_vad.py` — `test_vad_factory_*`

## Phase 3: Turn-Taking

### Task 4.7: Turn-taking state machine ✅
- [x] Implement `TurnManager` that consumes **both VAD events and raw `audio_in` frames** and manages turn state:
  - **Idle** — waiting for speech
  - **UserSpeaking** — VAD detected speech, capturing audio
  - **UserPaused** — silence detected, waiting for end-of-turn timeout
  - **Processing** — user turn complete, waiting for agent + TTS
  - **BotSpeaking** — TTS audio playing back
- [x] **Audio frame consumption:** TurnManager receives raw audio frames via `on_audio_frame()` so it can:
  - Maintain a rolling pre-roll buffer (N ms of recent audio before VAD trigger)
  - Prepend pre-roll frames into the STT capture stream when speech starts
  - Without raw audio access, pre-roll buffering is impossible since VAD events alone don't carry the audio data
- [x] Transitions:
  - `vad.start_speaking` -> Idle to UserSpeaking (emit `turn.started`, flush pre-roll buffer to STT)
  - `vad.stop_speaking` -> UserSpeaking to UserPaused
  - Silence timeout expires -> UserPaused to Processing (emit `turn.ended`; **Session** then calls `end_stream()` on STT)
  - Speech resumes before timeout -> UserPaused back to UserSpeaking
  - Agent + TTS complete -> Processing to BotSpeaking (emit `bot.started_speaking`)
  - TTS playback complete -> BotSpeaking to Idle (emit `bot.stopped_speaking`)
- [x] **Responsibility boundary:** TurnManager emits `turn.ended`, not `stt.final`. STT providers produce their final transcript only after their `end_stream()` is called.
- [x] Configurable: end-of-turn silence timeout (default 1000ms)
- **Implementation:** `src/easycat/turn_manager.py` — `TurnManager`, `TurnManagerState`, `TurnManagerConfig`
- **Tests:** `tests/turns/test_turn_manager.py` — state machine transitions, pre-roll buffer, silence timeout

### Task 4.8: Push-to-talk / manual end-of-turn mode ✅
- [x] Alternative turn mode for testing and specific use cases
- [x] `start_turn()` and `end_turn()` methods to manually control turns (bypasses VAD timeout)
- [x] Toggle between VAD mode and push-to-talk mode via `set_mode()` or `TurnManagerConfig.mode`
- [x] In push-to-talk mode, VAD events are ignored
- [x] Unit tests for manual turn ending
- **Implementation:** `src/easycat/turn_manager.py` — `TurnMode.PUSH_TO_TALK`, `start_turn()`, `end_turn()`
- **Tests:** `tests/turns/test_turn_manager.py` — `test_push_to_talk_*`, `test_mode_switching`

### Task 4.9: Barge-in / interruption handling ✅
- [x] If state is **BotSpeaking** and `vad.start_speaking` fires:
  - Trigger cancel callback (cancels TTS playback, agent streaming, and queued outbound audio)
  - Transition to **UserSpeaking** and begin capturing the new turn
- [x] Emit `Interruption` event for observability (metrics can count interruptions)
- [x] Emit `TurnStarted` for the new user turn
- [x] Pre-roll buffer flushed into new turn audio on barge-in
- [x] Test: simulate bot speaking + user interrupt -> verify cancel callback called, new turn starts
- **Implementation:** `src/easycat/turn_manager.py` — `_handle_barge_in()`, `cancel_turn_callback`
- **Tests:** `tests/turns/test_turn_manager.py` — `test_barge_in_*`

## Phase 4: Integration

### Task 4.10: Pipeline integration test ✅
- [x] Wire noise reduction -> VAD -> turn-taking in sequence
- [x] Feed audio through the pipeline and verify ordering: NR first, then VAD, then turn-taking
- [x] Verify: turn boundaries are detected correctly (TurnStarted + TurnEnded in order)
- [x] Verify: barge-in scenario works end-to-end (cancel + new turn)
- [x] Verify: pre-roll buffering preserved through pipeline
- [x] Verify: push-to-talk mode works through pipeline
- [x] Verify: multiple consecutive turns handled correctly
- [x] Verify: passthrough noise reducer (auto fallback) does not affect VAD
- **Tests:** `tests/turns/test_turn_pipeline.py` — 7 integration tests

---

## Summary

**Status: COMPLETE** ✅

All 10 tasks across 4 phases implemented and tested.

### Files Created
| File | Description |
|------|-------------|
| `src/easycat/noise_reduction.py` | RNNoiseReducer, KrispNoiseReducer, PassthroughNoiseReducer, factory |
| `src/easycat/vad.py` | SileroVAD, KrispVAD, factory |
| `src/easycat/turn_manager.py` | TurnManager state machine, push-to-talk, barge-in |
| `tests/audio/test_noise_reduction.py` | 16 tests for noise reduction |
| `tests/vad/test_vad.py` | 12 tests for VAD |
| `tests/turns/test_turn_manager.py` | 21 tests for turn-taking |
| `tests/turns/test_turn_pipeline.py` | 7 integration tests |

### Files Modified
| File | Change |
|------|--------|
| `src/easycat/__init__.py` | Added WS4 exports (noise reduction, VAD, turn manager) |

### Test Results
- **149 tests total**, all passing
- **56 new WS4 tests** added
- **Lint:** all checks passed (ruff)

### Public API Additions
- `RNNoiseReducer`, `KrispNoiseReducer`, `PassthroughNoiseReducer`, `NoiseReducerConfig`, `create_noise_reducer`
- `SileroVAD`, `KrispVAD`, `VADConfig`, `create_vad`
- `TurnManager`, `TurnManagerConfig`, `TurnManagerState`, `TurnMode`
