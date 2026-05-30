"""Shared lookup helper for STT/TTS provider factories.

The STT and TTS factories each maintain a ``provider name → (provider
class, config class)`` map plus a sibling ``provider name → API-key env
var`` map. They differ only in the concrete provider/config types and a
couple of error labels. This module hoists their parallel machinery —
name lookups, reverse map, fuzzy-matched ``parse_string`` — into one
:class:`ProviderCatalog` value object that each factory parameterizes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any


@dataclass(frozen=True)
class ProviderCatalog:
    """Static name-to-class lookup shared by STT and TTS factories.

    ``providers`` maps the public provider name (e.g. ``"deepgram"``) to
    a ``(provider_cls, config_cls)`` pair. ``env_vars`` maps the same
    provider name to the environment variable that holds its API key —
    used by :meth:`parse_string` to auto-fill credentials when the
    caller passed a string-keyed provider shortcut.

    The ``kind`` field is a short label (``"STT"`` / ``"TTS"``) used in
    error messages so the user sees which factory rejected their input.

    Configs may set a ``MODEL_FIELD`` :data:`typing.ClassVar[str]` to
    bridge non-standard field names (e.g. ElevenLabs uses ``model_id``).
    Defaults to ``"model"`` when absent.
    """

    providers: dict[str, tuple[type, type]]
    env_vars: dict[str, str]
    kind: str
    config_to_provider: dict[type, type] = field(init=False)

    def __post_init__(self) -> None:
        provider_keys = set(self.providers)
        env_var_keys = set(self.env_vars)
        if provider_keys != env_var_keys:
            missing_env_vars = sorted(provider_keys - env_var_keys)
            unknown_env_vars = sorted(env_var_keys - provider_keys)
            details: list[str] = []
            if missing_env_vars:
                details.append(f"missing env_vars for: {', '.join(missing_env_vars)}")
            if unknown_env_vars:
                details.append(f"env_vars without providers: {', '.join(unknown_env_vars)}")
            raise ValueError(
                f"{self.kind} provider catalog keys must match env var keys; " + "; ".join(details)
            )

        # Frozen dataclasses block normal attribute assignment, so the
        # reverse map is set via object.__setattr__ — same pattern the
        # standard library uses for derived fields.
        reverse = {cfg_cls: provider_cls for provider_cls, cfg_cls in self.providers.values()}
        object.__setattr__(self, "config_to_provider", reverse)

    def available_names(self) -> list[str]:
        """Return every registered provider name, sorted."""
        return sorted(self.providers)

    def provider_for_config(self, config_type: type) -> type:
        """Look up the provider class implementing ``config_type``."""
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

    def parse_string(self, spec: str) -> Any:
        """Parse a ``"provider/model"`` (or bare ``"provider"``) shortcut.

        Looks up the provider in :attr:`providers`, reads the API key
        from :attr:`env_vars`, and instantiates the provider's config
        class. The ``model`` token is written to whichever field the
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
        api_key = os.getenv(env_var, "")
        if not api_key:
            raise EASYCAT_E203(var=env_var)

        _, config_cls = self.providers[provider]
        kwargs: dict[str, Any] = {"api_key": api_key}
        if model:
            model_field = getattr(config_cls, "MODEL_FIELD", "model")
            kwargs[model_field] = model
        return config_cls(**kwargs)
