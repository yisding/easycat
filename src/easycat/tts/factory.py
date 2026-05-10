"""TTS provider factory for creating providers from configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from difflib import get_close_matches
from typing import Any

from easycat.events import EventBus
from easycat.providers import TTSProvider
from easycat.tts.cartesia_tts import CartesiaTTS, CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig

TTSConfig = OpenAITTSConfig | DeepgramTTSConfig | ElevenLabsTTSConfig | CartesiaTTSConfig

# Registry of known provider names to their config/class pairs
_PROVIDERS: dict[str, tuple[type[TTSProvider], type[TTSConfig]]] = {
    "openai": (OpenAITTS, OpenAITTSConfig),
    "deepgram": (DeepgramTTS, DeepgramTTSConfig),
    "elevenlabs": (ElevenLabsTTS, ElevenLabsTTSConfig),
    "cartesia": (CartesiaTTS, CartesiaTTSConfig),
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
    needs_event_bus = isinstance(
        config,
        (DeepgramTTSConfig, ElevenLabsTTSConfig, CartesiaTTSConfig),
    )
    if needs_event_bus and config.event_bus is None:
        provider_config = replace(config, event_bus=event_bus)
    return provider_cls(provider_config)


def _provider_for_config(config_type: type[TTSConfig]) -> type[TTSProvider]:
    provider_cls = _CONFIG_TO_PROVIDER.get(config_type)
    if provider_cls is None:
        raise ValueError("Unsupported TTS configuration type.")
    return provider_cls


# Provider name → env var that holds its API key. Used by string-keyed
# provider selection (e.g. ``tts="openai"``) to auto-detect the API
# key without explicit wiring.
_PROVIDER_ENV_VAR: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
}

# ElevenLabs and Cartesia both name the model field ``model_id`` rather
# than ``model``. Bridge the naming gap so users can write
# ``tts="elevenlabs/eleven_flash_v2_5"`` or ``tts="cartesia/sonic-turbo"``
# without caring about the field-level quirk.
_MODEL_FIELD_NAME: dict[str, str] = {
    "elevenlabs": "model_id",
    "cartesia": "model_id",
}


def available_providers() -> list[str]:
    """Return every registered TTS provider name, sorted."""
    return sorted(_PROVIDERS)


def parse_tts_string(spec: str) -> TTSConfig:
    """Parse a ``"provider/model"`` (or bare ``"provider"``) shortcut.

    Looks up the provider in the registry, reads the corresponding API
    key from the env var (:data:`_PROVIDER_ENV_VAR`), and returns a
    concrete :class:`TTSConfig` with ``model`` set when supplied.

    Callers that want programmatic API-key injection (e.g. feeding
    ``EasyConfig.openai_api_key`` into a ``tts="openai"`` shortcut)
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

    if provider not in _PROVIDERS:
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

    _, config_cls = _PROVIDERS[provider]
    kwargs: dict[str, Any] = {"api_key": api_key}
    if model:
        model_field = _MODEL_FIELD_NAME.get(provider, "model")
        kwargs[model_field] = model
    return config_cls(**kwargs)
