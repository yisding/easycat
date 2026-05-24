"""TTS provider implementations for EasyCat.

Exports load lazily via PEP 562 ``__getattr__`` so importing the
package doesn't pull every provider SDK off disk — only the provider
the caller actually touches.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

_LAZY: dict[str, tuple[str, str]] = {
    "CartesiaTTS": ("easycat.tts.cartesia_tts", "CartesiaTTS"),
    "CartesiaTTSConfig": ("easycat.tts.cartesia_tts", "CartesiaTTSConfig"),
    "DeepgramTTS": ("easycat.tts.deepgram_tts", "DeepgramTTS"),
    "DeepgramTTSConfig": ("easycat.tts.deepgram_tts", "DeepgramTTSConfig"),
    "ElevenLabsTTS": ("easycat.tts.elevenlabs_tts", "ElevenLabsTTS"),
    "ElevenLabsTTSConfig": ("easycat.tts.elevenlabs_tts", "ElevenLabsTTSConfig"),
    "OpenAITTS": ("easycat.tts.openai_tts", "OpenAITTS"),
    "OpenAITTSConfig": ("easycat.tts.openai_tts", "OpenAITTSConfig"),
    "TTSBase": ("easycat.tts.base", "TTSBase"),
    "create_tts_provider": ("easycat.tts.factory", "create_tts_provider"),
}


if TYPE_CHECKING:
    from easycat.tts.base import TTSBase
    from easycat.tts.cartesia_tts import CartesiaTTS, CartesiaTTSConfig
    from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
    from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig
    from easycat.tts.factory import create_tts_provider
    from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig


def __getattr__(name: str):  # PEP 562
    try:
        module_path, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module 'easycat.tts' has no attribute {name!r}") from None
    module = importlib.import_module(module_path)
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_LAZY)))


__all__ = sorted(_LAZY)
