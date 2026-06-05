# Public API Contract

This page defines what `from easycat import ...` means for application code.
The exact top-level allowlist is also pinned in `tests/test_public_api.py`, so
changes to this page and the snapshot should be reviewed together.

## Rules

- The first-run path is `EasyConfig` plus `run`.
- Long-running applications should use `create_session` or
  `create_text_session`.
- Advanced users may import `Session`, `SessionConfig`, provider protocols,
  transport configs, core events, and debug bundle helpers from the top-level
  package when those names are listed below.
- Provider implementations, bridge implementations, telephony internals, stage
  internals, testing helpers, and recipes stay in their own submodules.
- Top-level exports load lazily. Adding a top-level name must not make
  `import easycat` import provider SDKs, transport SDKs, or telephony runtime
  modules.
- Any top-level addition or removal must update this page and the
  `PUBLIC_API_SNAPSHOT` in `tests/test_public_api.py`.

## Preferred Imports

Use the smallest import that matches your use case:

```python
from easycat import EasyConfig, run
```

```python
from easycat import EasyConfig, STTFinal, create_session
```

Use submodules for rare or implementation-specific names:

```python
from easycat.integrations.agents import OpenAIAgentsBridge
from easycat.telephony.ivr import IVRNavigator
```

## Top-Level Allowlist

### App Construction

- `EasyConfig`
- `create_session`
- `create_text_session`
- `run`
- `Session`
- `SessionConfig`
- `SessionActions`
- `SessionManager`
- `SessionAudioBroadcaster`
- `CallIdentity`
- `CancelToken`
- `auto_adapt_agent`

### Configuration Helpers

- `OutboundCallConfig`
- `TelephonyConfig`
- `VoicemailDetectionConfig`
- `SmartTurnConfig`
- `TurnManagerConfig`
- `TurnMode`
- `attach_runtime_feedback`
- `require_env`
- `set_easycat_log_level`
- `wait_for_shutdown_signal`

### Provider Protocols And Factories

- `EchoCanceller`
- `NoiseReducer`
- `STTProvider`
- `Transport`
- `TTSProvider`
- `VADProvider`
- `NoiseReducerConfig`
- `STTProviderConfig`
- `TTSProviderConfig`
- `VADConfig`
- `available_stt_providers`
- `available_tts_providers`
- `create_noise_reducer`
- `create_stt_provider`
- `create_tts_provider`
- `create_vad`

### Events

- `AgentDelta`
- `AgentFinal`
- `AudioIn`
- `AudioOut`
- `BotStartedSpeaking`
- `BotStoppedSpeaking`
- `CallAnswered`
- `CallEnded`
- `CallFailed`
- `Error`
- `ErrorStage`
- `Event`
- `EventBus`
- `Interruption`
- `STTFinal`
- `STTPartial`
- `TTSAudio`
- `TTSMarkers`
- `TurnEnded`
- `TurnStarted`
- `VADStartSpeaking`
- `VADStopSpeaking`

### Audio And Transports

- `AudioChunk`
- `AudioFormat`
- `PCM16_MONO_8K`
- `PCM16_MONO_16K`
- `PCM16_MONO_24K`
- `PCM16_MONO_48K`
- `ICEServer`
- `LocalTransportConfig`
- `TwilioConnectionTransport`
- `TwilioSessionActionConfig`
- `WebRTCTransportConfig`
- `WebSocketConnectionTransport`
- `WebSocketTransportConfig`
- `WebTransportConnectionTransport`
- `WebTransportServer`
- `WebTransportTransportConfig`

### Output Processing

- `MarkdownStripProcessor`
- `PauseProcessor`
- `PhoneticReplacementProcessor`
- `default_pronunciation_processors`

### Debugging, Journals, And Errors

- `EasyCatError`
- `ErrorEntry`
- `JournalRecordKind`
- `RunBundle`
- `export_debug_bundle`
