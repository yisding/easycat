"""Speech-to-text provider implementations for EasyCat.

Exports load lazily via PEP 562 ``__getattr__`` so importing the
package doesn't pull every provider SDK off disk — only the provider
the caller actually touches.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

_LAZY: dict[str, tuple[str, str]] = {
    "CartesiaSTT": ("easycat.stt.cartesia_provider", "CartesiaSTT"),
    "CartesiaSTTConfig": ("easycat.stt.cartesia_provider", "CartesiaSTTConfig"),
    "DeepgramSTT": ("easycat.stt.deepgram_provider", "DeepgramSTT"),
    "DeepgramSTTConfig": ("easycat.stt.deepgram_provider", "DeepgramSTTConfig"),
    "ElevenLabsSTT": ("easycat.stt.elevenlabs_provider", "ElevenLabsSTT"),
    "ElevenLabsSTTConfig": ("easycat.stt.elevenlabs_provider", "ElevenLabsSTTConfig"),
    "OpenAIRealtimeSTT": ("easycat.stt.openai_realtime_provider", "OpenAIRealtimeSTT"),
    "OpenAIRealtimeSTTConfig": (
        "easycat.stt.openai_realtime_provider",
        "OpenAIRealtimeSTTConfig",
    ),
    "OpenAISTT": ("easycat.stt.openai_provider", "OpenAISTT"),
    "OpenAISTTConfig": ("easycat.stt.openai_provider", "OpenAISTTConfig"),
    "STTBase": ("easycat.stt.base", "STTBase"),
    "create_stt_provider": ("easycat.stt.factory", "create_stt_provider"),
    "pcm_to_wav": ("easycat.stt.base", "pcm_to_wav"),
}


if TYPE_CHECKING:
    from easycat.stt.base import STTBase, pcm_to_wav
    from easycat.stt.cartesia_provider import CartesiaSTT, CartesiaSTTConfig
    from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
    from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
    from easycat.stt.factory import create_stt_provider
    from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig
    from easycat.stt.openai_realtime_provider import (
        OpenAIRealtimeSTT,
        OpenAIRealtimeSTTConfig,
    )


def __getattr__(name: str):  # PEP 562
    try:
        module_path, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module 'easycat.stt' has no attribute {name!r}") from None
    module = importlib.import_module(module_path)
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_LAZY)))


__all__ = sorted(_LAZY)
