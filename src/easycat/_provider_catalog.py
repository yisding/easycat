"""Single source of truth for STT/TTS provider metadata.

The STT and TTS factories each maintain a ``provider name → (provider
class, config class)`` map plus sibling per-provider metadata maps (API
key env var, optional install extra, API domains). They differ only in
the concrete provider/config types and a couple of error labels. This
module hoists their parallel machinery — name lookups, reverse map,
fuzzy-matched ``parse_string``, key-completeness validation, third-party
registration, and entry-point discovery — into one
:class:`ProviderCatalog` value object that each factory parameterizes.

The module-level helpers at the bottom merge the STT and TTS catalogs so
downstream consumers (``easycat doctor``'s env checks, ``easycat init``'s
scaffold extras/env hints, validation's pytest provider markers, and
redaction's sensitive-URL regex) derive from the catalogs instead of
hand-maintaining their own provider lists.

Third-party providers join the catalog two ways:

1. Directly, via :meth:`ProviderCatalog.register` (wrapped by the public
   ``easycat.register_stt_provider`` / ``easycat.register_tts_provider``
   functions).
2. Automatically, by publishing a zero-arg callable under the catalog's
   ``entry_point_group`` (``easycat.stt_providers`` /
   ``easycat.tts_providers``). The callable is loaded and invoked once,
   at the first catalog lookup, and is expected to perform its own
   ``register_*_provider(...)`` call.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any

logger = logging.getLogger("easycat")


@dataclass(frozen=True)
class ProviderCatalog:
    """Name-to-class lookup shared by the STT and TTS factories.

    ``providers`` maps the public provider name (e.g. ``"deepgram"``) to
    a ``(provider_cls, config_cls)`` pair. The three metadata maps are
    keyed by the same provider names (enforced at construction):

    - ``env_vars`` — environment variable that holds the API key, used
      by :meth:`parse_string` to auto-fill credentials and by ``easycat
      doctor`` to know which credentials to check.
    - ``extras`` — optional install extra that ships the provider's
      dependencies, used by ``easycat init`` to scaffold
      ``pyproject.toml`` extras.
    - ``api_domains`` — API host domains the provider talks to, used by
      validation redaction to scrub provider URLs from artifacts.

    The ``kind`` field is a short label (``"STT"`` / ``"TTS"``) used in
    error messages so the user sees which factory rejected their input.

    ``entry_point_group`` names the :mod:`importlib.metadata` entry-point
    group scanned (once, lazily) for third-party providers; ``None``
    disables discovery. The catalog is open post-construction: every
    lookup method first runs :meth:`discover`, and :meth:`register`
    mutates the maps in place so module-level aliases of the same dicts
    (e.g. ``_PROVIDER_TO_CONFIG``) stay in sync.

    Configs may set a ``MODEL_FIELD`` :data:`typing.ClassVar[str]` to
    bridge non-standard field names (e.g. ElevenLabs uses ``model_id``).
    Defaults to ``"model"`` when absent.
    """

    providers: dict[str, tuple[type, type]]
    env_vars: dict[str, str]
    extras: dict[str, str]
    api_domains: dict[str, tuple[str, ...]]
    kind: str
    entry_point_group: str | None = None
    config_to_provider: dict[type, type] = field(init=False)
    _discovered: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self._validate_metadata_keys("env_vars", "env var keys", self.env_vars)
        self._validate_metadata_keys("extras", "extra keys", self.extras)
        self._validate_metadata_keys("api_domains", "api domain keys", self.api_domains)

        # Frozen dataclasses block normal attribute assignment, so the
        # reverse map is set via object.__setattr__ — same pattern the
        # standard library uses for derived fields.
        reverse = {cfg_cls: provider_cls for provider_cls, cfg_cls in self.providers.values()}
        object.__setattr__(self, "config_to_provider", reverse)

    def _validate_metadata_keys(
        self, field_name: str, label: str, mapping: Mapping[str, object]
    ) -> None:
        """Require ``mapping`` to cover exactly the registered providers."""
        provider_keys = set(self.providers)
        metadata_keys = set(mapping)
        if provider_keys == metadata_keys:
            return
        missing = sorted(provider_keys - metadata_keys)
        unknown = sorted(metadata_keys - provider_keys)
        details: list[str] = []
        if missing:
            details.append(f"missing {field_name} for: {', '.join(missing)}")
        if unknown:
            details.append(f"{field_name} without providers: {', '.join(unknown)}")
        raise ValueError(
            f"{self.kind} provider catalog keys must match {label}; " + "; ".join(details)
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
        """Register a provider under a string shortcut name.

        Validates inputs first, then updates ``providers``, ``env_vars``,
        the optional metadata maps, and the ``config_to_provider``
        reverse map together — preserving the key-parity invariant per
        registration. Re-registering an identical entry is a no-op (so
        entry-point discovery stays idempotent); a conflicting duplicate
        raises.

        ``extra`` optionally names the install extra shipping the
        provider's dependencies (surfaced by ``easycat init`` scaffold
        extras); ``api_domains`` optionally lists API host domains
        (folded into validation's URL redaction). Both default to empty,
        which simply means the provider does not surface there.
        """
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
        """Load entry-point providers once (idempotent, lazy).

        Scans :attr:`entry_point_group` via :mod:`importlib.metadata`;
        each entry point must load to a zero-arg callable that performs
        its own ``register_*_provider(...)`` call. A broken plugin logs
        a warning instead of breaking every factory call.
        """
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

    def provider_env_vars(self) -> dict[str, str]:
        """Return a copy of the provider-name → API-key-env-var map."""
        self.discover()
        return dict(self.env_vars)

    def is_config_instance(self, value: object) -> bool:
        """True when ``value`` is an instance of a registered config class.

        The open-world replacement for ``isinstance(x, STTConfig/TTSConfig)``
        union checks: membership is decided by the live catalog, so
        third-party configs registered via either layer count too.
        """
        self.discover()
        return type(value) in self.config_to_provider

    def provider_for_config(self, config_type: type) -> type:
        """Look up the provider class implementing ``config_type``."""
        self.discover()
        provider_cls = self.config_to_provider.get(config_type)
        if provider_cls is None:
            raise ValueError(f"Unsupported {self.kind} configuration type.")
        return provider_cls

    def validate_name(self, provider: object) -> str:
        """Normalize and validate a provider name against the registry.

        Returns the lowercased, registered provider name. Raises the
        shared :data:`~easycat.errors.EASYCAT_E104` (with a fuzzy-match
        ``Did you mean?`` hint) when the name is unknown — the same error
        path as :meth:`parse_string`, so the typed-config and
        string-shortcut entry points report unknown providers
        identically.

        Raises:
            EasyCatError (EASYCAT_E104): Unknown (or non-string) provider,
                with fuzzy-match suggestion.
        """
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
        """Parse a ``"provider/model"`` (or bare ``"provider"``) shortcut.

        Looks up the provider in :attr:`providers`, reads the API key
        from ``api_key_overrides`` or :attr:`env_vars`, and instantiates
        the provider's config class. The ``model`` token is written to whichever field the
        config exposes via its ``MODEL_FIELD`` class var (defaulting to
        ``"model"``).

        Raises:
            EasyCatError (EASYCAT_E104): Unknown provider, with
                fuzzy-match suggestion.
            EasyCatError (EASYCAT_E203): Missing required API key env
                var.
        """
        from easycat.errors import EASYCAT_E203

        provider, _, model = spec.partition("/")
        model = model.strip() or None
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
    """Return the (STT, TTS) catalogs, entry-point discovery included.

    Imported lazily because the factories import this module at their own
    import time — a top-level import here would be circular. Discovery is
    triggered here so every merged helper below (doctor env checks,
    scaffold extras/env hints, redaction domains) sees third-party
    providers registered via the entry-point groups.
    """
    from easycat.stt.factory import _CATALOG as stt_catalog
    from easycat.tts.factory import _CATALOG as tts_catalog

    stt_catalog.discover()
    tts_catalog.discover()
    return (stt_catalog, tts_catalog)


def provider_names() -> frozenset[str]:
    """Every registered STT/TTS provider name, merged across catalogs."""
    names: set[str] = set()
    for catalog in stt_tts_catalogs():
        names.update(catalog.providers)
    return frozenset(names)


def provider_env_vars() -> dict[str, str]:
    """Provider → API-key env var, merged across the STT and TTS catalogs."""
    merged: dict[str, str] = {}
    for catalog in stt_tts_catalogs():
        merged.update(catalog.env_vars)
    return merged


def provider_extras() -> dict[str, str]:
    """Provider → optional install extra, merged across the STT and TTS catalogs."""
    merged: dict[str, str] = {}
    for catalog in stt_tts_catalogs():
        merged.update(catalog.extras)
    return merged


def credential_env_vars() -> dict[str, str]:
    """Provider → env var with one provider per distinct credential.

    Providers that reuse another provider's credential (e.g.
    ``openai-realtime`` shares ``OPENAI_API_KEY`` with ``openai``) are
    collapsed onto the alphabetically first provider name, so ``easycat
    doctor`` checks each credential exactly once.
    """
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
    domains: set[str] = set()
    for catalog in stt_tts_catalogs():
        for provider_domains in catalog.api_domains.values():
            domains.update(provider_domains)
    return tuple(sorted(domains))
