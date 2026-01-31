"""EasyCat — slim, batteries-included voice bot framework."""

from easycat.audio_format import (
    PCM16_MONO_8K,
    PCM16_MONO_16K,
    PCM16_MONO_24K,
    PCM16_MONO_48K,
    AudioChunk,
    AudioFormat,
)
from easycat.audio_utils import chunk_frames, resample, resample_chunk, to_mono, to_mono_chunk
from easycat.cancel import CancelToken
from easycat.events import (
    DTMF,
    AgentDelta,
    AgentFinal,
    AudioIn,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    DTMFAggregated,
    Error,
    Event,
    EventBus,
    Interruption,
    ReconnectAttempt,
    ReconnectFailure,
    ReconnectSuccess,
    STTEvent,
    STTEventType,
    STTFinal,
    STTPartial,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TTSAudio,
    TTSEvent,
    TTSEventType,
    TTSMarkers,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
    VoicemailDetected,
)
from easycat.noise_reduction import (
    KrispNoiseReducer,
    NoiseReducerConfig,
    PassthroughNoiseReducer,
    RNNoiseReducer,
    create_noise_reducer,
)
from easycat.providers import NoiseReducer, STTProvider, Transport, TTSProvider, VADProvider
from easycat.session import Session, SessionConfig, TurnState
from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState, TurnMode
from easycat.vad import KrispVAD, SileroVAD, VADConfig, create_vad

__all__ = [
    # Audio format
    "AudioChunk",
    "AudioFormat",
    "PCM16_MONO_8K",
    "PCM16_MONO_16K",
    "PCM16_MONO_24K",
    "PCM16_MONO_48K",
    # Audio utilities
    "chunk_frames",
    "resample",
    "resample_chunk",
    "to_mono",
    "to_mono_chunk",
    # Cancel
    "CancelToken",
    # EasyCat events
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
    "TTSAudio",
    "TTSMarkers",
    "ToolCallDelta",
    "ToolCallResult",
    "ToolCallStarted",
    "TurnEnded",
    "TurnStarted",
    "VADStartSpeaking",
    "VADStopSpeaking",
    "VoicemailDetected",
    # Provider-scoped events
    "STTEvent",
    "STTEventType",
    "TTSEvent",
    "TTSEventType",
    # Providers
    "NoiseReducer",
    "STTProvider",
    "Transport",
    "TTSProvider",
    "VADProvider",
    # Noise reduction (WS4)
    "RNNoiseReducer",
    "KrispNoiseReducer",
    "PassthroughNoiseReducer",
    "NoiseReducerConfig",
    "create_noise_reducer",
    # VAD (WS4)
    "SileroVAD",
    "KrispVAD",
    "VADConfig",
    "create_vad",
    # Turn-taking (WS4)
    "TurnManager",
    "TurnManagerConfig",
    "TurnManagerState",
    "TurnMode",
    # Session
    "Session",
    "SessionConfig",
    "TurnState",
]
