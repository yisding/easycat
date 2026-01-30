"""EasyCat — slim, batteries-included voice bot framework."""

from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.audio_utils import chunk_frames, resample, resample_chunk, to_mono, to_mono_chunk
from easycat.events import (
    DTMF,
    AgentDelta,
    AgentFinal,
    AudioIn,
    DTMFAggregated,
    Error,
    Event,
    EventBus,
    STTFinal,
    STTPartial,
    TTSAudio,
    TTSMarkers,
    VADStartSpeaking,
    VADStopSpeaking,
    VoicemailDetected,
)
from easycat.providers import NoiseReducer, STTProvider, Transport, TTSProvider, VADProvider
from easycat.session import Session, SessionConfig, TurnState

__all__ = [
    # Audio format
    "AudioChunk",
    "AudioFormat",
    "PCM16_MONO_8K",
    "PCM16_MONO_16K",
    # Audio utilities
    "chunk_frames",
    "resample",
    "resample_chunk",
    "to_mono",
    "to_mono_chunk",
    # Events
    "AgentDelta",
    "AgentFinal",
    "AudioIn",
    "DTMF",
    "DTMFAggregated",
    "Error",
    "Event",
    "EventBus",
    "STTFinal",
    "STTPartial",
    "TTSAudio",
    "TTSMarkers",
    "VADStartSpeaking",
    "VADStopSpeaking",
    "VoicemailDetected",
    # Providers
    "NoiseReducer",
    "STTProvider",
    "Transport",
    "TTSProvider",
    "VADProvider",
    # Session
    "Session",
    "SessionConfig",
    "TurnState",
]
