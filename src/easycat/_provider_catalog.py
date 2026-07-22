"""Shared STT/TTS provider registration, lookup, and metadata."""

from __future__ import annotations

import importlib.metadata
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from difflib import get_close_matches
from typing import Any

logger = logging.getLogger("easycat")


@dataclass(frozen=True)
class ProviderSpec:
    """Classes and discovery metadata for one built-in provider."""

    provider_cls: Callable[..., Any]
    config_cls: type
    env_var: str
    extra: str
    api_domains: tuple[str, ...]


@dataclass(frozen=True)
class ProviderCatalog:
    """Open provider registry shared by the STT and TTS factories."""

    specs: Mapping[str, ProviderSpec]
    kind: str
    entry_point_group: str | None = None
    providers: dict[str, tuple[Callable[..., Any], type]] = field(init=False)
    env_vars: dict[str, str] = field(init=False)
    extras: dict[str, str] = field(init=False)
    api_domains: dict[str, tuple[str, ...]] = field(init=False)
    config_to_provider: dict[type, Callable[..., Any]] = field(init=False)
    _discovered: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        providers = {
            name: (spec.provider_cls, spec.config_cls) for name, spec in self.specs.items()
        }
        object.__setattr__(self, "providers", providers)
        object.__setattr__(
            self, "env_vars", {name: spec.env_var for name, spec in self.specs.items()}
        )
        object.__setattr__(self, "extras", {name: spec.extra for name, spec in self.specs.items()})
        object.__setattr__(
            self,
            "api_domains",
            {name: spec.api_domains for name, spec in self.specs.items()},
        )
        object.__setattr__(
            self,
            "config_to_provider",
            {config_cls: provider_cls for provider_cls, config_cls in providers.values()},
        )

    def register(
        self,
        name: str,
        provider_cls: type,
        config_cls: type,
        *,
        env_var: str,
        extra: str | None = None,
        api_domains: tuple[str, ...] = (),
    ) -> None:
        """Register a provider; identical registration is idempotent."""
        normalized = name.strip().lower() if isinstance(name, str) else ""
        if not normalized:
            raise ValueError(f"{self.kind} provider name must be a non-empty string.")
        if not env_var:
            raise ValueError(
                f"{self.kind} provider {normalized!r} requires an env_var naming its API key."
            )
        existing = self.providers.get(normalized)
        if existing is not None:
            same_metadata = (
                self.env_vars[normalized] == env_var
                and self.extras.get(normalized, "") == (extra or "")
                and self.api_domains.get(normalized, ()) == tuple(api_domains)
            )
            if existing == (provider_cls, config_cls) and same_metadata:
                return
            raise ValueError(
                f"{self.kind} provider {normalized!r} is already registered "
                f"with a different provider/config/env_var."
            )
        self.providers[normalized] = (provider_cls, config_cls)
        self.env_vars[normalized] = env_var
        self.extras[normalized] = extra or ""
        self.api_domains[normalized] = tuple(api_domains)
        self.config_to_provider[config_cls] = provider_cls

    def discover(self) -> None:
        """Load entry-point registration callbacks once, logging failures."""
        if self._discovered or not self.entry_point_group:
            return
        object.__setattr__(self, "_discovered", True)
        for entry_point in importlib.metadata.entry_points(group=self.entry_point_group):
            try:
                register = entry_point.load()
                register()
            except Exception:
                logger.warning(
                    "Failed to load %s provider entry point %r from group %r",
                    self.kind,
                    entry_point.name,
                    self.entry_point_group,
                    exc_info=True,
                )

    def available_names(self) -> list[str]:
        """Return every registered provider name, sorted."""
        self.discover()
        return sorted(self.providers)

    def is_config_instance(self, value: object) -> bool:
        """True when ``value`` is an instance of a registered config class."""
        self.discover()
        return type(value) in self.config_to_provider

    def provider_for_config(self, config_type: type) -> Callable[..., Any]:
        """Look up the provider class implementing ``config_type``."""
        self.discover()
        provider_cls = self.config_to_provider.get(config_type)
        if provider_cls is None:
            raise ValueError(f"Unsupported {self.kind} configuration type.")
        return provider_cls

    def create_provider(
        self,
        provider: object,
        *,
        params: Mapping[str, Any] | None = None,
        api_key: str | None = None,
    ) -> Any:
        """Build a provider from its registered name and config parameters."""
        name = self.validate_name(provider)
        provider_cls, config_cls = self.providers[name]
        kwargs = dict(params or {})
        if api_key is not None:
            kwargs["api_key"] = api_key
        try:
            config = config_cls(**kwargs)
        except TypeError as exc:
            raise ValueError(
                f"Invalid params for {provider!r} {self.kind} provider: {exc}"
            ) from exc
        if not getattr(config, "api_key", None):
            raise ValueError(f"API key is required for {self.kind} provider '{provider}'")
        return provider_cls(config)

    def create_from_config(self, config: Any, event_bus: Any) -> Any:
        """Build a provider, injecting an event bus when its config declares one."""
        provider_cls = self.provider_for_config(type(config))
        if any(item.name == "event_bus" for item in fields(config)) and config.event_bus is None:
            config = replace(config, event_bus=event_bus)
        return provider_cls(config)

    def validate_name(self, provider: object) -> str:
        """Return a normalized registered name or raise ``EASYCAT_E104``."""
        from easycat.errors import EASYCAT_E104

        self.discover()
        name = provider.strip().lower() if isinstance(provider, str) else ""
        if name not in self.providers:
            available = self.available_names()
            suggestion = get_close_matches(name, available, n=1, cutoff=0.5)
            hint = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
            raise EASYCAT_E104(
                provider=provider,
                available=", ".join(available),
                hint=hint,
            )
        return name

    def parse_string(
        self, spec: str, *, api_key_overrides: Mapping[str, str] | None = None
    ) -> Any:
        """Parse ``provider/model`` and resolve its credential."""
        from easycat.errors import EASYCAT_E203

        provider, _, model_token = spec.partition("/")
        model = model_token.strip() or None
        provider = self.validate_name(provider)

        env_var = self.env_vars[provider]
        if api_key_overrides is not None and env_var in api_key_overrides:
            api_key = api_key_overrides[env_var]
        else:
            api_key = os.getenv(env_var, "")
        if not api_key:
            raise EASYCAT_E203(var=env_var)

        _, config_cls = self.providers[provider]
        kwargs: dict[str, Any] = {"api_key": api_key}
        if model:
            model_field = getattr(config_cls, "MODEL_FIELD", "model")
            kwargs[model_field] = model
        return config_cls(**kwargs)


