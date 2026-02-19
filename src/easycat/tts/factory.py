"""TTS provider factory for creating providers from configuration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from easycat.events import EventBus
from easycat.providers import TTSProvider
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig

TTSConfig = OpenAITTSConfig | DeepgramTTSConfig | ElevenLabsTTSConfig

# Registry of known provider names to their config/class pairs
_PROVIDERS: dict[str, tuple[type[TTSProvider], type[TTSConfig]]] = {
    "openai": (OpenAITTS, OpenAITTSConfig),
    "deepgram": (DeepgramTTS, DeepgramTTSConfig),
    "elevenlabs": (ElevenLabsTTS, ElevenLabsTTSConfig),
}
_CONFIG_TO_PROVIDER: dict[type[TTSConfig], type[TTSProvider]] = {
    cfg_cls: provider_cls for provider_cls, cfg_cls in _PROVIDERS.values()
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


def create_tts_provider_from_config(config: TTSConfig, event_bus: EventBus) -> TTSProvider:
    """Create a TTS provider from a concrete config object.

    This is used by ``easycat.config.create_session`` so there is one TTS
    provider registry in the codebase.
    """
    provider_cls = _provider_for_config(type(config))
    provider_config = config
    if isinstance(config, DeepgramTTSConfig) and config.event_bus is None:
        provider_config = replace(config, event_bus=event_bus)
    return provider_cls(provider_config)


def _provider_for_config(config_type: type[TTSConfig]) -> type[TTSProvider]:
    provider_cls = _CONFIG_TO_PROVIDER.get(config_type)
    if provider_cls is None:
        raise ValueError("Unsupported TTS configuration type.")
    return provider_cls
