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

Less-common surfaces (stage internals, recorder types, bridge
boilerplate, telephony helpers) stay reachable from their submodules —
this module keeps the top-level namespace focused on what an
application author touches.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

_LAZY_ATTR: dict[str, tuple[str, str]] = {}


def _register(module: str, *names: str) -> None:
    for name in names:
        _LAZY_ATTR[name] = (module, name)


# ── Top-level factories and config ────────────────────────────────

_register(
    "easycat.config",
    "EasyCatConfig",
    "TelephonyConfig",
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
_register("easycat.quick", "speak", "transcribe_file")

# ── Core session + agent surface ──────────────────────────────────

_register("easycat.cancel", "CancelToken")
_register("easycat.session._session", "Session")
_register(
    "easycat.session._types",
    "Agent",
    "CallDirection",
    "CallIdentity",
    "CallerIdExposure",
    "SessionConfig",
    "TurnState",
)
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

# ── Agent framework bridges ───────────────────────────────────────

_register(
    "easycat.integrations.agents._agent_runner",
    "AgentRunner",
    "AgentRunnerConfig",
)
_register("easycat.integrations.agents._factory", "auto_adapt_agent")
_register(
    "easycat.integrations.agents.base",
    "AgentBridgeEvent",
    "AgentTurnInput",
    "ExternalAgentBridge",
)

# ── Speech pipeline knobs ─────────────────────────────────────────

_register(
    "easycat.smart_turn",
    "SmartTurnConfig",
    "SmartTurnONNX",
    "SmartTurnProvider",
    "SmartTurnResult",
    "create_smart_turn",
)
_register(
    "easycat.llm_output_processing",
    "LLMOutputProcessor",
    "PauseProcessor",
    "PhoneticReplacementProcessor",
    "default_pronunciation_processors",
)
_register(
    "easycat.timeouts",
    "AgentTimeoutError",
    "STTTimeoutError",
    "TimeoutConfig",
    "TTSTimeoutError",
)

# ── Debug / journal runtime ───────────────────────────────────────

_register(
    "easycat.runtime",
    "ExecutionJournal",
    "JournalRecord",
    "JournalRecordKind",
    "JournalView",
)
_register(
    "easycat.runtime.replay",
    "ReplayFidelity",
    "ReplaySpec",
    "ToolReplayPolicy",
)
_register("easycat.debug.bundle", "RunBundle")
_register("easycat.debug.export", "export_debug_bundle")
_register("easycat.debug.testing", "load_bundle")

# ── Errors ────────────────────────────────────────────────────────

_register("easycat.errors", "EasyCatError", "ErrorEntry")

# ── EasyCat-level events ──────────────────────────────────────────

_register(
    "easycat.events",
    "ALL_EVENTS",
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
    "CartesiaSTT",
    "CartesiaSTTConfig",
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
_register("easycat.tts.cartesia_tts", "CartesiaTTS", "CartesiaTTSConfig")
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
    from easycat.errors import EasyCatError, ErrorEntry
    from easycat.events import (
        ALL_EVENTS,
        DTMF,
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
    from easycat.integrations.agents._factory import auto_adapt_agent
    from easycat.integrations.agents.base import (
        AgentBridgeEvent,
        AgentTurnInput,
        ExternalAgentBridge,
    )
    from easycat.llm_output_processing import (
        LLMOutputProcessor,
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
    from easycat.runtime.replay import (
        ReplayFidelity,
        ReplaySpec,
        ToolReplayPolicy,
    )
    from easycat.session._session import Session
    from easycat.session._types import (
        Agent,
        CallDirection,
        CallerIdExposure,
        CallIdentity,
        SessionConfig,
        TurnState,
    )
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
    from easycat.stt import (
        CartesiaSTT,
        CartesiaSTTConfig,
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
    from easycat.tts.cartesia_tts import CartesiaTTS, CartesiaTTSConfig
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
