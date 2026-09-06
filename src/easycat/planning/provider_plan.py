"""Provider planner core: :class:`ProviderSelection` / :class:`ProviderPlan`.

The planner resolves all seven pipeline roles WITHOUT instantiating any provider
or importing a heavy SDK, then reports missing env vars / missing extras and any
incompatible provider/transport combinations. It is the static, side-effect-free
counterpart to ``create_session``; the planner-vs-``create_session`` parity test
is the required gate that keeps the two in lock-step.

This module is the PROJECTION half. Every role decision is made exactly once in
:mod:`easycat.planning._resolution`, which returns a
:class:`~easycat.planning._resolution.ResolvedConfiguration`;
:func:`build_provider_plan` projects that into the public
:class:`ProviderSelection` / :class:`ProviderPlan` shapes and nothing else. The
pure decisions the resolver shares with session construction (the live-instance
predicates, the noise-reduction switch, the STT-native-endpointing turn policy)
live one layer lower again, in :mod:`easycat._pipeline_decisions`.

Metadata sourcing (per the M6b spec):

* **stt / tts** — REUSE the STT/TTS :class:`~easycat._provider_catalog.ProviderCatalog`
  metadata (``env_vars`` / ``extras`` + the per-factory config-type map). The
  provider name is parsed from the shortcut the same way ``parse_string`` splits
  it (``"provider/model"``), but WITHOUT calling ``parse_string`` (which reads the
  env, raises ``EASYCAT_E203``, and constructs a config — all side effects the
  planner must avoid).
* **vad / noise_reducer / echo_canceller** — registered third-party providers
  reuse :class:`~easycat._provider_catalog.ProviderCatalog`; built-in fallback
  chains retain their declarative metadata in
  :mod:`easycat.planning.transport_registry`.
* **transport / agent** — declarative metadata from
  :mod:`easycat.planning.transport_registry`.

Missing-env detection reads the env mapping directly (no provider construction).
Missing-extra detection uses :func:`importlib.util.find_spec` on the extra's
probe module (NOT ``require_module``, which imports). Both reach the process
only through
:class:`~easycat.planning._resolution.ProbeEnvironment`, so a test can hand
resolution an explicit snapshot instead of monkeypatching the interpreter.

Import weight: :mod:`easycat.planning._resolution` and the catalog factories are
imported LAZILY inside :func:`build_provider_plan` so ``import easycat.planning``
itself stays free of provider SDK imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeGuard

if TYPE_CHECKING:
    from easycat.config import EasyConfig
    from easycat.planning._resolution import (
        ProbeEnvironment,
        ResolvedConfiguration,
        RoleDecision,
    )
    from easycat.project.schema import VoiceProfile

Role = Literal[
    "stt",
    "tts",
    "vad",
    "transport",
    "agent",
    "noise_reducer",
    "echo_canceller",
]


@dataclass(frozen=True)
class ProviderSelection:
    """The planner's verdict for a single role (one of the seven)."""

    role: Role
    provider: str
    model: str | None
    config_type: str
    extra: str | None
    required_env: str | None
    capabilities: frozenset[str]


@dataclass(frozen=True)
class ProviderPlan:
    """The resolved plan across all seven roles.

    ``selected`` maps each role to its :class:`ProviderSelection`.
    ``missing_env`` / ``missing_extras`` are the deduped, sorted blocking gaps;
    ``warnings`` carries non-blocking notes (e.g. an incompatible-but-tolerated
    combo). A role with a missing required env var OR a missing required extra is
    a BLOCKING error — what ``/health/ready`` and the parity gate read.
    """

    profile: str
    selected: dict[str, ProviderSelection]
    missing_env: tuple[str, ...]
    missing_extras: tuple[str, ...]
    warnings: tuple[str, ...]

    def blocking_errors(self) -> tuple[str, ...]:
        """Return content-free blocking-error reasons (sorted, deduped).

        A blocking error is a missing required env var or a missing required
        extra for a SELECTED role. Warnings are NOT blocking. The reasons are
        deliberately content-free (role + env/extra name only) so they are safe
        to echo on ``/health/ready`` and to compare in the parity test.
        """
        reasons: list[str] = []
        reasons.extend(f"missing_env:{var}" for var in self.missing_env)
        reasons.extend(f"missing_extra:{extra}" for extra in self.missing_extras)
        return tuple(reasons)

    @property
    def has_blocking_errors(self) -> bool:
        """``True`` when any selected role has a missing required env/extra."""
        return bool(self.missing_env or self.missing_extras)


