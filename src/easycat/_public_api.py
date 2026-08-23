"""Data-only registry for EasyCat's top-level lazy exports."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

LazyExportMap = Mapping[str, tuple[str, str]]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {}


def _register(module: str, *names: str) -> None:
    for name in names:
        if name in _LAZY_EXPORTS:
            raise RuntimeError(f"duplicate easycat public API export: {name}")
        _LAZY_EXPORTS[name] = (module, name)


# Core factories, config, and runtime helpers.
_register(
    "easycat.config",
    "EasyConfig",
    "OutboundCallConfig",
    "TelephonyConfig",
    "VoicemailDetectionConfig",
    "create_session",
    "create_text_session",
)
_register(
    "easycat.helpers",
    "arun",
    "attach_runtime_feedback",
    "require_env",
    "run",
    "wait_for_shutdown_signal",
)
_register("easycat._logging", "set_easycat_log_level")

# Session and advanced app construction.
_register("easycat.cancel", "CancelToken")
_register("easycat.session._session", "Session")
_register("easycat.session._types", "CallIdentity", "SessionConfig")
_register("easycat.session.actions", "SessionActions")
_register("easycat.session_manager", "SessionManager")
_register("easycat.voice_app", "VoiceApp")
_register("easycat.supervisor", "SessionAudioBroadcaster")
_register("easycat.turn_manager", "TurnManagerConfig", "TurnMode")
_register("easycat.integrations.agents", "auto_adapt_agent")

# Pluggable audio backends.
_register(
    "easycat.vad",
    "VADConfig",
    "available_vad_providers",
    "create_vad",
    "register_vad_provider",
)
_register("easycat.noise_reduction", "NoiseReducerConfig", "create_noise_reducer")

# Provider factory functions and explicit config types.
_register("easycat.stt.factory", "STTProviderConfig", "create_stt_provider")
_register("easycat.tts.factory", "TTSProviderConfig", "create_tts_provider")
_register("easycat.stt.factory", "available_stt_providers", "register_stt_provider")
_register("easycat.tts.factory", "available_tts_providers", "register_tts_provider")

# Speech and output-processing knobs commonly used by applications.
_register(
    "easycat.llm_output_processing",
    "MarkdownStripProcessor",
    "PauseProcessor",
    "PhoneticReplacementProcessor",
    "default_pronunciation_processors",
)
_register("easycat.smart_turn", "SmartTurnConfig")

# Public debug and journal inspection.
_register("easycat.runtime", "JournalRecordKind")
_register("easycat.debug.bundle", "RunBundle")
_register("easycat.debug.export", "export_debug_bundle")

# Errors.
_register("easycat.errors", "EasyCatError", "EasyConfigError", "ErrorEntry")

# Core events.
_register(
    "easycat.events",
    "AgentDelta",
    "AgentFinal",
    "AgentRequestStarted",
    "AudioIn",
    "AudioOut",
    "BotStartedSpeaking",
    "BotStoppedSpeaking",
    "CallAnswered",
    "CallEnded",
    "CallFailed",
    "CallInitiated",
    "CallRinging",
    "CallScreening",
    "CallStateChanged",
    "DTMF",
    "DTMFAggregated",
    "Error",
    "ErrorStage",
    "Event",
    "EventBus",
    "IVRAction",
    "Interruption",
    "PlaybackMarkAck",
    "ReconnectAttempt",
    "ReconnectFailure",
    "ReconnectSuccess",
    "ScreeningResponse",
    "ScreeningTimedOut",
    "SessionActionCompleted",
    "SessionActionFailed",
    "SessionActionRequested",
    "SessionActionStarted",
    "STTFinal",
    "STTPartial",
    "SupervisorListenerAttached",
    "SupervisorListenerDetached",
    "TTSAudio",
    "TTSMarkers",
    "ToolCallDelta",
    "ToolCallResult",
    "ToolCallStarted",
    "TransportAudioDelivered",
    "TransportDegraded",
    "TurnEnded",
    "TurnStarted",
    "VADStartSpeaking",
    "VADStopSpeaking",
    "VoicemailDetected",
)

# Stable provider protocols.
_register(
    "easycat.providers",
    "EchoCanceller",
    "EventBusBindable",
    "NoiseReducer",
    "STTProvider",
    "Transport",
    "TTSProvider",
    "VADProvider",
)

# Audio format values used when configuring transports/providers.
_register(
    "easycat.audio_format",
    "PCM16_MONO_8K",
    "PCM16_MONO_16K",
    "PCM16_MONO_24K",
    "PCM16_MONO_48K",
    "AudioChunk",
    "AudioFormat",
)

# Transport config and endpoint types used by README/examples.
_register("easycat.transports.local", "LocalTransportConfig")
_register("easycat.transports.telnyx_media", "TelnyxConnectionTransport")
_register("easycat.transports.twilio_media", "TwilioConnectionTransport")
_register("easycat.telephony.session_actions", "TwilioSessionActionConfig")
_register("easycat.transports._webrtc_config", "ICEServer", "WebRTCTransportConfig")
_register(
    "easycat.server.webrtc_routes",
    "run_webrtc_config_server",
    "serve_webrtc_config_sessions",
)
_register(
    "easycat.transports.websocket",
    "WebSocketConnectionTransport",
    "WebSocketTransportConfig",
)
_register(
    "easycat.transports.webtransport",
    "WebTransportConnectionTransport",
    "WebTransportServer",
    "WebTransportTransportConfig",
)

LAZY_EXPORTS: LazyExportMap = MappingProxyType(_LAZY_EXPORTS)

# Explicit classification of the dataclass configuration surface in
# ``LAZY_EXPORTS``. Keep this independent from name filtering: ``ICEServer`` is
# configuration despite its name, and a future non-config export ending in
# ``Config`` must not silently join security-sensitive repr coverage.
PUBLIC_CONFIG_EXPORTS: frozenset[str] = frozenset(
    {
        "EasyConfig",
        "ICEServer",
        "LocalTransportConfig",
        "NoiseReducerConfig",
        "OutboundCallConfig",
        "STTProviderConfig",
        "SessionConfig",
        "SmartTurnConfig",
        "TTSProviderConfig",
        "TelephonyConfig",
        "TurnManagerConfig",
        "TwilioSessionActionConfig",
        "VADConfig",
        "VoicemailDetectionConfig",
        "WebRTCTransportConfig",
        "WebSocketTransportConfig",
        "WebTransportTransportConfig",
    }
)

__all__ = ["LAZY_EXPORTS", "PUBLIC_CONFIG_EXPORTS", "LazyExportMap"]
