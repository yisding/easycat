"""TTS provider factory for creating providers from configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from easycat.providers import TTSProvider
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig

# Registry of known provider names to their config/class pairs
_PROVIDERS: dict[str, tuple[type, type]] = {
    "openai": (OpenAITTS, OpenAITTSConfig),
    "deepgram": (DeepgramTTS, DeepgramTTSConfig),
    "elevenlabs": (ElevenLabsTTS, ElevenLabsTTSConfig),
}


@dataclass
class TTSProviderConfig:
    """Top-level TTS provider configuration.

    Specifies which provider to use and passes provider-specific
    settings through to the provider's config class.
    """

    provider: str
    settings: dict[str, Any] | None = None


def create_tts_provider(config: TTSProviderConfig) -> TTSProvider:
    """Create a TTS provider instance from a configuration object.

    Validates the provider name and settings at construction time.

    Raises:
        ValueError: If the provider name is unknown or settings are invalid.
    """
    provider_name = config.provider.lower()

    if provider_name not in _PROVIDERS:
        available = ", ".join(sorted(_PROVIDERS.keys()))
        raise ValueError(f"Unknown TTS provider: {config.provider!r}. Available: {available}")

    provider_cls, config_cls = _PROVIDERS[provider_name]
    settings = config.settings or {}

    try:
        provider_config = config_cls(**settings)
    except TypeError as exc:
        raise ValueError(f"Invalid settings for {config.provider!r} TTS provider: {exc}") from exc

    return provider_cls(provider_config)
