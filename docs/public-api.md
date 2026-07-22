# Public API Contract

This page defines what `from easycat import ...` means for application code.
The exact top-level allowlist is also pinned in `tests/test_public_api.py`, so
changes to this page and the snapshot should be reviewed together.
This page is also listed in the maintained docs map; run `uv run easycat docs`
to confirm it remains discoverable. Maintainers can use
`uv run easycat docs --audience maintainers` for the focused route set
(`uv run easycat docs --json` and
`uv run easycat docs --audience maintainers --json` emit route entries and
command hints). Coding agent? Use the root [AGENTS.md](../AGENTS.md) for
repository coding rules; use [llms.txt](../llms.txt) for machine-readable docs
route discovery or run `uv run easycat explain json-schema`.

## Rules

- The app-first entry point is `from easycat import VoiceApp`: one noun for a
  voice product that runs across `local`/`browser`/`websocket`/`twilio` modes.
- The first-run path is `EasyConfig` plus `run`.
- Long-running applications should use `create_session` plus
  `easycat.helpers.run_session`, or `create_text_session`.
- Advanced users who own concrete providers should prefer
  `Session.from_providers(...)`. They may also import `SessionConfig`,
  provider protocols, transport configs, core events, and debug bundle helpers
  from the top-level package when those names are listed below.
- Provider implementations, bridge implementations, telephony internals, stage
  internals, testing helpers, and recipes stay in their own submodules.
- Top-level exports load lazily. Adding a top-level name must not make
  `import easycat` import provider SDKs, transport SDKs, or telephony runtime
  modules.
- Any top-level addition or removal must update this page and the
  `PUBLIC_API_SNAPSHOT` in `tests/test_public_api.py`.
- Reader-facing snippets in the root README, maintained docs, examples, and
  scaffold templates must use this allowlist when they write
  `from easycat import ...`; import implementation-specific names from their
  submodules instead.
- The `Top-Level Allowlist` bullets below must exactly match `easycat.__all__`;
  CI parses this section rather than accepting incidental mentions elsewhere.
- After changing top-level exports, run
  `just guard-docs` before opening the PR. It includes
  `uv run pytest tests/test_public_api.py`. If `just` is not installed, use the
  raw command table in
  [`CONTRIBUTING.md`](../CONTRIBUTING.md#the-development-loop), or run
  `uv run pytest tests/test_quickstart_e2e.py tests/test_command_hints.py tests/install/test_install_guidance.py tests/docs tests/test_public_api.py tests/test_llms_txt.py tests/test_regen_guard_commands.py tests/cli/test_app.py tests/cli/test_json_schema.py`.

## Preferred Imports

Use the smallest import that matches your use case:

```python
from easycat import EasyConfig, run
```

For long-running apps that need event subscriptions, debugger setup, or other
pre-start hooks, keep the same `EasyConfig` surface and run the prebuilt
session:

```python
from easycat import EasyConfig, STTFinal, create_session
from easycat.helpers import run_session


def main(agent) -> None:
    session = create_session(EasyConfig.mic(agent=agent))
    subscription = session.subscribe_event(STTFinal, lambda e: print("You said:", e.text))
    try:
        run_session(session)
    finally:
        subscription.unsubscribe()
```

Use submodules for rare or implementation-specific names:

```python
from easycat.integrations.agents import OpenAIAgentsBridge
from easycat.telephony.ivr import IVRNavigator
```

For raw provider instances, prefer the named constructor over the low-level
config object:

```python
from easycat import Session


session = Session.from_providers(
    stt=my_stt,
    tts=my_tts,
    vad=my_vad,
    transport=my_transport,
    agent=my_agent,
)
```

## Transport Extension Surface

Out-of-tree transports build on a small public surface re-exported from
`easycat.transports` (not from the top level, so `import easycat` stays
cheap). This surface is pinned by `tests/test_public_api.py` alongside the
top-level allowlist:

- `AudioQueueMixin` — inbound audio queue, `receive_audio()` iterator, and
  `TransportDegraded` emission plumbing for any custom transport.
- `ServerTransportBase` — `AudioQueueMixin` plus a managed `websockets`
  server lifecycle for server-style transports.
- `TransportDegraded` — the event those base classes emit on the session
  bus when frames are dropped or a peer tears down abnormally.

```python
from easycat.transports import AudioQueueMixin, ServerTransportBase, TransportDegraded
```

See the [extending guides](extending/) for complete custom provider and
transport walkthroughs, and `examples/custom_transport.py` for a runnable
custom transport.

## Top-Level Allowlist

### App Construction

- `VoiceApp`
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
- `register_stt_provider`
- `register_tts_provider`

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
- `SupervisorListenerAttached`
- `SupervisorListenerDetached`
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
- `run_webrtc_config_server`
- `serve_webrtc_config_sessions`
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

## Deprecation & Removal Policy

Stable symbols normally carry a machine-visible deprecation signal before
removal. During the pre-release period, obsolete APIs may be removed directly
when retaining them would preserve ownership ambiguity.
