"""STT provider factory — create providers by name with validated config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from easycat.stt.base import STTBase
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig

_PROVIDERS = {"openai", "deepgram", "elevenlabs"}


@dataclass
class STTProviderConfig:
    """Top-level configuration for creating an STT provider."""

    provider: str
    api_key: str
    params: dict[str, Any] | None = None


def create_stt_provider(config: STTProviderConfig) -> STTBase:
    """Create an STT provider instance from configuration.

    Validates the provider name and API key at construction time (fail-fast).
    Provider-specific parameters are passed via ``config.params``.
    """
    if config.provider not in _PROVIDERS:
        raise ValueError(
            f"Unknown STT provider '{config.provider}'. "
            f"Available providers: {', '.join(sorted(_PROVIDERS))}"
        )

    if not config.api_key:
        raise ValueError(f"API key is required for STT provider '{config.provider}'")

    extra = config.params or {}

    if config.provider == "openai":
        return OpenAISTT(OpenAISTTConfig(api_key=config.api_key, **extra))

    if config.provider == "deepgram":
        return DeepgramSTT(DeepgramSTTConfig(api_key=config.api_key, **extra))

    # elevenlabs
    return ElevenLabsSTT(ElevenLabsSTTConfig(api_key=config.api_key, **extra))
