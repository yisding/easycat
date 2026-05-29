"""EasyCat — a voice bot in three lines.

Start here (requires ``uv add 'easycat[quickstart]'``)::

    from agents import Agent
    from easycat import EasyConfig, run
    run(EasyConfig.mic(agent=Agent(name="assistant", instructions="Be helpful.")))

``EasyConfig`` + ``run`` is the entry path. Drop to ``SessionConfig`` + ``Session``
only when you need to hand-build provider instances.

The top-level package intentionally exposes the app-facing surface only;
providers, stage internals, and telephony/debug helpers stay importable
from their own modules. Exports load lazily via PEP 562 so cold starts stay
cheap.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

_LAZY_ATTR: dict[str, tuple[str, str]] = {}


def _register(module: str, *names: str) -> None:
    for name in names:
        _LAZY_ATTR[name] = (module, name)


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
    "attach_runtime_feedback",
    "require_env",
    "run",
    "wait_for_shutdown_signal",
)

# Session and advanced app construction.
_register("easycat.cancel", "CancelToken")
_register("easycat.session._session", "Session")
_register("easycat.session._types", "CallIdentity", "SessionConfig")
_register("easycat.session.actions", "SessionActions")
_register("easycat.session_manager", "SessionManager")
_register("easycat.supervisor", "SessionAudioBroadcaster")
_register("easycat.turn_manager", "TurnManagerConfig", "TurnMode")
_register("easycat.integrations.agents", "auto_adapt_agent")

# Pluggable audio backends — pass these into EasyConfig to pin a
# specific VAD / noise-reduction implementation.
_register("easycat.vad", "VADConfig", "create_vad")
_register("easycat.noise_reduction", "NoiseReducerConfig", "create_noise_reducer")

# Provider factory functions and their explicit config types.
_register("easycat.stt.factory", "STTProviderConfig", "create_stt_provider")
_register("easycat.tts.factory", "TTSProviderConfig", "create_tts_provider")
# Discovery helpers — enumerate valid ``stt=`` / ``tts=`` string names.
_register("easycat.stt.factory", "available_stt_providers")
_register("easycat.tts.factory", "available_tts_providers")

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
_register("easycat.errors", "EasyCatError", "ErrorEntry")

# Core events.
_register(
    "easycat.events",
    "AgentDelta",
    "AgentFinal",
    "AudioIn",
    "AudioOut",
    "BotStartedSpeaking",
    "BotStoppedSpeaking",
    "CallAnswered",
    "CallEnded",
    "CallFailed",
    "Error",
    "ErrorStage",
    "Event",
    "EventBus",
    "Interruption",
    "STTFinal",
    "STTPartial",
    "TTSAudio",
    "TTSMarkers",
    "TurnEnded",
    "TurnStarted",
    "VADStartSpeaking",
    "VADStopSpeaking",
)

# Stable provider protocols.
_register(
    "easycat.providers",
    "EchoCanceller",
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
_register("easycat.transports.twilio_media", "TwilioConnectionTransport")
_register("easycat.telephony.session_actions", "TwilioSessionActionConfig")
_register("easycat.transports.webrtc", "ICEServer", "WebRTCTransportConfig")
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


if TYPE_CHECKING:
    from easycat.audio_format import (
        PCM16_MONO_8K,
        PCM16_MONO_16K,
        PCM16_MONO_24K,
        PCM16_MONO_48K,
        AudioChunk,
        AudioFormat,
    )
    from easycat.cancel import CancelToken
    from easycat.config import (
        EasyConfig,
        OutboundCallConfig,
        TelephonyConfig,
        VoicemailDetectionConfig,
        create_session,
        create_text_session,
    )
    from easycat.debug.bundle import RunBundle
    from easycat.debug.export import export_debug_bundle
    from easycat.errors import EasyCatError, ErrorEntry
    from easycat.events import (
        AgentDelta,
        AgentFinal,
        AudioIn,
        AudioOut,
        BotStartedSpeaking,
        BotStoppedSpeaking,
        CallAnswered,
        CallEnded,
        CallFailed,
        Error,
        ErrorStage,
        Event,
        EventBus,
        Interruption,
        STTFinal,
        STTPartial,
        TTSAudio,
        TTSMarkers,
        TurnEnded,
        TurnStarted,
        VADStartSpeaking,
        VADStopSpeaking,
    )
    from easycat.helpers import (
        attach_runtime_feedback,
        require_env,
        run,
        wait_for_shutdown_signal,
    )
    from easycat.integrations.agents import auto_adapt_agent
    from easycat.llm_output_processing import (
        MarkdownStripProcessor,
        PauseProcessor,
        PhoneticReplacementProcessor,
        default_pronunciation_processors,
    )
    from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer
    from easycat.providers import (
        EchoCanceller,
        NoiseReducer,
        STTProvider,
        Transport,
        TTSProvider,
        VADProvider,
    )
    from easycat.runtime import JournalRecordKind
    from easycat.session._session import Session
    from easycat.session._types import CallIdentity, SessionConfig
    from easycat.session.actions import SessionActions
    from easycat.session_manager import SessionManager
    from easycat.smart_turn import SmartTurnConfig
    from easycat.stt.factory import (
        STTProviderConfig,
        available_stt_providers,
        create_stt_provider,
    )
    from easycat.supervisor import SessionAudioBroadcaster
    from easycat.telephony.session_actions import TwilioSessionActionConfig
    from easycat.transports.local import LocalTransportConfig
    from easycat.transports.twilio_media import TwilioConnectionTransport
    from easycat.transports.webrtc import ICEServer, WebRTCTransportConfig
    from easycat.transports.websocket import (
        WebSocketConnectionTransport,
        WebSocketTransportConfig,
    )
    from easycat.transports.webtransport import (
        WebTransportConnectionTransport,
        WebTransportServer,
        WebTransportTransportConfig,
    )
    from easycat.tts.factory import (
        TTSProviderConfig,
        available_tts_providers,
        create_tts_provider,
    )
    from easycat.turn_manager import TurnManagerConfig, TurnMode
    from easycat.vad import VADConfig


def __getattr__(name: str):  # PEP 562
    """Lazy re-export dispatcher. Runs once per attribute per session."""
    try:
        module_path, attr = _LAZY_ATTR[name]
    except KeyError:
        raise AttributeError(f"module 'easycat' has no attribute {name!r}") from None
    module = importlib.import_module(module_path)
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_LAZY_ATTR)))


__all__ = sorted(_LAZY_ATTR)
