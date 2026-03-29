"""Speech-to-text provider implementations for EasyCat."""

from easycat.stt.base import STTBase, pcm_to_wav
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from easycat.stt.factory import create_stt_provider
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig

__all__ = [
    "STTBase",
    "pcm_to_wav",
    "OpenAISTT",
    "OpenAISTTConfig",
    "OpenAIRealtimeSTT",
    "OpenAIRealtimeSTTConfig",
    "DeepgramSTT",
    "DeepgramSTTConfig",
    "ElevenLabsSTT",
    "ElevenLabsSTTConfig",
    "create_stt_provider",
]
