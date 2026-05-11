"""Speech-to-text provider implementations for EasyCat."""

from easycat.stt.base import STTBase, pcm_to_wav
from easycat.stt.cartesia_provider import CartesiaSTT, CartesiaSTTConfig
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from easycat.stt.factory import create_stt_provider
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig

__all__ = [
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
    "STTBase",
    "create_stt_provider",
    "pcm_to_wav",
]
