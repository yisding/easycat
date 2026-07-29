"""Shared provider registration, lookup, discovery, and metadata."""

from __future__ import annotations

import importlib.metadata
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from difflib import get_close_matches
from typing import Any

from easycat._provider_domains import register_sensitive_api_domains

logger = logging.getLogger("easycat")

ProviderCapabilityResolver = Callable[[Any, str | None], frozenset[str]]


def inject_event_bus(config: Any, event_bus: Any) -> Any:
    """Return *config* with ``event_bus`` structurally injected when possible.

    "Needs an event bus" is derived from the config dataclass itself (it
    declares an ``event_bus`` field) rather than from a hand-maintained
    isinstance tuple, so any future event-bus-aware provider is included
    automatically. An ``event_bus`` already set on the config wins.
    """
    if event_bus is None:
        return config
    if not is_dataclass(config) or isinstance(config, type):
        return config
    dataclass_config: Any = config
    if any(f.name == "event_bus" for f in fields(config)) and dataclass_config.event_bus is None:
        return replace(config, event_bus=event_bus)
    return config


@dataclass(frozen=True)
class ProviderSpec:
    """Classes and discovery metadata for one provider."""

    provider_cls: Callable[..., Any]
    config_cls: type
    env_var: str | None
    extra: str
    api_domains: tuple[str, ...]
    probe_module: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    capability_resolver: ProviderCapabilityResolver | None = None


@dataclass(frozen=True)
class ProviderCatalog:
    """Open provider registry shared by audio-stage factories."""

    specs: Mapping[str, ProviderSpec]
    kind: str
    entry_point_group: str | None = None
    providers: dict[str, tuple[Callable[..., Any], type]] = field(init=False)
    env_vars: dict[str, str | None] = field(init=False)
    extras: dict[str, str] = field(init=False)
    api_domains: dict[str, tuple[str, ...]] = field(init=False)
    probe_modules: dict[str, str | None] = field(init=False)
    capabilities: dict[str, frozenset[str]] = field(init=False)
    capability_resolvers: dict[str, ProviderCapabilityResolver | None] = field(init=False)
    config_to_provider: dict[type, Callable[..., Any]] = field(init=False)
    _discovered: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        register_sensitive_api_domains(
            domain for spec in self.specs.values() for domain in spec.api_domains
        )
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
            "probe_modules",
            {name: spec.probe_module for name, spec in self.specs.items()},
        )
        object.__setattr__(
            self,
            "capabilities",
            {name: frozenset(spec.capabilities) for name, spec in self.specs.items()},
        )
        object.__setattr__(
            self,
            "capability_resolvers",
            {name: spec.capability_resolver for name, spec in self.specs.items()},
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
        env_var: str | None = None,
        extra: str | None = None,
        api_domains: tuple[str, ...] = (),
        probe_module: str | None = None,
        capabilities: frozenset[str] = frozenset(),
        capability_resolver: ProviderCapabilityResolver | None = None,
    ) -> None:
        """Register a provider; identical registration is idempotent."""
        normalized = name.strip().lower() if isinstance(name, str) else ""
        if not normalized:
            raise ValueError(f"{self.kind} provider name must be a non-empty string.")
        if env_var is not None and not env_var.strip():
            raise ValueError(
                f"{self.kind} provider {normalized!r} env_var must be non-empty or None."
            )
        existing = self.providers.get(normalized)
        if existing is not None:
            same_metadata = (
                self.env_vars[normalized] == env_var
                and self.extras.get(normalized, "") == (extra or "")
                and self.api_domains.get(normalized, ()) == tuple(api_domains)
                and self.probe_modules.get(normalized) == probe_module
                and self.capabilities.get(normalized, frozenset()) == frozenset(capabilities)
                and self.capability_resolvers.get(normalized) is capability_resolver
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
        self.probe_modules[normalized] = probe_module
        self.capabilities[normalized] = frozenset(capabilities)
        self.capability_resolvers[normalized] = capability_resolver
        self.config_to_provider[config_cls] = provider_cls
        register_sensitive_api_domains(api_domains)

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

    def capabilities_for(
        self,
        provider: str,
        *,
        config: Any = None,
        model: str | None = None,
    ) -> frozenset[str]:
        """Resolve static and config/model-dependent capabilities."""
        self.discover()
        resolved = set(self.capabilities.get(provider, ()))
        resolver = self.capability_resolvers.get(provider)
        if resolver is not None:
            resolved.update(resolver(config, model))
        return frozenset(resolved)

    def capabilities_for_config(self, config: Any) -> frozenset[str]:
        """Resolve capabilities for a registered concrete config instance."""
        self.discover()
        config_type = type(config)
        provider = next(
            (
                name
                for name, (_provider, candidate) in self.providers.items()
                if candidate is config_type
            ),
            None,
        )
        if provider is None:
            return frozenset()
        model = getattr(config, getattr(config_type, "MODEL_FIELD", "model"), None)
        return self.capabilities_for(provider, config=config, model=model)

    def create_provider(
        self,
        provider: object,
        *,
        params: Mapping[str, Any] | None = None,
        api_key: str | None = None,
        event_bus: Any = None,
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
        if self.env_vars[name] is not None and not getattr(config, "api_key", None):
            raise ValueError(f"API key is required for {self.kind} provider '{provider}'")
        return provider_cls(inject_event_bus(config, event_bus))

    def create_from_config(self, config: Any, event_bus: Any) -> Any:
        """Build a provider, injecting an event bus when its config declares one."""
        provider_cls = self.provider_for_config(type(config))
        return provider_cls(inject_event_bus(config, event_bus))

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
        api_key: str | None = None
        if env_var is not None:
            if api_key_overrides is not None and env_var in api_key_overrides:
                api_key = api_key_overrides[env_var]
            else:
                api_key = os.getenv(env_var, "")
            if not api_key:
                raise EASYCAT_E203(var=env_var)

        _, config_cls = self.providers[provider]
        kwargs: dict[str, Any] = {}
        if api_key is not None:
            kwargs["api_key"] = api_key
        if model:
            model_field = getattr(config_cls, "MODEL_FIELD", "model")
            kwargs[model_field] = model
        return config_cls(**kwargs)