def selection_to_dict(selection: ProviderSelection) -> dict[str, Any]:
    """Project one :class:`ProviderSelection` to its JSON payload shape.

    The ONE such projection: ``easycat plan --json`` and the server's ``/plan``
    / ``/capabilities`` payloads both call it, so a field can never be added to
    one surface and forgotten on the other.
    """
    return {
        "role": selection.role,
        "provider": selection.provider,
        "model": selection.model,
        "config_type": selection.config_type,
        "extra": selection.extra,
        "required_env": selection.required_env,
        "capabilities": sorted(selection.capabilities),
    }


# ── Projection ───────────────────────────────────────────────────────


def _project_selection(decision: RoleDecision) -> ProviderSelection:
    """Narrow one resolved role decision to the public selection shape.

    ``RoleDecision.spec`` — which may hold a credential-bearing config or the
    caller's live provider object — is deliberately NOT copied across.
    """
    return ProviderSelection(
        role=decision.role,
        provider=decision.provider,
        model=decision.model,
        config_type=decision.config_type,
        extra=decision.extra,
        required_env=decision.required_env,
        capabilities=decision.capabilities,
    )


def _project_plan(resolved: ResolvedConfiguration) -> ProviderPlan:
    """Project a :class:`ResolvedConfiguration` into the public plan."""
    return ProviderPlan(
        profile=resolved.profile,
        selected={role: _project_selection(d) for role, d in resolved.roles.items()},
        missing_env=resolved.missing_env,
        missing_extras=resolved.missing_extras,
        warnings=resolved.warnings,
    )


# ── Public entry point ───────────────────────────────────────────────


def build_provider_plan(
    config: EasyConfig | VoiceProfile,
    *,
    environ: Mapping[str, str] | None = None,
    profile: str = "default",
) -> ProviderPlan:
    """Resolve all seven roles into a :class:`ProviderPlan` (side-effect-free).

    Accepts an :class:`~easycat.config.EasyConfig` (whose stt/tts are already
    parsed to config instances) OR a :class:`~easycat.project.schema.VoiceProfile`
    (whose stt/tts are still raw shortcut STRINGS). A ``VoiceProfile`` is read
    DIRECTLY — it is NOT coerced through ``EasyConfig``, because ``EasyConfig``
    calls ``parse_stt_string`` which reads the env and raises ``EASYCAT_E203``
    when a key is missing. The planner must report a missing key WITHOUT raising,
    so it resolves catalog roles straight from the shortcut string.
    """
    from easycat.planning._resolution import ProbeEnvironment

    return _plan_with_probe(config, probe=ProbeEnvironment.from_process(environ), profile=profile)


def _plan_with_probe(
    config: EasyConfig | VoiceProfile,
    *,
    probe: ProbeEnvironment,
    profile: str = "default",
) -> ProviderPlan:
    """Testable seam: build a plan against an explicit probe snapshot.

    ``build_provider_plan`` is this function with the process snapshot; tests
    that need a specific extra to look absent hand in
    ``ProbeEnvironment.fake(...)`` instead of monkeypatching
    :func:`importlib.util.find_spec`.
    """
    from easycat.planning._resolution import resolve_from_easyconfig, resolve_from_profile

    resolved = (
        resolve_from_profile(config, probe=probe, profile=profile)
        if _is_voice_profile(config)
        else resolve_from_easyconfig(config, probe=probe, profile=profile)  # type: ignore[arg-type]
    )
    return _project_plan(resolved)


def _is_voice_profile(config: Any) -> TypeGuard[VoiceProfile]:
    """Whether ``config`` is a manifest :class:`VoiceProfile`."""
    from easycat.project.schema import VoiceProfile

    return isinstance(config, VoiceProfile)


__all__ = [
    "ProviderPlan",
    "ProviderSelection",
    "Role",
    "build_provider_plan",
    "selection_to_dict",
]
