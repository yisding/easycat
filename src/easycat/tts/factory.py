"""TTS provider factory for creating providers from configuration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from easycat._provider_catalog import ProviderCatalog
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

# Provider name → env var that holds its API key. Used by string-keyed
# provider selection (e.g. ``tts="openai"``) to auto-detect the API
# key without explicit wiring.
_PROVIDER_ENV_VAR: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
}

_CATALOG = ProviderCatalog(
    providers=_PROVIDERS,
    env_vars=_PROVIDER_ENV_VAR,
    kind="TTS",
)
_CONFIG_TO_PROVIDER: dict[type[TTSConfig], type[TTSProvider]] = _CATALOG.config_to_provider


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
    if not isinstance(config.provider, str) or not config.provider:
        available = ", ".join(sorted(_PROVIDERS.keys()))
        raise ValueError(f"Unknown TTS provider: {config.provider!r}. Available: {available}")

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

    if not provider_config.api_key:
        raise ValueError(f"API key is required for TTS provider '{config.provider}'")

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
    return _CATALOG.provider_for_config(config_type)


def available_providers() -> list[str]:
    """Return every registered TTS provider name, sorted."""
    return _CATALOG.available_names()


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
    return _CATALOG.parse_string(spec)
