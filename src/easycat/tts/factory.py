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
TTSConfigType = (
    type[OpenAITTSConfig]
    | type[DeepgramTTSConfig]
    | type[ElevenLabsTTSConfig]
    | type[CartesiaTTSConfig]
)

# Registry of known provider names to their config/class pairs. Named to
# mirror ``easycat.stt.factory._PROVIDER_TO_CONFIG`` so the two factories
# stay symmetric.
_PROVIDER_TO_CONFIG: dict[str, tuple[type[TTSProvider], TTSConfigType]] = {
    "openai": (OpenAITTS, OpenAITTSConfig),
    "deepgram": (DeepgramTTS, DeepgramTTSConfig),
    "elevenlabs": (ElevenLabsTTS, ElevenLabsTTSConfig),
    "cartesia": (CartesiaTTS, CartesiaTTSConfig),
}

# Back-compat alias for the pre-rename registry name.
_PROVIDERS = _PROVIDER_TO_CONFIG

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
    providers=_PROVIDER_TO_CONFIG,
    env_vars=_PROVIDER_ENV_VAR,
    kind="TTS",
)
_CONFIG_TO_PROVIDER: dict[TTSConfigType, type[TTSProvider]] = _CATALOG.config_to_provider


@dataclass
class TTSProviderConfig:
    """Top-level configuration for creating a TTS provider.

    Mirrors :class:`easycat.stt.factory.STTProviderConfig`: ``api_key``
    is a top-level field and provider-specific parameters are passed via
    ``params``. An ``api_key`` nested inside ``params`` is also honored
    (and a top-level ``api_key`` takes precedence when both are set).

    ``settings`` is a deprecated alias for ``params``, kept so existing
    callers (e.g. ``TTSProviderConfig(provider="openai",
    settings={"api_key": k})``) keep working; it is folded into
    ``params`` at construction.
    """

    provider: str
    api_key: str | None = None
    params: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # Fold the deprecated ``settings`` alias into ``params`` so the
        # rest of the factory only has to read ``params``.
        if self.settings is not None:
            merged = dict(self.settings)
            if self.params:
                merged.update(self.params)
            self.params = merged
            self.settings = None


def create_tts_provider(config: TTSProviderConfig) -> TTSProvider:
    """Create a TTS provider instance from a configuration object.

    Validates the provider name and params at construction time.

    Raises:
        EasyCatError (EASYCAT_E104): Unknown provider name, with fuzzy-match
            suggestion (shared with the ``tts="provider/model"`` shortcut path).
        ValueError: If the params are invalid or the API key is missing.
    """
    provider_name = _CATALOG.validate_name(config.provider)

    provider_cls, config_cls = _PROVIDER_TO_CONFIG[provider_name]
    params = dict(config.params or {})
    if config.api_key is not None:
        params["api_key"] = config.api_key

    try:
        provider_config = config_cls(**params)
    except TypeError as exc:
        raise ValueError(f"Invalid params for {config.provider!r} TTS provider: {exc}") from exc

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


def _provider_for_config(config_type: TTSConfigType) -> type[TTSProvider]:
    return _CATALOG.provider_for_config(config_type)


def available_providers() -> list[str]:
    """Return every registered TTS provider name, sorted."""
    return _CATALOG.available_names()


def available_tts_providers() -> list[str]:
    """Return every valid ``tts=`` provider name, sorted.

    Public, unambiguously named alias of :func:`available_providers`,
    exported from the top-level ``easycat`` package so callers can
    enumerate valid ``tts="provider/model"`` shortcut names.
    """
    return available_providers()


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
