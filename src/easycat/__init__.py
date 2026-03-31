"""EasyCat — slim, batteries-included voice bot framework.

Public API
----------
This module exports the symbols intended for typical library consumers.
Internal plumbing remains importable from submodules for advanced use::

    from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState
    from easycat.bounded_queue import BoundedAudioQueue, DropPolicy
    from easycat.reconnecting_ws import ReconnectingWebSocket, ReconnectConfig
    from easycat.health_check import PeriodicHealthChecker, HealthCheckable
    from easycat.tracing import Span, SpanStatus, TraceContext, InMemoryTraceExporter
    from easycat.metrics import timed_metric, measure_latency, STT_LATENCY, ...
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

# ── Core session & agent ──────────────────────────────────────────

from easycat.agent_runner import (  # noqa: I001
    AgentRunner,
    AgentRunnerConfig,
    AgentStreamEvent,
    AgentStreamEventType,
    StreamingAgent,
)
from easycat.agents.base import BaseAgentAdapter, serialize_output
from easycat.agents.openai_agents import OpenAIAgentsAdapter, build_openai_agents_adapter
from easycat.agents.pydantic_ai import PydanticAIAdapter
from easycat.cancel import CancelToken
from easycat.smart_turn import (
    SmartTurnConfig,
    SmartTurnONNX,
    SmartTurnProvider,
    SmartTurnResult,
    create_smart_turn,
)
from easycat.session._session import Session
from easycat.session._types import SessionConfig, TurnState
from easycat.session_manager import SessionManager
from easycat.turn_manager import TurnMode
from easycat.llm_output_processing import (
    LLMOutputProcessor,
    MarkdownStripProcessor,
    PauseProcessor,
    PhoneticReplacementProcessor,
    default_pronunciation_processors,
)
from easycat.config import (
    EasyCatConfig,
    EventLoggingConfig,
    MetricsConfig,
    TelephonyConfig,
    TracingConfig,
    create_session,
)
from easycat.helpers import (
    attach_runtime_feedback,
    default_event_logging,
    require_env,
    wait_for_shutdown_signal,
)

# ── EasyCat-level events ─────────────────────────────────────────

from easycat.events import (
    AGENT_EVENTS,
    ALL_EVENTS,
    AUDIO_EVENTS,
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
    AudioIn,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    DTMF,
    DTMFAggregated,
    Error,
    Event,
    EventBus,
    Interruption,
    ReconnectAttempt,
    ReconnectFailure,
    ReconnectSuccess,
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

# ── Provider protocols ────────────────────────────────────────────

from easycat.providers import (
    EchoCanceller,
    NoiseReducer,
    STTProvider,
    Transport,
    TTSProvider,
    VADProvider,
)

# ── Audio format ──────────────────────────────────────────────────

from easycat.audio_format import (
    PCM16_MONO_8K,
    PCM16_MONO_16K,
    PCM16_MONO_24K,
    PCM16_MONO_48K,
    AudioChunk,
    AudioFormat,
)

# ── Provider implementations ─────────────────────────────────────

from easycat.echo_cancellation import (
    EchoCancellationConfig,
    LiveKitAEC,
    PassthroughAEC,
    create_echo_canceller,
)
from easycat.noise_reduction import (
    KrispNoiseReducer,
    NoiseReducerConfig,
    PassthroughNoiseReducer,
    RNNoiseReducer,
    create_noise_reducer,
)
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
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig
from easycat.tts.input import TTSInput, TTSInputFormat
from easycat.vad import KrispVAD, SileroVAD, TenVAD, VADConfig, create_vad

# ── Transport implementations ────────────────────────────────────

from easycat.transports.local import LocalTransport, LocalTransportConfig
from easycat.transports.twilio_media import (
    TwilioConnectionTransport,
    TwilioTransport,
    TwilioTransportConfig,
)
from easycat.transports.webrtc import ICEServer, WebRTCTransport, WebRTCTransportConfig
from easycat.transports.websocket import (
    WebSocketConnectionTransport,
    WebSocketTransport,
    WebSocketTransportConfig,
)

# ── Configuration & errors ────────────────────────────────────────

from easycat.metrics import InMemoryMetrics, LatencyStats, MetricsCollector
from easycat.timeouts import (
    AgentTimeoutError,
    STTTimeoutError,
    TimeoutConfig,
    TTSTimeoutError,
)
from easycat.tracing import Tracer, TraceExporter

__all__ = [
    # Core session & agent
    "Session",
    "SessionConfig",
    "TurnState",
    "TurnMode",
    "SessionManager",
    "EasyCatConfig",
    "EventLoggingConfig",
    "MetricsConfig",
    "TelephonyConfig",
    "TracingConfig",
    "create_session",
    "AgentRunner",
    "AgentRunnerConfig",
    "AgentStreamEvent",
    "AgentStreamEventType",
    "StreamingAgent",
    # Agent adapters
    "BaseAgentAdapter",
    "OpenAIAgentsAdapter",
    "PydanticAIAdapter",
    "build_openai_agents_adapter",
    "serialize_output",
    "CancelToken",
    "LLMOutputProcessor",
    "MarkdownStripProcessor",
    "PauseProcessor",
    "PhoneticReplacementProcessor",
    "default_pronunciation_processors",
    # Smart turn
    "SmartTurnConfig",
    "SmartTurnONNX",
    "SmartTurnProvider",
    "SmartTurnResult",
    "create_smart_turn",
    # Event groups
    "AUDIO_EVENTS",
    "VAD_EVENTS",
    "STT_EVENTS",
    "AGENT_EVENTS",
    "TTS_EVENTS",
    "TOOL_EVENTS",
    "LIFECYCLE_EVENTS",
    "INTERRUPTION_EVENTS",
    "RECONNECT_EVENTS",
    "TELEPHONY_EVENTS",
    "ERROR_EVENTS",
    "ALL_EVENTS",
    # EasyCat-level events
    "AgentDelta",
    "AgentFinal",
    "AudioIn",
    "BotStartedSpeaking",
    "BotStoppedSpeaking",
    "DTMF",
    "DTMFAggregated",
    "Error",
    "Event",
    "EventBus",
    "Interruption",
    "ReconnectAttempt",
    "ReconnectFailure",
    "ReconnectSuccess",
    "STTFinal",
    "STTPartial",
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
    # Provider protocols
    "EchoCanceller",
    "NoiseReducer",
    "STTProvider",
    "Transport",
    "TTSProvider",
    "TTSInput",
    "TTSInputFormat",
    "VADProvider",
    # Audio format
    "AudioChunk",
    "AudioFormat",
    "PCM16_MONO_8K",
    "PCM16_MONO_16K",
    "PCM16_MONO_24K",
    "PCM16_MONO_48K",
    # STT providers
    "OpenAISTT",
    "OpenAISTTConfig",
    "OpenAIRealtimeSTT",
    "OpenAIRealtimeSTTConfig",
    "DeepgramSTT",
    "DeepgramSTTConfig",
    "ElevenLabsSTT",
    "ElevenLabsSTTConfig",
    # TTS providers
    "OpenAITTS",
    "OpenAITTSConfig",
    "DeepgramTTS",
    "DeepgramTTSConfig",
    "ElevenLabsTTS",
    "ElevenLabsTTSConfig",
    # VAD
    "SileroVAD",
    "KrispVAD",
    "TenVAD",
    "VADConfig",
    "create_vad",
    # Echo cancellation
    "EchoCancellationConfig",
    "LiveKitAEC",
    "PassthroughAEC",
    "create_echo_canceller",
    # Noise reduction
    "RNNoiseReducer",
    "KrispNoiseReducer",
    "PassthroughNoiseReducer",
    "NoiseReducerConfig",
    "create_noise_reducer",
    # Transports
    "ICEServer",
    "LocalTransport",
    "LocalTransportConfig",
    "WebRTCTransport",
    "WebRTCTransportConfig",
    "WebSocketTransport",
    "WebSocketTransportConfig",
    "WebSocketConnectionTransport",
    "TwilioTransport",
    "TwilioTransportConfig",
    "TwilioConnectionTransport",
    # Configuration & errors
    "TimeoutConfig",
    "STTTimeoutError",
    "AgentTimeoutError",
    "TTSTimeoutError",
    "MetricsCollector",
    "InMemoryMetrics",
    "LatencyStats",
    "Tracer",
    "TraceExporter",
    # Helpers
    "attach_runtime_feedback",
    "default_event_logging",
    "require_env",
    "wait_for_shutdown_signal",
]
