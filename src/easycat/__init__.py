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
    from easycat.stt.base import STTBase, pcm_to_wav
    from easycat.tts.base import TTSBase
    from easycat.stt import create_stt_provider
    from easycat.tts.factory import create_tts_provider, TTSProviderConfig
    from easycat.tts.elevenlabs_tts import ElevenLabsStreamMode
    from easycat.events import STTEvent, STTEventType, TTSEvent, TTSEventType, WordTimestamp
    from easycat.telephony import DTMFAggregator, VoicemailDetector, ...
    from easycat.transports.twilio_media import mulaw_to_pcm16, pcm16_to_mulaw, ...
"""

# ── Core session & agent ──────────────────────────────────────────

from easycat.session import Session, SessionConfig, TurnState
from easycat.agent_runner import (
    AgentRunner,
    AgentRunnerConfig,
    AgentStreamEvent,
    AgentStreamEventType,
    StreamingAgent,
)
from easycat.cancel import CancelToken
from easycat.turn_manager import TurnMode

# ── EasyCat-level events ─────────────────────────────────────────

from easycat.events import (
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

from easycat.providers import NoiseReducer, STTProvider, Transport, TTSProvider, VADProvider

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

from easycat.stt import (
    DeepgramSTT,
    DeepgramSTTConfig,
    ElevenLabsSTT,
    ElevenLabsSTTConfig,
    OpenAISTT,
    OpenAISTTConfig,
)
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig
from easycat.vad import KrispVAD, SileroVAD, VADConfig, create_vad
from easycat.noise_reduction import (
    KrispNoiseReducer,
    NoiseReducerConfig,
    PassthroughNoiseReducer,
    RNNoiseReducer,
    create_noise_reducer,
)

# ── Transport implementations ────────────────────────────────────

from easycat.transports.local import LocalTransport, LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransport, TwilioTransportConfig
from easycat.transports.websocket import WebSocketTransport, WebSocketTransportConfig

# ── Configuration & errors ────────────────────────────────────────

from easycat.timeouts import (
    AgentTimeoutError,
    STTTimeoutError,
    TimeoutConfig,
    TTSTimeoutError,
)
from easycat.metrics import InMemoryMetrics, LatencyStats, MetricsCollector
from easycat.tracing import Tracer, TraceExporter

__all__ = [
    # Core session & agent
    "Session",
    "SessionConfig",
    "TurnState",
    "TurnMode",
    "AgentRunner",
    "AgentRunnerConfig",
    "AgentStreamEvent",
    "AgentStreamEventType",
    "StreamingAgent",
    "CancelToken",
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
    "NoiseReducer",
    "STTProvider",
    "Transport",
    "TTSProvider",
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
    "VADConfig",
    "create_vad",
    # Noise reduction
    "RNNoiseReducer",
    "KrispNoiseReducer",
    "PassthroughNoiseReducer",
    "NoiseReducerConfig",
    "create_noise_reducer",
    # Transports
    "LocalTransport",
    "LocalTransportConfig",
    "WebSocketTransport",
    "WebSocketTransportConfig",
    "TwilioTransport",
    "TwilioTransportConfig",
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
]
