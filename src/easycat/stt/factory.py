"""STT provider factory — create providers by name with validated config."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from easycat.events import EventBus
from easycat.stt.base import STTBase
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig

STTConfig = OpenAISTTConfig | DeepgramSTTConfig | ElevenLabsSTTConfig

_PROVIDER_TO_CONFIG: dict[str, tuple[type[STTBase], type[STTConfig]]] = {
    "openai": (OpenAISTT, OpenAISTTConfig),
    "deepgram": (DeepgramSTT, DeepgramSTTConfig),
    "elevenlabs": (ElevenLabsSTT, ElevenLabsSTTConfig),
}
_CONFIG_TO_PROVIDER: dict[type[STTConfig], type[STTBase]] = {
    cfg_cls: provider_cls for provider_cls, cfg_cls in _PROVIDER_TO_CONFIG.values()
}


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
    provider_name = config.provider.lower()
    if provider_name not in _PROVIDER_TO_CONFIG:
        raise ValueError(
            f"Unknown STT provider '{config.provider}'. "
            f"Available providers: {', '.join(sorted(_PROVIDER_TO_CONFIG))}"
        )

    if not config.api_key:
        raise ValueError(f"API key is required for STT provider '{config.provider}'")

    extra = config.params or {}
    provider_cls, config_cls = _PROVIDER_TO_CONFIG[provider_name]
    provider_config = config_cls(api_key=config.api_key, **extra)
    return provider_cls(provider_config)


def create_stt_provider_from_config(config: STTConfig, event_bus: EventBus) -> STTBase:
    """Create an STT provider from a concrete config object.

    This is used by ``easycat.config.create_session`` so there is one STT
    provider registry in the codebase.
    """
    provider_cls = _provider_for_config(type(config))
    provider_config = config
    if isinstance(config, (DeepgramSTTConfig, ElevenLabsSTTConfig)) and config.event_bus is None:
        provider_config = replace(config, event_bus=event_bus)
    return provider_cls(provider_config)


def _provider_for_config(config_type: type[STTConfig]) -> type[STTBase]:
    provider_cls = _CONFIG_TO_PROVIDER.get(config_type)
    if provider_cls is None:
        raise ValueError("Unsupported STT configuration type.")
    return provider_cls
