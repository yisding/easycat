"""STT provider factory — create providers by name with validated config."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from easycat._provider_catalog import (
    ProviderCapabilityResolver,
    ProviderCatalog,
    ProviderSpec,
)
from easycat.events import EventBus
from easycat.stt.base import STTBase
from easycat.stt.cartesia_provider import CartesiaSTT, CartesiaSTTConfig
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig

# Typing aliases for the *built-in* configs. The runtime registry is open:
# third-party providers registered via :func:`register_stt_provider` (or the
# ``easycat.stt_providers`` entry-point group) dispatch through the catalog,
# so runtime checks must use :func:`is_stt_config`, never ``isinstance``
# against this union.
STTConfig = (
    OpenAISTTConfig
    | OpenAIRealtimeSTTConfig
    | DeepgramSTTConfig
    | ElevenLabsSTTConfig
    | CartesiaSTTConfig
)
STT_PROVIDER_ENTRY_POINT_GROUP = "easycat.stt_providers"

#: The provider ``EasyConfig`` falls back to when ``stt=`` is left unset.
#:
#: Named here so the planner and ``EasyConfig`` are greppable as a pair.
#: ``easycat.config.easy`` deliberately does NOT import this constant: it fills
#: its default by constructing ``OpenAIRealtimeSTTConfig`` directly, and routing
#: that through the catalog would force ``discover()`` — third-party entry-point
#: execution — during plain ``EasyConfig()`` construction. The two are tied
#: together by ``tests/config/test_easyconfig_defaults.py::
#: test_openai_defaults_match_the_planner_default_provider_names`` instead, so do
#: not "finish the job" by wiring them.
DEFAULT_STT_PROVIDER = "openai-realtime"


def _deepgram_capabilities(config: Any, model: str | None) -> frozenset[str]:
    selected_model = config.model if isinstance(config, DeepgramSTTConfig) else model
    selected_model = selected_model or DeepgramSTTConfig.model
    if selected_model.lower().startswith("flux"):
        return frozenset({"native_endpointing"})
    return frozenset()


def _cartesia_capabilities(config: Any, model: str | None) -> frozenset[str]:
    if isinstance(config, CartesiaSTTConfig):
        selected_model = config.resolved_model
    else:
        selected_model = model or CartesiaSTTConfig().resolved_model
    if selected_model == "ink-2":
        return frozenset({"native_endpointing"})
    return frozenset()


def _elevenlabs_capabilities(config: Any, model: str | None) -> frozenset[str]:
    del model
    if isinstance(config, ElevenLabsSTTConfig):
        mode = config.mode
        commit_strategy = config.realtime_commit_strategy
    else:
        mode = ElevenLabsSTTConfig.mode
        commit_strategy = ElevenLabsSTTConfig.realtime_commit_strategy
    if mode == "realtime" and commit_strategy == "vad":
        return frozenset({"native_endpointing"})
    return frozenset()


_CATALOG = ProviderCatalog(
    specs={
        # implementation, config, credential env, install extra, API domains
        "openai": ProviderSpec(
            OpenAISTT,
            OpenAISTTConfig,
            "OPENAI_API_KEY",
            "openai",
            ("openai.com",),
        ),
        "openai-realtime": ProviderSpec(
            OpenAIRealtimeSTT,
            OpenAIRealtimeSTTConfig,
            "OPENAI_API_KEY",
            "openai",
            ("openai.com",),
        ),
        "deepgram": ProviderSpec(
            DeepgramSTT,
            DeepgramSTTConfig,
            "DEEPGRAM_API_KEY",
            "deepgram",
            ("deepgram.com",),
            capability_resolver=_deepgram_capabilities,
        ),
        "elevenlabs": ProviderSpec(
            ElevenLabsSTT,
            ElevenLabsSTTConfig,
            "ELEVENLABS_API_KEY",
            "elevenlabs",
            ("elevenlabs.io",),
            capability_resolver=_elevenlabs_capabilities,
        ),
        "cartesia": ProviderSpec(
            CartesiaSTT,
            CartesiaSTTConfig,
            "CARTESIA_API_KEY",
            "cartesia",
            ("cartesia.ai",),
            capability_resolver=_cartesia_capabilities,
        ),
    },
    kind="STT",
    entry_point_group=STT_PROVIDER_ENTRY_POINT_GROUP,
)
_PROVIDER_TO_CONFIG = _CATALOG.providers
_PROVIDER_ENV_VAR = _CATALOG.env_vars


def register_stt_provider(
    name: str,
    provider_cls: type,
    config_cls: type,
    *,
    env_var: str | None = None,
    extra: str | None = None,
    api_domains: tuple[str, ...] = (),
    probe_module: str | None = None,
    capabilities: frozenset[str] = frozenset(),
    capability_resolver: ProviderCapabilityResolver | None = None,
) -> None:
    """Register an STT provider and its credential, discovery, and capability metadata.

    ``capability_resolver`` receives the concrete config (when available) and
    selected model. Its result is combined with the static ``capabilities``.
    """
    _CATALOG.register(
        name,
        provider_cls,
        config_cls,
        env_var=env_var,
        extra=extra,
        api_domains=api_domains,
        probe_module=probe_module,
        capabilities=capabilities,
        capability_resolver=capability_resolver,
    )


def is_stt_config(value: object) -> bool:
    """True when ``value`` is an instance of a registered STT config class."""
    return _CATALOG.is_config_instance(value)


@dataclass
class STTProviderConfig:
    """Named STT provider, credential, and provider-specific parameters."""

    provider: str
    api_key: str | None = field(default=None, repr=False)
    params: dict[str, Any] | None = None

    def __repr__(self) -> str:
        # Import lazily: safe_defaults discovers provider catalogs while it is
        # initializing its redaction policy.
        from easycat.runtime.safe_defaults import _safe_repr

        return (
            f"STTProviderConfig(provider={_safe_repr(self.provider)}, "
            f"params={_safe_repr(self.params)})"
        )


def create_stt_provider(config: STTProviderConfig, event_bus: EventBus | None = None) -> STTBase:
    """Create a registered STT provider, optionally wiring its event bus."""
    return _CATALOG.create_provider(
        config.provider,
        params=config.params,
        api_key=config.api_key,
        event_bus=event_bus,
    )


def create_stt_provider_from_config(config: STTConfig, event_bus: EventBus) -> STTBase:
    """Create an STT provider from a concrete provider config."""
    return _CATALOG.create_from_config(config, event_bus)


def available_stt_providers() -> list[str]:
    """Return every valid ``stt=`` provider name, sorted."""
    return _CATALOG.available_names()


def parse_stt_string(
    spec: str, *, api_key_overrides: Mapping[str, str] | None = None
) -> STTConfig:
    """Parse a ``provider/model`` shortcut into a concrete STT config."""
    return _CATALOG.parse_string(spec, api_key_overrides=api_key_overrides)
