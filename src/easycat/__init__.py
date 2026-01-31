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
    WordTimestamp,
)
from easycat.providers import NoiseReducer, STTProvider, Transport, TTSProvider, VADProvider
from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket
from easycat.session import Session, SessionConfig, TurnState
from easycat.stt import (
    DeepgramSTT,
    DeepgramSTTConfig,
    ElevenLabsSTT,
    ElevenLabsSTTConfig,
    OpenAISTT,
    OpenAISTTConfig,
    STTBase,
    create_stt_provider,
    pcm_to_wav,
)
from easycat.tts.base import TTSBase
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsStreamMode, ElevenLabsTTS, ElevenLabsTTSConfig
from easycat.tts.factory import TTSProviderConfig, create_tts_provider
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig

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
    "WordTimestamp",
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
    # ReconnectingWebSocket
    "ReconnectConfig",
    "ReconnectingWebSocket",
    # STT providers
    "STTBase",
    "OpenAISTT",
    "OpenAISTTConfig",
    "DeepgramSTT",
    "DeepgramSTTConfig",
    "ElevenLabsSTT",
    "ElevenLabsSTTConfig",
    "create_stt_provider",
    "pcm_to_wav",
    # TTS providers
    "TTSBase",
    "OpenAITTS",
    "OpenAITTSConfig",
    "DeepgramTTS",
    "DeepgramTTSConfig",
    "ElevenLabsTTS",
    "ElevenLabsTTSConfig",
    "ElevenLabsStreamMode",
    "TTSProviderConfig",
    "create_tts_provider",
]