def stt_tts_catalogs() -> tuple[ProviderCatalog, ProviderCatalog]:
    """Return the lazily imported and discovered STT/TTS catalogs."""
    from easycat.stt.factory import _CATALOG as stt_catalog
    from easycat.tts.factory import _CATALOG as tts_catalog

    stt_catalog.discover()
    tts_catalog.discover()
    return (stt_catalog, tts_catalog)


def provider_names() -> frozenset[str]:
    """Every registered STT/TTS provider name, merged across catalogs."""
    return frozenset(name for catalog in stt_tts_catalogs() for name in catalog.providers)


def provider_env_vars() -> dict[str, str]:
    """Provider → API-key env var, merged across the STT and TTS catalogs."""
    return {
        name: env_var
        for catalog in stt_tts_catalogs()
        for name, env_var in catalog.env_vars.items()
    }


def provider_extras() -> dict[str, str]:
    """Provider → optional install extra, merged across the STT and TTS catalogs."""
    return {
        name: extra for catalog in stt_tts_catalogs() for name, extra in catalog.extras.items()
    }


def credential_env_vars() -> dict[str, str]:
    """Provider → env var, deduplicated by credential."""
    merged = provider_env_vars()
    deduped: dict[str, str] = {}
    claimed_vars: set[str] = set()
    for provider in sorted(merged):
        env_var = merged[provider]
        if env_var in claimed_vars:
            continue
        deduped[provider] = env_var
        claimed_vars.add(env_var)
    return deduped


def sensitive_api_domains() -> tuple[str, ...]:
    """Sorted union of every provider API domain, for URL redaction."""
    return tuple(
        sorted(
            {
                domain
                for catalog in stt_tts_catalogs()
                for domains in catalog.api_domains.values()
                for domain in domains
            }
        )
    )
