"""EasyCat -- slim, batteries-included voice bot framework.

Public API
----------
This module exports the symbols intended for typical library consumers.
Top-level ``from easycat import X`` keeps working; every symbol in
:data:`__all__` is reachable.

Internally, symbols are loaded lazily via PEP 562 ``__getattr__`` to
keep CLI cold-start (``easycat --version``, ``easycat --help``) within
a 300ms budget.  Heavy provider modules (transports, stages, telephony)
only import when the symbol is actually touched.

Internal plumbing remains importable from submodules for advanced use::

    from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState
    from easycat.bounded_queue import BoundedAudioQueue, DropPolicy
    from easycat.reconnecting_ws import ReconnectingWebSocket, ReconnectConfig
    from easycat.health_check import PeriodicHealthChecker, HealthCheckable
    from easycat.timeouts import with_stt_timeout, with_agent_timeout, with_tts_timeout
    from easycat.audio_utils import chunk_frames, resample, to_mono, ...
    from easycat.audio_utils import pcm_to_wav
    from easycat.stt.base import STTBase
    from easycat.tts.base import TTSBase
    from easycat.stt import create_stt_provider
    from easycat.tts.factory import create_tts_provider, TTSProviderConfig
    from easycat.tts.elevenlabs_tts import ElevenLabsStreamMode
    from easycat.events import STTEvent, STTEventType, TTSEvent, TTSEventType, WordTimestamp
    from easycat.telephony import DTMFAggregator, VoicemailDetector, ...
    from easycat.transports.twilio_media import mulaw_to_pcm16, pcm16_to_mulaw, ...
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

# Mapping of public-name → (submodule-path, attribute-name).  Every
# entry in ``__all__`` MUST appear here.  ``_alias`` lets us rename
# ``REGISTRY`` → ``ERROR_REGISTRY`` and similar.
_LAZY_ATTR: dict[str, tuple[str, str]] = {}


def _register(module: str, *names: str, _alias: dict[str, str] | None = None) -> None:
    for name in names:
        _LAZY_ATTR[name] = (module, name)
    if _alias:
        for public, attr in _alias.items():
            _LAZY_ATTR[public] = (module, attr)


# ── Core session & agent ──────────────────────────────────────────

_register(
    "easycat.integrations.agents._agent_runner",
    "AgentRunner",
    "AgentRunnerConfig",
)
_register(
    "easycat.integrations.agents._base_adapter",
    "BaseAgentAdapter",
    "serialize_output",
)
_register("easycat.integrations.agents._factory", "auto_adapt_agent")
_register(
    "easycat.integrations.agents._stream_types",
    "AgentStreamEvent",
    "AgentStreamEventType",
    "StreamingAgent",
)
_register("easycat.cancel", "CancelToken")
_register(
    "easycat.smart_turn",
    "SmartTurnConfig",
    "SmartTurnONNX",
    "SmartTurnProvider",
    "SmartTurnResult",
    "create_smart_turn",
)
_register("easycat.session.action_executors", "CoreSessionActionExecutor")
_register("easycat.session._session", "Session")
_register("easycat.session._types", "SessionConfig", "TurnState")
_register(
    "easycat.session.actions",
    "CustomAction",
    "EndCallAction",
    "SendDTMFAction",
    "SendSMSAction",
    "SessionAction",
    "SessionActionExecutor",
    "SessionActionResult",
    "SessionActions",
    "SessionActionType",
    "TransferCallAction",
    "TransferPlan",
)
_register("easycat.session_manager", "SessionManager")
_register("easycat.supervisor", "SessionAudioBroadcaster", "SupervisorAudioFrame")
_register(
    "easycat.turn_manager",
    "TurnManager",
    "TurnManagerConfig",
    "TurnManagerState",
    "TurnMode",
)
_register("easycat.session._types", "Agent")
_register("easycat.quick", "speak", "transcribe_file")
_register(
    "easycat.llm_output_processing",
    "LLMOutputProcessor",
    "MarkdownStripProcessor",
    "PauseProcessor",
    "PhoneticReplacementProcessor",
    "default_pronunciation_processors",
)
_register(
    "easycat.config",
    "EasyCatConfig",
    "TelephonyConfig",
    "create_session",
    "create_text_session",
)
_register(
    "easycat.runtime",
    "ExecutionJournal",
    "JournalRecord",
    "JournalRecordKind",
    "JournalView",
)
_register(
    "easycat.errors",
    "EasyCatError",
    "ErrorEntry",
    _alias={"ERROR_REGISTRY": "REGISTRY"},
)
_register(
    "easycat.helpers",
    "attach_runtime_feedback",
    "require_env",
    "run",
    "wait_for_shutdown_signal",
)

# ── Debug-first runtime ─────────────────────────────────────────

_register("easycat.debug.bundle", "RunBundle")
_register("easycat.debug.export", "export_debug_bundle")
_register("easycat.debug.testing", "load_bundle")
_register("easycat.integrations.agents.base", "ExternalAgentBridge")
_register("easycat.stages.base", "Stage")

# ── EasyCat-level events ─────────────────────────────────────────

_register(
    "easycat.events",
    "ACTION_EVENTS",
    "AGENT_EVENTS",
    "ALL_EVENTS",
    "AUDIO_EVENTS",
    "ERROR_EVENTS",
    "INTERRUPTION_EVENTS",
    "LIFECYCLE_EVENTS",
    "RECONNECT_EVENTS",
    "STT_EVENTS",
    "TELEPHONY_EVENTS",
    "TOOL_EVENTS",
    "TTS_EVENTS",
    "VAD_EVENTS",
    "AgentDelta",
    "AgentFinal",
    "AgentRequestStarted",
    "AudioIn",
    "AudioOut",
    "BotStartedSpeaking",
    "BotStoppedSpeaking",
    "DTMF",
    "DTMFAggregated",
    "Error",
    "ErrorStage",
    "Event",
    "EventBus",
    "Interruption",
    "ReconnectAttempt",
    "ReconnectFailure",
    "ReconnectSuccess",
    "STTFinal",
    "STTPartial",
    "SessionActionCompleted",
    "SessionActionFailed",
    "SessionActionRequested",
    "SessionActionStarted",
    "ToolCallDelta",
    "ToolCallResult",
    "ToolCallStarted",
    "TTSAudio",
    "TTSMarkers",
    "TurnEnded",
    "TurnStarted",
    "VADStartSpeaking",
    "VADStopSpeaking",
    "VoicemailDetected",
)

# ── Provider protocols ────────────────────────────────────────────

_register(
    "easycat.providers",
    "EchoCanceller",
    "NoiseReducer",
    "STTProvider",
    "Transport",
    "TTSProvider",
    "VADProvider",
)

# ── Audio format ──────────────────────────────────────────────────

_register(
    "easycat.audio_format",
    "PCM16_MONO_8K",
    "PCM16_MONO_16K",
    "PCM16_MONO_24K",
    "PCM16_MONO_48K",
    "AudioChunk",
    "AudioFormat",
)

# ── Provider implementations ─────────────────────────────────────

_register(
    "easycat.echo_cancellation",
    "EchoCancellationConfig",
    "LiveKitAEC",
    "PassthroughAEC",
    "create_echo_canceller",
)
_register(
    "easycat.noise_reduction",
    "KrispNoiseReducer",
    "NoiseReducerConfig",
    "PassthroughNoiseReducer",
    "RNNoiseReducer",
    "create_noise_reducer",
)
_register(
    "easycat.stt",
    "DeepgramSTT",
    "DeepgramSTTConfig",
    "ElevenLabsSTT",
    "ElevenLabsSTTConfig",
    "OpenAIRealtimeSTT",
    "OpenAIRealtimeSTTConfig",
    "OpenAISTT",
    "OpenAISTTConfig",
)
_register(
    "easycat.stt.factory",
    "STTProviderConfig",
    "create_stt_provider",
    "parse_stt_string",
)
_register(
    "easycat.tts.factory",
    "TTSProviderConfig",
    "create_tts_provider",
    "parse_tts_string",
)
_register("easycat.tts.deepgram_tts", "DeepgramTTS", "DeepgramTTSConfig")
_register("easycat.tts.elevenlabs_tts", "ElevenLabsTTS", "ElevenLabsTTSConfig")
_register("easycat.tts.openai_tts", "OpenAITTS", "OpenAITTSConfig")
_register("easycat.tts.input", "TTSInput", "TTSInputFormat")
_register(
    "easycat.vad",
    "FunASROnnxVAD",
    "KrispVAD",
    "SileroVAD",
    "TenVAD",
    "VADConfig",
    "create_vad",
)

# ── Transport implementations ────────────────────────────────────

_register("easycat.transports.local", "LocalTransport", "LocalTransportConfig")
_register(
    "easycat.transports.twilio_media",
    "TwilioConnectionTransport",
    "TwilioTransport",
    "TwilioTransportConfig",
)
_register(
    "easycat.telephony.session_actions",
    "TwilioSessionActionConfig",
    "TwilioSessionActionExecutor",
)
_register(
    "easycat.transports.webrtc",
    "ICEServer",
    "WebRTCTransport",
    "WebRTCTransportConfig",
)
_register(
    "easycat.transports.websocket",
    "WebSocketConnectionTransport",
    "WebSocketTransport",
    "WebSocketTransportConfig",
)

# ── Configuration & errors ────────────────────────────────────────

_register(
    "easycat.timeouts",
    "AgentTimeoutError",
    "STTTimeoutError",
    "TimeoutConfig",
    "TTSTimeoutError",
)


if TYPE_CHECKING:
    # Static-analysis view of every lazy export.  None of these imports
    # run at runtime — ``__getattr__`` handles those.
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
        EasyCatConfig,
        TelephonyConfig,
        create_session,
        create_text_session,
    )
    from easycat.debug.bundle import RunBundle
    from easycat.debug.export import export_debug_bundle
    from easycat.debug.testing import load_bundle
    from easycat.echo_cancellation import (
        EchoCancellationConfig,
        LiveKitAEC,
        PassthroughAEC,
        create_echo_canceller,
    )
    from easycat.errors import (
        REGISTRY as ERROR_REGISTRY,
    )
    from easycat.errors import (
        EasyCatError,
        ErrorEntry,
    )
    from easycat.events import (
        ACTION_EVENTS,
        AGENT_EVENTS,
        ALL_EVENTS,
        AUDIO_EVENTS,
        DTMF,
        ERROR_EVENTS,
        INTERRUPTION_EVENTS,
        LIFECYCLE_EVENTS,
        RECONNECT_EVENTS,
        STT_EVENTS,
        TELEPHONY_EVENTS,
        TOOL_EVENTS,
        TTS_EVENTS,
        VAD_EVENTS,
        AgentDelta,
        AgentFinal,
        AgentRequestStarted,
        AudioIn,
        AudioOut,
        BotStartedSpeaking,
        BotStoppedSpeaking,
        DTMFAggregated,
        Error,
        ErrorStage,
        Event,
        EventBus,
        Interruption,
        ReconnectAttempt,
        ReconnectFailure,
        ReconnectSuccess,
        SessionActionCompleted,
        SessionActionFailed,
        SessionActionRequested,
        SessionActionStarted,
        STTFinal,
        STTPartial,
        ToolCallDelta,
        ToolCallResult,
        ToolCallStarted,
        TTSAudio,
        TTSMarkers,
        TurnEnded,
        TurnStarted,
        VADStartSpeaking,
        VADStopSpeaking,
        VoicemailDetected,
    )
    from easycat.helpers import (
        attach_runtime_feedback,
        require_env,
        run,
        wait_for_shutdown_signal,
    )
    from easycat.integrations.agents._agent_runner import (
        AgentRunner,
        AgentRunnerConfig,
    )
    from easycat.integrations.agents._base_adapter import (
        BaseAgentAdapter,
        serialize_output,
    )
    from easycat.integrations.agents._factory import auto_adapt_agent
    from easycat.integrations.agents._stream_types import (
        AgentStreamEvent,
        AgentStreamEventType,
        StreamingAgent,
    )
    from easycat.integrations.agents.base import ExternalAgentBridge
    from easycat.llm_output_processing import (
        LLMOutputProcessor,
        MarkdownStripProcessor,
        PauseProcessor,
        PhoneticReplacementProcessor,
        default_pronunciation_processors,
    )
    from easycat.noise_reduction import (
        KrispNoiseReducer,
        NoiseReducerConfig,
        PassthroughNoiseReducer,
        RNNoiseReducer,
        create_noise_reducer,
    )
    from easycat.providers import (
        EchoCanceller,
        NoiseReducer,
        STTProvider,
        Transport,
        TTSProvider,
        VADProvider,
    )
    from easycat.quick import speak, transcribe_file
    from easycat.runtime import (
        ExecutionJournal,
        JournalRecord,
        JournalRecordKind,
        JournalView,
    )
    from easycat.session._session import Session
    from easycat.session._types import Agent, SessionConfig, TurnState
    from easycat.session.action_executors import CoreSessionActionExecutor
    from easycat.session.actions import (
        CustomAction,
        EndCallAction,
        SendDTMFAction,
        SendSMSAction,
        SessionAction,
        SessionActionExecutor,
        SessionActionResult,
        SessionActions,
        SessionActionType,
        TransferCallAction,
        TransferPlan,
    )
    from easycat.session_manager import SessionManager
    from easycat.smart_turn import (
        SmartTurnConfig,
        SmartTurnONNX,
        SmartTurnProvider,
        SmartTurnResult,
        create_smart_turn,
    )
    from easycat.stages.base import Stage
    from easycat.stt import (
        DeepgramSTT,
        DeepgramSTTConfig,
        ElevenLabsSTT,
        ElevenLabsSTTConfig,
        OpenAIRealtimeSTT,
        OpenAIRealtimeSTTConfig,
        OpenAISTT,
        OpenAISTTConfig,
    )
    from easycat.stt.factory import (
        STTProviderConfig,
        create_stt_provider,
        parse_stt_string,
    )
    from easycat.supervisor import SessionAudioBroadcaster, SupervisorAudioFrame
    from easycat.telephony.session_actions import (
        TwilioSessionActionConfig,
        TwilioSessionActionExecutor,
    )
    from easycat.timeouts import (
        AgentTimeoutError,
        STTTimeoutError,
        TimeoutConfig,
        TTSTimeoutError,
    )
    from easycat.transports.local import LocalTransport, LocalTransportConfig
    from easycat.transports.twilio_media import (
        TwilioConnectionTransport,
        TwilioTransport,
        TwilioTransportConfig,
    )
    from easycat.transports.webrtc import (
        ICEServer,
        WebRTCTransport,
        WebRTCTransportConfig,
    )
    from easycat.transports.websocket import (
        WebSocketConnectionTransport,
        WebSocketTransport,
        WebSocketTransportConfig,
    )
    from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
    from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig
    from easycat.tts.factory import (
        TTSProviderConfig,
        create_tts_provider,
        parse_tts_string,
    )
    from easycat.tts.input import TTSInput, TTSInputFormat
    from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig
    from easycat.turn_manager import (
        TurnManager,
        TurnManagerConfig,
        TurnManagerState,
        TurnMode,
    )
    from easycat.vad import FunASROnnxVAD, KrispVAD, SileroVAD, TenVAD, VADConfig, create_vad


def __getattr__(name: str):  # PEP 562
    """Lazy re-export dispatcher.  Runs once per attribute per session."""
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
