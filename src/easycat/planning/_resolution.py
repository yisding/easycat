"""One pure resolution path for the seven pipeline roles.

Pure means: no provider construction, no SDK import, no network call, no
resource allocation, and no read of process state that is not passed in through
:class:`ProbeEnvironment`.

TRUST BOUNDARY — third-party discovery. Resolving a catalog role calls
``ProviderCatalog.discover()`` (``easycat/_provider_catalog.py``), which loads
and executes entry-point registration callbacks from installed third-party
distributions. That is arbitrary code owned by whoever the operator installed;
it runs once per process and it is the ONE side effect on this path. Built-in
role resolution stays pure; discovery is the documented exception. This is
pre-existing behaviour (the planner has always resolved catalog roles this way)
— named here so it is not mistaken for purity.

This module is private: it is not in ``easycat.planning.__all__`` and is not
exported anywhere. :mod:`easycat.planning.provider_plan` projects the
:class:`ResolvedConfiguration` it returns into the public
``ProviderSelection`` / ``ProviderPlan`` shapes.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from easycat._pipeline_decisions import (
    ECHO_CANCELLER_INSTANCE_METHODS,
    NOISE_REDUCER_INSTANCE_METHODS,
    VAD_INSTANCE_METHODS,
    auto_turn_from_stt_final,
    has_provider_shape,
    is_push_to_talk,
    noise_reduction_enabled,
    vad_stage_enabled,
)
from easycat.planning.transport_registry import (
    AGENT_BACKENDS,
    DEFAULT_AGENT,
    DEFAULT_ECHO_CANCELLER,
    DEFAULT_NOISE_REDUCER,
    DEFAULT_VAD,
    ECHO_CANCELLER_BACKENDS,
    NOISE_REDUCER_BACKENDS,
    TRANSPORT_BACKENDS,
    TRANSPORT_BACKENDS_BY_CONFIG_TYPE,
    TRANSPORT_CONFIG_TYPE_TO_SHORTCUT,
    VAD_BACKENDS,
    RoleBackend,
    probe_module_for_extra,
)

if TYPE_CHECKING:
    from easycat.config import EasyConfig
    from easycat.planning.provider_plan import Role
    from easycat.project.schema import VoiceProfile

_ROLE_ORDER: tuple[Role, ...] = (
    "stt",
    "tts",
    "vad",
    "transport",
    "agent",
    "noise_reducer",
    "echo_canceller",
)

# ``create_vad('auto')`` tries Silero -> FunASR -> TEN -> Krisp and only raises
# when NONE is importable. These are the probe modules for that union (Silero +
# FunASR both ride onnxruntime); the planner blocks the ``auto`` VAD only when
# all are absent. ``silero-vad`` is the extra it recommends installing.
_AUTO_VAD_PROBE_MODULES: tuple[str, ...] = ("onnxruntime", "ten_vad", "krisp_audio")
_AUTO_VAD_INSTALL_EXTRA = "silero-vad"


def _default_module_available(name: str) -> bool:
    """Whether ``name`` can be located (``find_spec``, no import).

    Module-level so tests can patch the probe seam for code paths that build a
    plan internally (the server's ``/plan`` and ``/health/ready`` handlers) and
    therefore cannot inject a :class:`ProbeEnvironment`.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class ProbeEnvironment:
    """The explicit environment/probe snapshot resolution reads.

    Threading this through instead of reading ``os.environ`` and
    ``importlib.util.find_spec`` as process globals is what makes resolution
    testable without monkeypatching the interpreter.

    ``module_available`` is a plain callable *field* (not a method), so a frozen
    slots dataclass stores it unbound and ``self.module_available(name)`` calls
    it directly.
    """

    env: Mapping[str, str]
    module_available: Callable[[str], bool]

    @classmethod
    def from_process(cls, environ: Mapping[str, str] | None = None) -> ProbeEnvironment:
        """Snapshot ``os.environ`` (or ``environ``) and probe with ``find_spec``.

        The probe is bound by *lazy lookup* rather than by capturing the function
        object, so a ``monkeypatch.setattr`` on
        ``easycat.planning._resolution._default_module_available`` takes effect
        for plans built inside the server.
        """
        return cls(
            env=dict(environ) if environ is not None else dict(os.environ),
            module_available=lambda name: _default_module_available(name),
        )

    @classmethod
    def fake(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        available: Collection[str] = (),
        unavailable: Collection[str] = (),
        default: bool = False,
    ) -> ProbeEnvironment:
        """Snapshot for tests.

        A module in ``available`` probes True, one in ``unavailable`` probes
        False, and anything else probes ``default``. Naming a module in both
        raises ``ValueError`` rather than picking silently.
        """
        present = frozenset(available)
        absent = frozenset(unavailable)
        both = sorted(present & absent)
        if both:
            raise ValueError(f"Modules named both available and unavailable: {', '.join(both)}.")

        def probe(name: str) -> bool:
            if name in present:
                return True
            if name in absent:
                return False
            return default

        return cls(env=dict(env or {}), module_available=probe)


@dataclass(frozen=True, slots=True)
class RoleDecision:
    """The resolved verdict for a single role, before public projection.

    Carries everything :class:`~easycat.planning.provider_plan.ProviderSelection`
    carries plus the decisions the public projection does not expose.
    """

    role: Role
    provider: str
    model: str | None
    config_type: str
    extra: str | None
    required_env: str | None
    capabilities: frozenset[str]
    # Decisions the public projection does not carry.
    enabled: bool = True
    """``False`` => construction builds nothing at all for this role."""
    probe_module: str | None = None
    """SDK probe module even when ``extra`` is ``None``."""
    spec: Any = field(default=None, repr=False, compare=False)
    """The caller's live provider object or config, held BY IDENTITY.

    ``repr=False, compare=False`` on purpose: this field may hold a
    credential-bearing config, so it must never reach a repr, a JSON payload, or
    an equality comparison. That is the secrets constraint enforced structurally
    rather than by review.
    """


@dataclass(frozen=True, slots=True)
class ResolvedConfiguration:
    """Every role decision plus the pipeline booleans, resolved once."""

    profile: str
    roles: Mapping[Role, RoleDecision]
    auto_turn_from_stt_final: bool
    enable_vad: bool
    enable_noise_reduction: bool
    echo_canceller_selected: bool
    """Whether the planner selected an *active* canceller (not the passthrough).

    Deliberately NOT named ``enable_echo_cancellation``: that is the
    ``SessionConfig`` field, whose rule is ``isinstance``-based and reports
    ``False`` for an injected canceller and for a registered third-party AEC
    config — exactly the two cases where this field reports ``True`` (see
    :func:`easycat._pipeline_decisions.echo_cancellation_enabled` and
    ``tests/planning/test_resolution.py``). A consumer that wants the
    ``SessionConfig`` value must call that function, not read this field.
    """
    missing_env: tuple[str, ...]
    missing_extras: tuple[str, ...]
    missing_backends: tuple[str, ...]
    warnings: tuple[str, ...]


# ── Catalog resolution ───────────────────────────────────────────────


def split_shortcut(spec: str) -> tuple[str, str | None]:
    """Split a ``"provider/model"`` shortcut into ``(provider, model)``."""
    provider, _, model = spec.partition("/")
    return provider.strip().lower(), (model.strip() or None)


def _decide_catalog_role(role: Role, spec: Any, *, catalog: Any) -> RoleDecision:
    """Resolve a provider role from the catalog WITHOUT instantiating it.

    ``spec`` is the value on the config: a shortcut string, a concrete config
    instance, or ``None``. The provider name, config-type, extra, and required
    env are read straight from the catalog metadata.
    """
    catalog.discover()
    if isinstance(spec, str):
        provider, model = split_shortcut(spec)
        provider = catalog.validate_name(provider)
        _, config_cls = catalog.providers[provider]
        config_type = config_cls.__name__
    else:
        # A concrete config instance (or None). Read the provider name back from
        # the catalog's reverse map; ``None`` should not reach here (caller
        # guards it) but stays robust.
        config_cls = type(spec)
        provider = next(
            (name for name, (_p, cfg) in catalog.providers.items() if cfg is config_cls),
            config_cls.__name__,
        )
        model = getattr(spec, getattr(config_cls, "MODEL_FIELD", "model"), None)
        config_type = config_cls.__name__

    extra = catalog.extras.get(provider) or None
    required_env = catalog.env_vars.get(provider)
    return RoleDecision(
        role=role,
        provider=provider,
        model=model,
        config_type=config_type,
        extra=extra,
        required_env=required_env,
        capabilities=catalog.capabilities_for(provider, config=spec, model=model),
        spec=spec,
    )


def _decide_catalog_string(
    role: Role, spec: str | None, *, catalog: Any, default_provider: str
) -> RoleDecision:
    """Resolve an stt/tts role from a raw shortcut STRING (no env read).

    A ``None`` spec falls back to ``default_provider`` (mirroring
    ``EasyConfig``'s "default to OpenAI when unset" behavior). The provider name,
    config-type, extra, and required env come from the catalog metadata — no
    ``parse_string`` call, so no env read and no ``EASYCAT_E203``.
    """
    catalog.discover()
    if spec is None:
        provider, model = default_provider, None
    else:
        provider, model = split_shortcut(spec)
        provider = catalog.validate_name(provider)
    _, config_cls = catalog.providers[provider]
    return RoleDecision(
        role=role,
        provider=provider,
        model=model,
        config_type=config_cls.__name__,
        extra=catalog.extras.get(provider) or None,
        required_env=catalog.env_vars.get(provider),
        capabilities=catalog.capabilities_for(provider, model=model),
        spec=spec,
    )


# ── Built-in and injected role resolution ────────────────────────────


def _backend_decision(
    role: Role,
    backend_name: str,
    backend: RoleBackend,
    *,
    model: str | None = None,
    spec: Any = None,
) -> RoleDecision:
    return RoleDecision(
        role=role,
        provider=backend_name,
        model=model,
        config_type=backend.config_type,
        extra=backend.extra,
        required_env=backend.required_env,
        capabilities=backend.capabilities,
        spec=spec,
    )


def _injected_decision(role: Role, provider: Any) -> RoleDecision:
    """Describe a live injected provider without pretending it is a built-in.

    Reached through :func:`~easycat._pipeline_decisions.has_provider_shape`, the
    LOOSER of the two shared predicates, because that is the check
    ``create_vad`` / ``create_noise_reducer`` / ``create_echo_canceller``
    themselves apply — a class object satisfying the method names is handed
    straight back by them today. Tightening the planner to
    ``is_provider_instance`` would change plan output for that input without
    changing what construction does, so it belongs in a behaviour-change PR, not
    here.
    """
    provider_type = type(provider).__name__
    return RoleDecision(
        role=role,
        provider=provider_type,
        model=None,
        config_type=provider_type,
        extra=None,
        required_env=None,
        capabilities=frozenset({"injected"}),
        spec=provider,
    )


def _disabled_decision(role: Role, config_type: str) -> RoleDecision:
    """The verdict for a role the session builds nothing for."""
    return RoleDecision(
        role=role,
        provider="off",
        model=None,
        config_type=config_type,
        extra=None,
        required_env=None,
        capabilities=frozenset({"disabled"}),
        enabled=False,
    )


def _decide_transport(transport: Any) -> RoleDecision:
    """Resolve the transport role from an ``EasyConfig.transport`` instance."""
    config_type = type(transport).__name__
    backend = TRANSPORT_BACKENDS_BY_CONFIG_TYPE.get(config_type)
    shortcut = TRANSPORT_CONFIG_TYPE_TO_SHORTCUT.get(config_type, config_type)
    if backend is None:
        # A custom / unknown transport — declare it provider-only, no extra.
        return RoleDecision(
            role="transport",
            provider=shortcut,
            model=None,
            config_type=config_type,
            extra=None,
            required_env=None,
            capabilities=frozenset(),
            spec=transport,
        )
    return _backend_decision("transport", shortcut, backend, spec=transport)


def _decide_vad(vad: Any, *, catalog: Any, probe: ProbeEnvironment) -> RoleDecision:
    """Resolve the vad role from a config value (string, VADConfig, or instance).

    An UNKNOWN backend shortcut RAISES rather than silently falling back to the
    ``auto`` metadata while keeping the bad name. ``create_vad`` /
    ``ProjectManifest.to_easyconfig`` both reject an unknown VAD backend
    (``ValueError`` / ``EASYCAT_E602``), so the planner must too — otherwise
    ``/plan`` and ``/health/ready`` would report a CLEAN plan for a profile that
    crashes on the first connection, breaking the planner-vs-``create_session``
    parity contract. The readiness path catches this and renders a structured
    not-ready response (see ``VoiceServer._manifest_readiness``).
    """
    catalog.discover()
    if catalog.is_config_instance(vad):
        return _decide_catalog_role("vad", vad, catalog=catalog)
    if vad is not None and has_provider_shape(vad, VAD_INSTANCE_METHODS):
        return _injected_decision("vad", vad)

    backend_name = DEFAULT_VAD
    if isinstance(vad, str):
        provider, _model = split_shortcut(vad)
        if provider in catalog.providers:
            return _decide_catalog_role("vad", vad, catalog=catalog)
        backend_name = provider
    elif vad is not None and hasattr(vad, "backend"):
        backend_name = vad.backend
    backend = VAD_BACKENDS.get(backend_name)
    if backend is None:
        allowed = ", ".join(sorted(set(VAD_BACKENDS) | set(catalog.providers)))
        raise ValueError(
            f"Unknown VAD backend {backend_name!r} or registered provider. "
            f"Expected one of: {allowed}."
        )
    decision = _backend_decision("vad", backend_name, backend, spec=vad)
    if backend_name == "auto":
        # ``auto`` is satisfiable by ANY backend in the create_vad union, so it
        # is only a blocking gap when none of the probe modules is importable —
        # otherwise the static ``silero-vad`` extra would falsely block a server
        # that ``create_vad`` would happily run on TEN or Krisp. Mirror the union.
        if any(probe.module_available(m) for m in _AUTO_VAD_PROBE_MODULES):
            decision = replace(decision, extra=None)
        else:
            decision = replace(decision, extra=_AUTO_VAD_INSTALL_EXTRA)
    return decision


def _decide_vad_role(
    vad: Any, *, catalog: Any, probe: ProbeEnvironment, auto_turn: bool
) -> RoleDecision:
    """Resolve the VAD role, THEN report it disabled when the stage is skipped.

    ``create_session`` builds NO VAD when the STT owns endpointing
    (``config/_factory.py``'s ``enable_vad`` decision), so a planner that kept
    selecting one would block ``/health/ready`` on a VAD extra for a deployment
    that starts fine. Both sides now read the same
    :func:`easycat._pipeline_decisions.vad_stage_enabled` rule.

    Resolution runs FIRST even when the stage is skipped, so an unknown backend
    still RAISES (``_decide_vad``'s parity rule): an unresolvable profile must
    stay unresolvable regardless of who owns endpointing.
    """
    decision = _decide_vad(vad, catalog=catalog, probe=probe)
    if vad_stage_enabled(auto_turn_from_stt_final=auto_turn):
        return decision
    return replace(decision, enabled=False)


def _decide_noise_reducer(config: EasyConfig, *, catalog: Any) -> RoleDecision:
    enabled = noise_reduction_enabled(
        enable_noise_reduction=config.enable_noise_reduction,
        noise_reduction=config.noise_reduction,
    )
    if not enabled:
        return _disabled_decision("noise_reducer", "NoiseReducerConfig")
    backend_name = DEFAULT_NOISE_REDUCER
    fallback_policy = "passthrough"
    cfg = config.noise_reduction
    if cfg is not None:
        if catalog.is_config_instance(cfg):
            return _decide_catalog_role("noise_reducer", cfg, catalog=catalog)
        if isinstance(cfg, str):
            provider, _model = split_shortcut(cfg)
            if provider in catalog.providers:
                return _decide_catalog_role("noise_reducer", cfg, catalog=catalog)
            backend_name = provider
        elif has_provider_shape(cfg, NOISE_REDUCER_INSTANCE_METHODS):
            return _injected_decision("noise_reducer", cfg)
        elif hasattr(cfg, "backend"):
            backend_name = cfg.backend
        fallback_policy = str(getattr(cfg, "fallback_policy", "passthrough"))
    # An UNKNOWN backend RAISES rather than silently falling back to the default
    # while keeping the bad name (the same parity rule as ``_decide_vad``):
    # ``create_noise_reducer`` rejects an unknown backend (``ValueError``), so the
    # planner must too — otherwise ``/plan`` / ``/health/ready`` would report a
    # CLEAN plan for a config that crashes on the first connection.
    backend = NOISE_REDUCER_BACKENDS.get(backend_name)
    if backend is None:
        allowed = ", ".join(sorted(NOISE_REDUCER_BACKENDS))
        raise ValueError(
            f"Unknown noise reducer backend {backend_name!r}. Expected one of: {allowed}."
        )
    decision = _backend_decision("noise_reducer", backend_name, backend, spec=cfg)
    # ``create_noise_reducer`` only degrades gracefully in AUTO mode: it tries
    # Krisp -> RNNoise and, when neither is installed, honors ``fallback_policy``
    # ("passthrough" => no-op reducer, "error" => raise). An explicit
    # ``backend="rnnoise"`` calls ``RNNoiseReducer()`` directly and still raises
    # when the extra is missing, so only auto+non-error degrades. Mirror that (as
    # ``_echo_canceller_decision`` does) so a missing ``rnnoise`` extra is a
    # WARNING — not a blocking gap — exactly when ``create_session`` would degrade
    # to passthrough rather than raise.
    if backend_name == "auto" and fallback_policy != "error":
        decision = replace(
            decision, capabilities=decision.capabilities | {"degrades_to_passthrough"}
        )
    return decision


def _echo_canceller_decision(
    *, enabled: bool, fallback_policy: str, spec: Any = None
) -> RoleDecision:
    """Resolve the echo-canceller role, honoring graceful passthrough fallback.

    A missing ``aec`` extra is only a BLOCKING gap when ``fallback_policy ==
    "error"``: with the default ``"passthrough"`` policy ``create_session``
    degrades to :class:`~easycat.echo_cancellation.PassthroughAEC` instead of
    raising (see ``create_echo_canceller``), so the planner tags the decision
    with the ``"degrades_to_passthrough"`` capability and the projected plan
    reports a missing extra as a WARNING rather than blocking ``/health/ready``
    for an otherwise-deployable browser server.
    """
    backend_name = "livekit" if enabled else DEFAULT_ECHO_CANCELLER
    backend = ECHO_CANCELLER_BACKENDS[backend_name]
    capabilities = backend.capabilities
    if enabled and fallback_policy != "error":
        capabilities = capabilities | {"degrades_to_passthrough"}
    return RoleDecision(
        role="echo_canceller",
        provider=backend_name,
        model=None,
        config_type=backend.config_type,
        extra=backend.extra,
        required_env=backend.required_env,
        capabilities=capabilities,
        spec=spec,
    )


def _decide_echo_canceller(config: EasyConfig, *, catalog: Any) -> RoleDecision:
    cfg = config.echo_cancellation
    if catalog.is_config_instance(cfg):
        return _decide_catalog_role("echo_canceller", cfg, catalog=catalog)
    if isinstance(cfg, str):
        provider, _model = split_shortcut(cfg)
        if provider in catalog.providers:
            return _decide_catalog_role("echo_canceller", cfg, catalog=catalog)
    if cfg is not None and has_provider_shape(cfg, ECHO_CANCELLER_INSTANCE_METHODS):
        return _injected_decision("echo_canceller", cfg)
    # ``getattr``, NOT the ``isinstance`` rule of
    # ``easycat._pipeline_decisions.echo_cancellation_enabled``. This expression
    # answers a DIFFERENT question — "is the ``aec`` extra a blocking gap?" — and
    # is reached only when ``cfg`` is an ``EchoCancellationConfig`` or ``None``
    # (a registered third-party config took the catalog branch above, a live
    # canceller the injected branch). Merging the two on this spelling would
    # silently flip ``SessionConfig.enable_echo_cancellation`` for every
    # registered third-party AEC config.
    enabled = bool(getattr(cfg, "enabled", False))
    fallback_policy = str(getattr(cfg, "fallback_policy", "passthrough"))
    return _echo_canceller_decision(enabled=enabled, fallback_policy=fallback_policy, spec=cfg)


def _decide_agent(config: EasyConfig) -> RoleDecision:
    backend_name = "python" if config.agent is not None else DEFAULT_AGENT
    backend = AGENT_BACKENDS[backend_name]
    return _backend_decision("agent", backend_name, backend, spec=config.agent)


# ── Incompatibility detection ────────────────────────────────────────


def _incompatibility_warnings(roles: Mapping[Role, RoleDecision]) -> tuple[str, ...]:
    """Detect incompatible provider/transport combos (parity-anchored).

    Conservative by design: a combo is only flagged when there is a real
    constraint in the tree. The parity test is the arbiter — anything
    ``create_session`` tolerates is at most a warning, never a blocking error.
    """
    warnings: list[str] = []
    transport = roles.get("transport")
    stt = roles.get("stt")
    # Telephony is 8 kHz mu-law; an STT/TTS pinned to a non-telephony sample rate
    # is auto-aligned by ``align_tts_config_to_transport``, so it is at most a
    # note. We surface a single conservative compatibility note rather than
    # second-guessing the aligner.
    if transport is not None and transport.provider == "twilio" and stt is not None:
        warnings.append("transport_twilio_audio_format_auto_aligned")
    return tuple(warnings)


# ── Turn-ownership decision ──────────────────────────────────────────


def _resolved_smart_turn_enabled(config: Any) -> bool:
    """``smart_turn.enabled`` as ``create_session`` RE-DERIVES it, not as stored.

    ``EasyConfig.smart_turn`` is typed ``SmartTurnConfig | bool | None`` and
    mutating it after construction is supported, so the stored attribute is not
    the value the session runs with: ``_validate_for_session`` calls
    ``EasyConfig._renormalize_smart_turn`` before anything is built. A bare
    ``getattr(config.smart_turn, "enabled", False)`` therefore missed two
    supported spellings and reported the VAD role ``off`` for a config
    ``create_session`` then refuses to build without a VAD backend:

    * ``cfg.smart_turn = True`` — ``getattr(True, "enabled", False)`` is
      ``False``, while ``_normalize_smart_turn_config`` reads the bool.
    * ``cfg.smart_turn_sensitivity = 0.7`` — sensitivity forces
      ``enabled=True`` no matter what ``smart_turn`` holds.

    A manifest ``VoiceProfile`` carries none of these attributes, so every
    ``getattr`` misses and the profile path answers ``False``.
    """
    if getattr(config, "smart_turn_sensitivity", None) is not None:
        # ``_normalize_smart_turn_config`` forces ``enabled=True`` whenever a
        # sensitivity is supplied, and rejects the combination outright when
        # smart-turn was explicitly turned off — so sensitivity decides first.
        return True
    is_untouched_default = getattr(config, "_smart_turn_is_untouched_default", None)
    if callable(is_untouched_default) and is_untouched_default():
        # The value ``EasyConfig`` synthesized for an unset ``smart_turn``, which
        # ``_renormalize_smart_turn`` re-derives from the CURRENT stt and
        # transport at ``create_session`` time (gh-1027). Reading the stale
        # materialized default is what made a late ``cfg.stt`` switch to a
        # native-endpointing provider keep a VAD in the plan that the session no
        # longer builds. The re-derivation only turns smart-turn ON for a
        # local-microphone transport whose STT does NOT own endpointing — the
        # case where :func:`auto_turn_from_stt_final` already answers ``False``
        # — so treating an untouched default as "no override" is exactly what
        # construction resolves, without a second transport lookup here.
        return False
    smart_turn = getattr(config, "smart_turn", None)
    if isinstance(smart_turn, bool):
        return smart_turn
    return bool(getattr(smart_turn, "enabled", False))


def _decide_auto_turn(config: Any, *, stt: RoleDecision) -> bool:
    """Whether turn boundaries come from STT finals, so the VAD stage is skipped.

    The overrides are read with ``getattr`` so ONE function serves both entry
    points: a manifest ``VoiceProfile`` carries no push-to-talk / smart-turn /
    voicemail knob, so every override is absent there and the decision reduces
    to the STT's ``native_endpointing`` capability.
    """
    return auto_turn_from_stt_final(
        push_to_talk=is_push_to_talk(getattr(getattr(config, "turn_taking", None), "mode", None)),
        smart_turn_enabled=_resolved_smart_turn_enabled(config),
        voicemail_detector_enabled=bool(
            getattr(getattr(config, "telephony", None), "enable_voicemail_detector", False)
        ),
        # The capability the STT decision already carries — the same
        # ``native_endpointing`` string ``easycat.stt.factory``'s catalog gives
        # ``easycat.config.easy._stt_uses_native_endpointing`` — so there is no
        # second catalog query and no ``easycat.turn_manager`` import.
        stt_native_endpointing="native_endpointing" in stt.capabilities,
    )


# ── Gap detection ────────────────────────────────────────────────────


def _role_gap(decision: RoleDecision, probe: ProbeEnvironment) -> bool:
    """Whether a decided role's exact probe module is absent."""
    module = probe_module_for_extra(
        decision.extra,
        role=decision.role,
        provider=decision.provider,
    )
    if module is None:
        return False
    return not probe.module_available(module)


# ── Entry points ─────────────────────────────────────────────────────


def _catalogs() -> dict[str, Any]:
    """Lazily import the five provider catalogs (kept out of module scope)."""
    from easycat.echo_cancellation import _CATALOG as echo_canceller_catalog
    from easycat.noise_reduction import _CATALOG as noise_reducer_catalog
    from easycat.stt.factory import _CATALOG as stt_catalog
    from easycat.tts.factory import _CATALOG as tts_catalog
    from easycat.vad.factory import _CATALOG as vad_catalog

    return {
        "stt": stt_catalog,
        "tts": tts_catalog,
        "vad": vad_catalog,
        "noise_reducer": noise_reducer_catalog,
        "echo_canceller": echo_canceller_catalog,
    }


def resolve_from_easyconfig(
    config: EasyConfig, *, probe: ProbeEnvironment, profile: str = "default"
) -> ResolvedConfiguration:
    """Resolve all seven roles from an :class:`~easycat.config.EasyConfig`."""
    catalogs = _catalogs()
    roles: dict[Role, RoleDecision] = {}
    roles["stt"] = _decide_catalog_role("stt", config.stt, catalog=catalogs["stt"])
    roles["tts"] = _decide_catalog_role("tts", config.tts, catalog=catalogs["tts"])
    auto_turn = _decide_auto_turn(config, stt=roles["stt"])
    roles["vad"] = _decide_vad_role(
        config.vad, catalog=catalogs["vad"], probe=probe, auto_turn=auto_turn
    )
    roles["transport"] = _decide_transport(config.transport)
    roles["agent"] = _decide_agent(config)
    roles["noise_reducer"] = _decide_noise_reducer(config, catalog=catalogs["noise_reducer"])
    roles["echo_canceller"] = _decide_echo_canceller(config, catalog=catalogs["echo_canceller"])
    return _finalize(profile=profile, roles=roles, probe=probe, auto_turn=auto_turn)


def resolve_from_profile(
    spec: VoiceProfile, *, probe: ProbeEnvironment, profile: str = "default"
) -> ResolvedConfiguration:
    """Resolve all seven roles DIRECTLY from a manifest ``VoiceProfile``.

    Reads the profile's raw shortcut strings (stt/tts/vad/transport/agent)
    without constructing an ``EasyConfig`` (which would call ``parse_stt_string``
    and raise on a missing key). The default-to-OpenAI behavior mirrors
    ``EasyConfig``: an unset stt defaults to ``openai-realtime``, an unset tts to
    ``openai``.
    """
    from easycat.stt.factory import DEFAULT_STT_PROVIDER
    from easycat.tts.factory import DEFAULT_TTS_PROVIDER

    catalogs = _catalogs()
    roles: dict[Role, RoleDecision] = {}
    roles["stt"] = _decide_catalog_string(
        "stt", spec.stt, catalog=catalogs["stt"], default_provider=DEFAULT_STT_PROVIDER
    )
    roles["tts"] = _decide_catalog_string(
        "tts", spec.tts, catalog=catalogs["tts"], default_provider=DEFAULT_TTS_PROVIDER
    )
    auto_turn = _decide_auto_turn(spec, stt=roles["stt"])
    roles["vad"] = _decide_vad_role(
        spec.vad, catalog=catalogs["vad"], probe=probe, auto_turn=auto_turn
    )

    # Transport: map the manifest shortcut to its backend.
    transport_backend = TRANSPORT_BACKENDS.get(spec.transport)
    if transport_backend is not None:
        roles["transport"] = _backend_decision("transport", spec.transport, transport_backend)
    else:
        roles["transport"] = RoleDecision(
            role="transport",
            provider=spec.transport,
            model=None,
            config_type=spec.transport,
            extra=None,
            required_env=None,
            capabilities=frozenset(),
        )

    # Agent: present in the manifest -> the python resolver backend.
    agent_name = "python" if spec.agent is not None else DEFAULT_AGENT
    roles["agent"] = _backend_decision("agent", agent_name, AGENT_BACKENDS[agent_name])

    # Noise reduction / echo cancellation: the manifest has no knob for these, so
    # the transport's create_session default drives them. ``create_session``
    # auto-enables AEC for EVERY transport whose ``default_echo_cancellation_enabled``
    # capability is True (browser/websocket/local/webtransport — NOT just the
    # browser preset; only twilio is off) via
    # ``EasyConfig._default_echo_cancellation_for_transport``, so the planner must
    # read the SAME per-transport default or it would mis-report AEC for the
    # websocket/local profiles. The manifest has no echo-cancellation fallback
    # knob, so the auto-enabled AEC always uses the default ``passthrough`` policy
    # (matching the ``EasyConfig`` presets): a missing ``aec`` extra degrades to
    # PassthroughAEC rather than blocking readiness. Noise reduction defaults off
    # unless explicitly enabled (no manifest field -> off).
    echo_enabled = (
        transport_backend.default_echo_cancellation_enabled
        if transport_backend is not None
        else False
    )
    roles["echo_canceller"] = _echo_canceller_decision(
        enabled=echo_enabled, fallback_policy="passthrough"
    )
    roles["noise_reducer"] = _disabled_decision("noise_reducer", "NoiseReducerConfig")
    return _finalize(profile=profile, roles=roles, probe=probe, auto_turn=auto_turn)


def _finalize(
    *,
    profile: str,
    roles: dict[Role, RoleDecision],
    probe: ProbeEnvironment,
    auto_turn: bool,
) -> ResolvedConfiguration:
    """Turn decided roles into gaps, warnings and the pipeline booleans."""
    missing_env: set[str] = set()
    missing_extras: set[str] = set()
    degraded_extras: list[str] = []
    for role in _ROLE_ORDER:
        decision = roles[role]
        if not decision.enabled:
            # ``create_session`` builds nothing for a disabled role, so its
            # credential and its extra are not requirements of this deployment.
            continue
        if decision.required_env and not probe.env.get(decision.required_env):
            missing_env.add(decision.required_env)
        if _role_gap(decision, probe):
            assert decision.extra is not None
            # A role that degrades gracefully when its extra is absent (the AEC
            # passthrough fallback) is a WARNING, not a blocking gap:
            # ``create_session`` still runs, so ``/health/ready`` must stay
            # ready. Anything ``create_session`` would refuse stays blocking.
            if "degrades_to_passthrough" in decision.capabilities:
                degraded_extras.append(f"{decision.role}_extra_{decision.extra}_missing_degraded")
            else:
                missing_extras.add(decision.extra)

    return ResolvedConfiguration(
        profile=profile,
        roles=roles,
        auto_turn_from_stt_final=auto_turn,
        enable_vad=roles["vad"].enabled,
        enable_noise_reduction=roles["noise_reducer"].enabled,
        echo_canceller_selected=roles["echo_canceller"].provider != DEFAULT_ECHO_CANCELLER,
        missing_env=tuple(sorted(missing_env)),
        missing_extras=tuple(sorted(missing_extras)),
        missing_backends=(),
        warnings=_incompatibility_warnings(roles) + tuple(sorted(degraded_extras)),
    )
