"""TTS provider factory for creating providers from configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from easycat._provider_catalog import ProviderCatalog, ProviderSpec
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
TTS_PROVIDER_ENTRY_POINT_GROUP = "easycat.tts_providers"

#: The provider ``EasyConfig`` falls back to when ``tts=`` is left unset.
#:
#: Named here so the planner and ``EasyConfig`` are greppable as a pair.
#: ``easycat.config.easy`` deliberately does NOT import this constant: it fills
#: its default by constructing ``OpenAITTSConfig`` directly, and routing that
#: through the catalog would force ``discover()`` — third-party entry-point
#: execution — during plain ``EasyConfig()`` construction. The two are tied
#: together by ``tests/config/test_easyconfig_defaults.py::
#: test_openai_defaults_match_the_planner_default_provider_names`` instead, so do
#: not "finish the job" by wiring them.
DEFAULT_TTS_PROVIDER = "openai"

_CATALOG = ProviderCatalog(
    specs={
        # implementation, config, credential env, install extra, API domains
        "openai": ProviderSpec(
            OpenAITTS,
            OpenAITTSConfig,
            "OPENAI_API_KEY",
            "openai",
            ("openai.com",),
        ),
        "deepgram": ProviderSpec(
            DeepgramTTS, DeepgramTTSConfig, "DEEPGRAM_API_KEY", "deepgram", ("deepgram.com",)
        ),
        "elevenlabs": ProviderSpec(
            ElevenLabsTTS,
            ElevenLabsTTSConfig,
            "ELEVENLABS_API_KEY",
            "elevenlabs",
            ("elevenlabs.io",),
        ),
        "cartesia": ProviderSpec(
            CartesiaTTS, CartesiaTTSConfig, "CARTESIA_API_KEY", "cartesia", ("cartesia.ai",)
        ),
    },
    kind="TTS",
    entry_point_group=TTS_PROVIDER_ENTRY_POINT_GROUP,
)
_PROVIDER_TO_CONFIG = _CATALOG.providers
_PROVIDER_ENV_VAR = _CATALOG.env_vars


def register_tts_provider(
    name: str,
    provider_cls: type,
    config_cls: type,
    *,
    env_var: str | None = None,
    extra: str | None = None,
    api_domains: tuple[str, ...] = (),
    probe_module: str | None = None,
    capabilities: frozenset[str] = frozenset(),
) -> None:
    """Register a TTS provider and its optional credential/discovery metadata."""
    _CATALOG.register(
        name,
        provider_cls,
        config_cls,
        env_var=env_var,
        extra=extra,
        api_domains=api_domains,
        probe_module=probe_module,
        capabilities=capabilities,
    )


def is_tts_config(value: object) -> bool:
    """True when ``value`` is an instance of a registered TTS config class."""
    return _CATALOG.is_config_instance(value)


@dataclass
class TTSProviderConfig:
    """Named TTS provider, credential, and provider-specific parameters."""

    provider: str
    api_key: str | None = field(default=None, repr=False)
    params: dict[str, Any] | None = None

    def __repr__(self) -> str:
        # Import lazily: safe_defaults discovers provider catalogs while it is
        # initializing its redaction policy.
        from easycat.runtime.safe_defaults import _safe_repr

        return (
            f"TTSProviderConfig(provider={_safe_repr(self.provider)}, "
            f"params={_safe_repr(self.params)})"
        )


def create_tts_provider(
    config: TTSProviderConfig, event_bus: EventBus | None = None
) -> TTSProvider:
    """Create a registered TTS provider, optionally wiring its event bus."""
    return _CATALOG.create_provider(
        config.provider,
        params=config.params,
        api_key=config.api_key,
        event_bus=event_bus,
    )


def create_tts_provider_from_config(config: TTSConfig, event_bus: EventBus) -> TTSProvider:
    """Create a TTS provider from a concrete provider config."""
    return _CATALOG.create_from_config(config, event_bus)


def available_tts_providers() -> list[str]:
    """Return every valid ``tts=`` provider name, sorted."""
    return _CATALOG.available_names()


def parse_tts_string(
    spec: str, *, api_key_overrides: Mapping[str, str] | None = None
) -> TTSConfig:
    """Parse a ``provider/model`` shortcut into a concrete TTS config."""
    return _CATALOG.parse_string(spec, api_key_overrides=api_key_overrides)
