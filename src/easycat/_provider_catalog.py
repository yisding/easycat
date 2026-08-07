"""Shared provider registration, lookup, discovery, and metadata."""

from __future__ import annotations

import importlib.metadata
import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from difflib import get_close_matches
from typing import Any

from easycat._credentials import has_usable_credential
from easycat._provider_domains import register_sensitive_api_domains
from easycat.errors import EasyCatError, EasyConfigError

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


def _same_alias_metadata(first: ProviderSpec, second: ProviderSpec) -> bool:
    """Whether two names are interchangeable after construction loses the name."""
    return (
        first.env_var == second.env_var
        and first.extra == second.extra
        and first.api_domains == second.api_domains
        and first.probe_module == second.probe_module
        and first.capabilities == second.capabilities
        and first.capability_resolver is second.capability_resolver
    )


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
    _discovery_owner: int | None = field(init=False, default=None, repr=False, compare=False)
    _discovery_lock: threading.RLock = field(
        init=False,
        default_factory=threading.RLock,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        config_to_provider: dict[type, Callable[..., Any]] = {}
        config_to_name: dict[type, str] = {}
        for name, spec in self.specs.items():
            existing_provider = config_to_provider.get(spec.config_cls)
            if existing_provider is not None:
                existing_name = config_to_name[spec.config_cls]
                existing_spec = self.specs[existing_name]
                if existing_provider is not spec.provider_cls:
                    raise ValueError(
                        f"{self.kind} config class {spec.config_cls.__name__!r} is already "
                        f"registered for provider {existing_name!r}; it cannot also map to "
                        f"provider {name!r} with a different implementation."
                    )
                if not _same_alias_metadata(existing_spec, spec):
                    raise ValueError(
                        f"{self.kind} provider alias {name!r} shares config class "
                        f"{spec.config_cls.__name__!r} with {existing_name!r}, so both "
                        "aliases must use identical metadata."
                    )
            config_to_provider[spec.config_cls] = spec.provider_cls
            config_to_name.setdefault(spec.config_cls, name)

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
            config_to_provider,
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
        with self._discovery_lock:
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
            existing_provider = self.config_to_provider.get(config_cls)
            if existing_provider is not None:
                existing_name = next(
                    name
                    for name, (candidate_provider, candidate_config) in self.providers.items()
                    if candidate_config is config_cls and candidate_provider is existing_provider
                )
                if existing_provider is not provider_cls:
                    raise ValueError(
                        f"{self.kind} config class {config_cls.__name__!r} is already registered "
                        f"for provider {existing_name!r}; it cannot also map to provider "
                        f"{normalized!r} with a different implementation."
                    )
                alias_spec = ProviderSpec(
                    provider_cls=provider_cls,
                    config_cls=config_cls,
                    env_var=env_var,
                    extra=extra or "",
                    api_domains=tuple(api_domains),
                    probe_module=probe_module,
                    capabilities=frozenset(capabilities),
                    capability_resolver=capability_resolver,
                )
                existing_spec = ProviderSpec(
                    provider_cls=provider_cls,
                    config_cls=config_cls,
                    env_var=self.env_vars[existing_name],
                    extra=self.extras[existing_name],
                    api_domains=self.api_domains[existing_name],
                    probe_module=self.probe_modules[existing_name],
                    capabilities=self.capabilities[existing_name],
                    capability_resolver=self.capability_resolvers[existing_name],
                )
                if not _same_alias_metadata(existing_spec, alias_spec):
                    raise ValueError(
                        f"{self.kind} provider alias {normalized!r} shares config class "
                        f"{config_cls.__name__!r} with {existing_name!r}, so both aliases "
                        "must use identical metadata."
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
        with self._discovery_lock:
            if self._discovered:
                return
            owner = threading.get_ident()
            if self._discovery_owner == owner:
                return
            object.__setattr__(self, "_discovery_owner", owner)
            try:
                entry_points = importlib.metadata.entry_points(group=self.entry_point_group)
                for entry_point in entry_points:
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
            except Exception:
                # Metadata enumeration can fail independently of any one
                # callback. Do not publish a partial catalog as discovered;
                # a later call must be able to retry the whole enumeration.
                logger.warning(
                    "Failed to enumerate %s provider entry points from group %r",
                    self.kind,
                    self.entry_point_group,
                    exc_info=True,
                )
                return
            else:
                # Completion is published only after every registration
                # callback finishes, so the lock-free fast path cannot observe
                # partially populated catalog dictionaries.
                object.__setattr__(self, "_discovered", True)
            finally:
                object.__setattr__(self, "_discovery_owner", None)

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
        except EasyCatError:
            raise
        except (TypeError, ValueError) as exc:
            raise EasyConfigError(
                f"Invalid params for {provider!r} {self.kind} provider: {exc}"
            ) from exc
        if self.env_vars[name] is not None and not has_usable_credential(
            getattr(config, "api_key", None)
        ):
            raise EasyConfigError(f"API key is required for {self.kind} provider '{provider}'")
        try:
            return provider_cls(inject_event_bus(config, event_bus))
        except EasyCatError:
            raise
        except (TypeError, ValueError) as exc:
            raise EasyConfigError(
                f"Could not construct {provider!r} {self.kind} provider: {exc}"
            ) from exc

    def create_from_config(self, config: Any, event_bus: Any) -> Any:
        """Build a provider, injecting an event bus when its config declares one."""
        config_type = type(config)
        provider_cls = self.provider_for_config(config_type)
        provider_name = next(
            name
            for name, (candidate_provider, candidate_config) in self.providers.items()
            if candidate_config is config_type and candidate_provider is provider_cls
        )
        if self.env_vars[provider_name] is not None and not has_usable_credential(
            getattr(config, "api_key", None)
        ):
            raise EasyConfigError(
                f"API key is required for {self.kind} provider {provider_name!r}"
            )
        try:
            return provider_cls(inject_event_bus(config, event_bus))
        except EasyCatError:
            raise
        except (TypeError, ValueError) as exc:
            raise EasyConfigError(
                f"Could not construct {provider_name!r} {self.kind} provider: {exc}"
            ) from exc

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
            if not has_usable_credential(api_key):
                raise EASYCAT_E203(var=env_var)

        _, config_cls = self.providers[provider]
        kwargs: dict[str, Any] = {}
        if api_key is not None:
            kwargs["api_key"] = api_key
        if model:
            model_field = getattr(config_cls, "MODEL_FIELD", "model")
            kwargs[model_field] = model
        return config_cls(**kwargs)
