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
  voice product that runs across `local`/`browser`/`websocket`/`twilio`/
  `telnyx` modes.
- The first-run path is `VoiceApp(agent=...).run("local")`.
- `EasyConfig` plus `run` is the explicit-configuration path when an app needs
  provider, turn-taking, journal, or transport control. In applications that
  already own an asyncio event loop, use `await arun(...)` instead.
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
- `easycat.__version__` reports the installed distribution version for feature
  detection. As conventional package metadata, it is deliberately outside the
  app-facing `__all__` allowlist and loads lazily with the rest of this module.
- After changing top-level exports, run
  `just guard-docs` before opening the PR. It includes
  `uv run pytest tests/test_public_api.py`. If `just` is not installed, use the
  raw command table in
  [`CONTRIBUTING.md`](../CONTRIBUTING.md#the-development-loop), or run
  `uv run pytest tests/test_quickstart_e2e.py tests/install/test_install_guidance.py tests/docs tests/test_public_api.py tests/test_llms_txt.py tests/test_regen_guard_commands.py tests/cli/test_app.py tests/cli/test_json_schema.py tests/test_markdown_links.py`.

## Preferred Imports

Use the smallest import that matches your use case:

```python
from easycat import VoiceApp


VoiceApp(agent=agent).run("local")
```

When provider or runtime choices need to be explicit, graduate to the config
layer:

```python
from easycat import EasyConfig, run


run(EasyConfig.mic(agent=agent, stt="deepgram/flux", debug="full"))
```

The same convenience lifecycle is available without nesting an event loop:

```python
from easycat import EasyConfig, arun


async def main(agent) -> None:
    await arun(EasyConfig.mic(agent=agent))
```

Calling synchronous `run(...)` from an active event loop fails immediately
with an error that points to `await arun(...)`; no session is created first.

For long-running apps that need event subscriptions, debugger setup, or other
pre-start hooks, build and run the session directly:

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

The returned `EventSubscription` is the preferred unsubscribe handle: it
retains the exact event type and handler and its `unsubscribe()` method is
idempotent. `session.unsubscribe_event(event_type, handler)` remains available
for code that already manages those pairs directly.

Multi-session owners can inspect every best-effort teardown attempt instead of
inferring partial failure from logs:

```python
report = await manager.stop_all(force=True)
for failure in report.failures:
    logger.error("session %r did not stop: %s", failure.key, failure.exception)
```

`report.ok`, `attempted_keys`, `stopped_keys`, `failed_keys`, and `failures`
describe the sweep. Failed sessions remain registered so teardown can be
retried. Import `SessionStopReport` and `SessionStopFailure` from
`easycat.session_manager` when explicit annotations are useful.

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

## Provider Testing Extension Surface

Out-of-tree provider and bridge packages can subclass the offline contract
suites shipped from `easycat.testing`. This module is versioned as an extension
surface but is not re-exported at the top level, keeping `import easycat`
lightweight:

- `STTProviderContractSuite`
- `TTSProviderContractSuite`
- `VADProviderContractSuite`
- `TransportContractSuite`
- `AgentBridgeContractSuite`
- `ContractSuite`
- `ProviderContractSuite`
- `RecordingAgentRecorder`
- `AGENT_BRIDGE_EVENT_KINDS`
- `ProviderCapabilities`
- `ProviderCapabilityReport`
- `ProviderIdentifier`
- `contains_unredacted_sensitive_text`

```python
from easycat.testing import STTProviderContractSuite


class TestAcmeSTT(STTProviderContractSuite):
    provider_factory = AcmeSTT
```

The [extending guides](extending/) show the corresponding suite for each
provider surface and how to add optional live checks.

## Agent Bridge Extension Surface

Agent framework bridges are public from `easycat.integrations.agents`, not from
the top-level `easycat` package. Application compilers and bridge authors can
depend on this surface:

- `ExternalAgentBridge` — async protocol implemented by every bridge.
- `AgentTurnInput` — normalized user turn input passed into bridges.
- `AgentBridgeEvent` — normalized stream event yielded by bridges, including
  append-only text deltas and indexed text-part replacements.
- `AgentRecorder` — write-side journal protocol passed into bridge turns.
- `CancellationMode` — supported interruption and drain strategies.
- `FrameworkStateSnapshot` — JSON-safe framework state captured by bridges.
- `InterruptionPlan` — planned framework mutation used by interruption handling.
- `INTERRUPTION_NOTE` — standard history note used when a partial assistant
  response is interrupted.
- `BridgeTemplate` — starter base class for custom bridge authors; constructor
  `BridgeTemplate(*, display_name=None)`.
- `register_agent_detector` / `clear_agent_detectors` / `auto_adapt_agent` —
  registry hooks for adapting framework-native agent objects.
- `is_reusable_agent_spec` — reports whether an agent specification can be
  shared safely across sessions.
- `AgentRunner` / `AgentRunnerConfig` — wrapper for plain async `run(text)`
  agents; constructor `AgentRunner(agent, config=None)`.
- `OpenAIAgentsBridge` — constructor `OpenAIAgentsBridge(agent, *,
  run_config=None, context=None, use_previous_response_id=True, max_turns=None,
  hooks=None, mcp_servers=None)`.
- `PydanticAIBridge` — constructor `PydanticAIBridge(*, agent=None, deps=None,
  model_settings=None, graph=None, state_factory=None, initial_node_factory=None,
  agents=None, mcp_servers=None, toolsets=None)`.
- `RemoteResponsesAPIBridge` — constructor
  `RemoteResponsesAPIBridge(base_url, model, *, api_key=None, timeout=120.0,
  metadata=None)`.
- `GenericWorkflowBridge` — constructor
  `GenericWorkflowBridge(workflow, *, display_name=None)`.
- `LangChainBridge` — constructor `LangChainBridge(runnable, *,
  display_name=None, input_key="input", history_key="history",
  messages_input=False, include_types=..., session_id=None, config=None)`.
- `LangGraphBridge` — constructor `LangGraphBridge(graph, *, thread_id=None,
  messages_key="messages", display_name=None, include_types=...)`.
- `LlamaAgentsBridge` — constructor `LlamaAgentsBridge(workflow=None, *,
  client=None, base_url=None, workflow_name=None, input_key="message",
  context_key="context", turn_id_key="turn_id",
  interruption_note_key="easycat_interruption_note", preserve_context=True,
  run_kwargs=None, start_event_factory=None, event_text_extractor=None,
  human_response_event_factory=None, human_response_key="response",
  human_response_step=None, display_name=None, include_internal_events=False)`.

```python
from easycat.integrations.agents import PydanticAIBridge
```

## Top-Level Allowlist

### App Construction

- `VoiceApp`
- `EasyConfig`
- `create_session`
- `create_text_session`
- `run`
- `arun`
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
- `EventBusBindable`
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
- `available_vad_providers`
- `create_noise_reducer`
- `create_stt_provider`
- `create_tts_provider`
- `create_vad`
- `register_stt_provider`
- `register_tts_provider`
- `register_vad_provider`

### Events

- `AgentDelta`
- `AgentFinal`
- `AgentRequestStarted`
- `AudioIn`
- `AudioOut`
- `BotStartedSpeaking`
- `BotStoppedSpeaking`
- `CallAnswered`
- `CallEnded`
- `CallFailed`
- `CallInitiated`
- `CallRinging`
- `CallScreening`
- `CallStateChanged`
- `DTMF`
- `DTMFAggregated`
- `Error`
- `ErrorStage`
- `Event`
- `EventBus`
- `IVRAction`
- `Interruption`
- `PlaybackMarkAck`
- `ReconnectAttempt`
- `ReconnectFailure`
- `ReconnectSuccess`
- `ScreeningResponse`
- `ScreeningTimedOut`
- `SessionActionCompleted`
- `SessionActionFailed`
- `SessionActionRequested`
- `SessionActionStarted`
- `STTFinal`
- `STTPartial`
- `SupervisorListenerAttached`
- `SupervisorListenerDetached`
- `TTSAudio`
- `TTSMarkers`
- `ToolCallDelta`
- `ToolCallResult`
- `ToolCallStarted`
- `TransportAudioDelivered`
- `TransportDegraded`
- `TurnEnded`
- `TurnStarted`
- `VADStartSpeaking`
- `VADStopSpeaking`
- `VoicemailDetected`

### Audio And Transports

- `AudioChunk`
- `AudioFormat`
- `PCM16_MONO_8K`
- `PCM16_MONO_16K`
- `PCM16_MONO_24K`
- `PCM16_MONO_48K`
- `ICEServer`
- `LocalTransportConfig`
- `TelnyxConnectionTransport`
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

`WebSocketConnectionTransport.request`, `TwilioConnectionTransport.request`,
and `TelnyxConnectionTransport.request` expose the accepted `websockets`
handshake request when the server provides one. `WebRTCTransport.offer_request`
exposes the accepted aiohttp offer request when the transport is created by
the mounted WebRTC route, so session factories can derive per-connection
configuration from headers or URL query parameters.

`AuthResult` intentionally carries only an authorization verdict, not an
application principal. Applications that need a typed tenant or caller identity
should use the same verifier in the server auth policy and in the session
factory, reading the accepted request from the transport. See
[Binding a typed principal](deployment/production-servers.md#binding-a-typed-principal)
for the complete composition.

### Output Processing

- `MarkdownStripProcessor`
- `PauseProcessor`
- `PhoneticReplacementProcessor`
- `default_pronunciation_processors`

### Debugging, Journals, And Errors

- `EasyCatError`
- `EasyConfigError`
- `ErrorEntry`
- `JournalRecordKind`
- `RunBundle`
- `export_debug_bundle`

`EasyConfigError` is also a `ValueError`; catch `EasyCatError` to handle both
configuration and coded provider/construction failures.

## Deprecation & Removal Policy

Stable symbols normally carry a machine-visible deprecation signal before
removal. During the pre-release period, obsolete APIs may be removed directly
when retaining them would preserve ownership ambiguity.

Starting with version 1.0.0, EasyCat follows Semantic Versioning: incompatible
changes to this documented public API require a major release,
backward-compatible additions require a minor release, and
backward-compatible fixes require a patch release.
