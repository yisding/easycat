"""TTS provider factory for creating providers from configuration."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any

from easycat._provider_catalog import ProviderCatalog
from easycat.events import EventBus
from easycat.providers import TTSProvider
from easycat.tts.cartesia_tts import CartesiaTTS, CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig

# Typing aliases for the *built-in* configs. The runtime registry is open:
# third-party providers registered via :func:`register_tts_provider` (or the
# ``easycat.tts_providers`` entry-point group) dispatch through the catalog,
# so runtime checks must use :func:`is_tts_config`, never ``isinstance``
# against this union.
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

# Provider name → optional install extra shipping its dependencies.
# Consumed by ``easycat init`` to scaffold ``pyproject.toml`` extras.
_PROVIDER_EXTRA: dict[str, str] = {
    "openai": "openai",
    "deepgram": "deepgram",
    "elevenlabs": "elevenlabs",
    "cartesia": "cartesia",
}

# Provider name → API host domains. Consumed by validation redaction to
# scrub provider URLs from exported artifacts.
_PROVIDER_API_DOMAINS: dict[str, tuple[str, ...]] = {
    "openai": ("openai.com",),
    "deepgram": ("deepgram.com",),
    "elevenlabs": ("elevenlabs.io",),
    "cartesia": ("cartesia.ai",),
}

# Entry-point group scanned (lazily, at first factory call) for
# third-party TTS providers. Each entry point must load to a zero-arg
# callable that calls :func:`register_tts_provider`.
TTS_PROVIDER_ENTRY_POINT_GROUP = "easycat.tts_providers"

_CATALOG = ProviderCatalog(
    providers=_PROVIDER_TO_CONFIG,
    env_vars=_PROVIDER_ENV_VAR,
    extras=_PROVIDER_EXTRA,
    api_domains=_PROVIDER_API_DOMAINS,
    kind="TTS",
    entry_point_group=TTS_PROVIDER_ENTRY_POINT_GROUP,
)
_CONFIG_TO_PROVIDER: dict[TTSConfigType, type[TTSProvider]] = _CATALOG.config_to_provider


def register_tts_provider(
    name: str,
    provider_cls: type,
    config_cls: type,
    *,
    env_var: str,
    extra: str | None = None,
    api_domains: tuple[str, ...] = (),
) -> None:
    """Register a third-party TTS provider under a string shortcut name.

    After registration the provider participates everywhere built-ins do:
    ``tts="<name>/<model>"`` shortcuts, :func:`create_tts_provider`,
    :func:`available_tts_providers`, ``easycat doctor`` env-var checks,
    and ``easycat init`` scaffold validation.

    ``provider_cls`` must accept its ``config_cls`` instance as the sole
    constructor argument (the same contract built-in providers follow);
    ``env_var`` names the environment variable holding the API key.
    ``extra`` optionally names an install extra surfaced by ``easycat
    init`` scaffold extras; ``api_domains`` optionally lists API host
    domains folded into validation's URL redaction.

    Packages can register automatically by exposing a zero-arg callable
    that performs this call under the ``easycat.tts_providers``
    entry-point group.
    """
    _CATALOG.register(
        name, provider_cls, config_cls, env_var=env_var, extra=extra, api_domains=api_domains
    )


def is_tts_config(value: object) -> bool:
    """True when ``value`` is an instance of a registered TTS config class."""
    return _CATALOG.is_config_instance(value)


def provider_env_vars() -> dict[str, str]:
    """Return the provider-name → API-key-env-var map (discovery included)."""
    return _CATALOG.provider_env_vars()


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
    # Derive "needs an event bus" structurally from the dataclass itself
    # (it declares an ``event_bus`` field) rather than from a hand-maintained
    # isinstance tuple — so any future event-bus-aware provider is included
    # automatically.
    has_event_bus_field = any(f.name == "event_bus" for f in fields(config))
    if has_event_bus_field and config.event_bus is None:
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
