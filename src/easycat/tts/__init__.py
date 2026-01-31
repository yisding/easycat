"""TTS provider implementations for EasyCat."""

from easycat.tts.base import TTSBase
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig
from easycat.tts.factory import create_tts_provider
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig
from easycat.tts.test_harness import collect_tts_output, write_wav

__all__ = [
    "TTSBase",
    "OpenAITTS",
    "OpenAITTSConfig",
    "DeepgramTTS",
    "DeepgramTTSConfig",
    "ElevenLabsTTS",
    "ElevenLabsTTSConfig",
    "create_tts_provider",
    "collect_tts_output",
    "write_wav",
]
