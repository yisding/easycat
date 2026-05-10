"""TTS provider implementations for EasyCat."""

from easycat.tts.base import TTSBase
from easycat.tts.cartesia_tts import CartesiaTTS, CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig
from easycat.tts.factory import create_tts_provider
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig

__all__ = [
    "CartesiaTTS",
    "CartesiaTTSConfig",
    "DeepgramTTS",
    "DeepgramTTSConfig",
    "ElevenLabsTTS",
    "ElevenLabsTTSConfig",
    "OpenAITTS",
    "OpenAITTSConfig",
    "TTSBase",
    "create_tts_provider",
]
