"""STT provider factory — create providers by name with validated config."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from difflib import get_close_matches
from typing import Any

from easycat.events import EventBus
from easycat.stt.base import STTBase
from easycat.stt.cartesia_provider import CartesiaSTT, CartesiaSTTConfig
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig

STTConfig = (
    OpenAISTTConfig
    | OpenAIRealtimeSTTConfig
    | DeepgramSTTConfig
    | ElevenLabsSTTConfig
    | CartesiaSTTConfig
)
STTConfigType = (
    type[OpenAISTTConfig]
    | type[OpenAIRealtimeSTTConfig]
    | type[DeepgramSTTConfig]
    | type[ElevenLabsSTTConfig]
    | type[CartesiaSTTConfig]
)

_PROVIDER_TO_CONFIG: dict[str, tuple[type[STTBase], STTConfigType]] = {
    "openai": (OpenAISTT, OpenAISTTConfig),
    "openai-realtime": (OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig),
    "deepgram": (DeepgramSTT, DeepgramSTTConfig),
    "elevenlabs": (ElevenLabsSTT, ElevenLabsSTTConfig),
    "cartesia": (CartesiaSTT, CartesiaSTTConfig),
}
_CONFIG_TO_PROVIDER: dict[STTConfigType, type[STTBase]] = {
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
    if not isinstance(config.provider, str):
        raise ValueError(
            f"Unknown STT provider '{config.provider}'. "
            f"Available providers: {', '.join(sorted(_PROVIDER_TO_CONFIG))}"
        )

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
    needs_event_bus = isinstance(
        config,
        (DeepgramSTTConfig, ElevenLabsSTTConfig, OpenAIRealtimeSTTConfig, CartesiaSTTConfig),
    )
    if needs_event_bus and config.event_bus is None:
        provider_config = replace(config, event_bus=event_bus)
    return provider_cls(provider_config)


def _provider_for_config(config_type: STTConfigType) -> type[STTBase]:
    provider_cls = _CONFIG_TO_PROVIDER.get(config_type)
    if provider_cls is None:
        raise ValueError("Unsupported STT configuration type.")
    return provider_cls


# Provider name → env var that holds its API key. Used by string-keyed
# provider selection (e.g. ``stt="deepgram/flux"``) to auto-detect the
# API key without explicit wiring.
_PROVIDER_ENV_VAR: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "openai-realtime": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
}


def available_providers() -> list[str]:
    """Return every registered STT provider name, sorted."""
    return sorted(_PROVIDER_TO_CONFIG)


def parse_stt_string(spec: str) -> STTConfig:
    """Parse a ``"provider/model"`` (or bare ``"provider"``) shortcut.

    Looks up the provider in the registry, reads the corresponding API
    key from the env var (:data:`_PROVIDER_ENV_VAR`), and returns a
    concrete :class:`STTConfig` with ``model`` set when supplied.

    Callers that want programmatic API-key injection (e.g. feeding
    ``EasyConfig.openai_api_key`` into an ``stt="openai"`` shortcut)
    should set the provider's env var in the process scope before
    calling — see ``_openai_env_override`` in ``easycat.config``.

    Raises:
        EasyCatError (EASYCAT_E104): Unknown provider, with fuzzy-match
            suggestion.
        EasyCatError (EASYCAT_E203): Missing required API key env var.
    """
    from easycat.errors import EASYCAT_E104, EASYCAT_E203

    provider, _, model = spec.partition("/")
    provider = provider.strip().lower()
    model = model.strip() or None

    if provider not in _PROVIDER_TO_CONFIG:
        available = available_providers()
        suggestion = get_close_matches(provider, available, n=1, cutoff=0.5)
        hint = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
        raise EASYCAT_E104(
            provider=provider,
            available=", ".join(available),
            hint=hint,
        )

    env_var = _PROVIDER_ENV_VAR[provider]
    api_key = os.getenv(env_var, "")
    if not api_key:
        raise EASYCAT_E203(var=env_var)

    _, config_cls = _PROVIDER_TO_CONFIG[provider]
    kwargs: dict[str, Any] = {"api_key": api_key}
    if model:
        kwargs["model"] = model
    return config_cls(**kwargs)
