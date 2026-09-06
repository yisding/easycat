"""Provider planner core: :class:`ProviderSelection` / :class:`ProviderPlan`.

The planner resolves all seven pipeline roles WITHOUT instantiating any provider
or importing a heavy SDK, then reports missing env vars / missing extras and any
incompatible provider/transport combinations. It is the static, side-effect-free
counterpart to ``create_session``; the planner-vs-``create_session`` parity test
is the required gate that keeps the two in lock-step.

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
probe module (NOT ``require_module``, which imports).

Import weight: the catalog factories are imported LAZILY inside
:func:`build_provider_plan` so ``import easycat.planning`` itself stays free of
provider SDK imports.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, TypeGuard

from easycat.errors import SetupIssue
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

_ROLE_ORDER: tuple[Role, ...] = (
    "stt",
    "tts",
    "vad",
    "transport",
    "agent",
    "noise_reducer",
    "echo_canceller",
)


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

    ``defects`` carries coded selection defects the pure planner cannot see
    because they live on the MANIFEST rather than on a ``VoiceProfile`` (an
    unset ``bearer-env:`` reference, a phone profile with no token). It is
    populated by :func:`easycat.planning.selection.build_manifest_plan` and
    stays ``()`` for every plan built straight from an ``EasyConfig``.
    """

    profile: str
    selected: dict[str, ProviderSelection]
    missing_env: tuple[str, ...]
    missing_extras: tuple[str, ...]
    warnings: tuple[str, ...]
    defects: tuple[SetupIssue, ...] = ()

    def blocking_errors(self) -> tuple[str, ...]:
        """Return content-free blocking-error reasons (sorted, deduped).

        A blocking error is a missing required env var, a missing required
        extra for a SELECTED role, or a ``blocking`` selection defect (an
        ``incomplete_selection:[voice.<name>]`` reason). Warnings are NOT
        blocking. The reasons are deliberately content-free (role, env/extra
        name, or a manifest path — never a value) so they are safe to echo on
        ``/health/ready`` and to compare in the parity test.
        """
        reasons: list[str] = []
        reasons.extend(f"missing_env:{var}" for var in self.missing_env)
        reasons.extend(f"missing_extra:{extra}" for extra in self.missing_extras)
        reasons.extend(
            f"incomplete_selection:{issue.field}"
            for issue in self.defects
            if issue.severity == "blocking"
        )
        return tuple(reasons)

    @property
    def has_blocking_errors(self) -> bool:
        """``True`` when any selected role has a missing required env/extra.

        A ``blocking`` selection defect counts too: a phone profile with no
        ``token`` cannot serve one call, so ``/health/ready`` must be red for it
        rather than green-then-``EASYCAT_E602``-on-first-connection. A
        ``warning`` defect (an unset ``[server] auth`` reference, which
        ``VoiceServer.from_manifest`` already refuses to construct around) is
        reported but never blocking.
        """
        return bool(
            self.missing_env
            or self.missing_extras
            or any(issue.severity == "blocking" for issue in self.defects)
        )


def _module_available(module: str) -> bool:
    """Whether ``module`` can be located (find_spec, no import)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _extra_is_missing(choice: ProviderSelection) -> bool:
    """Whether a selected provider's exact probe module is absent."""
    probe = probe_module_for_extra(
        choice.extra,
        role=choice.role,
        provider=choice.provider,
    )
    if probe is None:
        return False
    return not _module_available(probe)


# ``create_vad('auto')`` tries Silero -> FunASR -> TEN -> Krisp and only raises
# when NONE is importable. These are the probe modules for that union (Silero +
# FunASR both ride onnxruntime); the planner blocks the ``auto`` VAD only when
# all are absent. ``silero-vad`` is the extra it recommends installing.
_AUTO_VAD_PROBE_MODULES: tuple[str, ...] = ("onnxruntime", "ten_vad", "krisp_audio")
_AUTO_VAD_INSTALL_EXTRA = "silero-vad"


# ── Catalog resolution ───────────────────────────────────────────────


def _split_shortcut(spec: str) -> tuple[str, str | None]:
    """Split a ``"provider/model"`` shortcut into ``(provider, model)``."""
    provider, _, model = spec.partition("/")
    return provider.strip().lower(), (model.strip() or None)


def _select_catalog_role(
    role: Role,
    spec: Any,
    *,
    catalog: Any,
) -> ProviderSelection:
    """Resolve a provider role from the catalog WITHOUT instantiating it.

    ``spec`` is the value on the config: a shortcut string, a concrete config
    instance, or ``None``. The provider name, config-type, extra, and required
    env are read straight from the catalog metadata.
    """
    catalog.discover()
    if isinstance(spec, str):
        provider, model = _split_shortcut(spec)
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
    return ProviderSelection(
        role=role,
        provider=provider,
        model=model,
        config_type=config_type,
        extra=extra,
        required_env=required_env,
        capabilities=catalog.capabilities_for(provider, config=spec, model=model),
    )


# ── Built-in and injected role resolution ────────────────────────────


def _backend_selection(
    role: Role, backend_name: str, backend: RoleBackend, *, model: str | None = None
) -> ProviderSelection:
    return ProviderSelection(
        role=role,
        provider=backend_name,
        model=model,
        config_type=backend.config_type,
        extra=backend.extra,
        required_env=backend.required_env,
        capabilities=backend.capabilities,
    )


def _select_transport(transport: Any) -> ProviderSelection:
    """Resolve the transport role from an ``EasyConfig.transport`` instance."""
    config_type = type(transport).__name__
    backend = TRANSPORT_BACKENDS_BY_CONFIG_TYPE.get(config_type)
    shortcut = TRANSPORT_CONFIG_TYPE_TO_SHORTCUT.get(config_type, config_type)
    if backend is None:
        # A custom / unknown transport — declare it provider-only, no extra.
        return ProviderSelection(
            role="transport",
            provider=shortcut,
            model=None,
            config_type=config_type,
            extra=None,
            required_env=None,
            capabilities=frozenset(),
        )
    return _backend_selection("transport", shortcut, backend)


def _injected_selection(role: Role, provider: Any) -> ProviderSelection:
    """Describe a live injected provider without pretending it is a built-in."""
    provider_type = type(provider).__name__
    return ProviderSelection(
        role=role,
        provider=provider_type,
        model=None,
        config_type=provider_type,
        extra=None,
        required_env=None,
        capabilities=frozenset({"injected"}),
    )


def _select_vad(vad: Any, *, catalog: Any) -> ProviderSelection:
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
        return _select_catalog_role("vad", vad, catalog=catalog)
    if (
        vad is not None
        and not isinstance(vad, str)
        and callable(getattr(vad, "process", None))
        and callable(getattr(vad, "configure", None))
    ):
        return _injected_selection("vad", vad)

    backend_name = DEFAULT_VAD
    if isinstance(vad, str):
        provider, _model = _split_shortcut(vad)
        if provider in catalog.providers:
            return _select_catalog_role("vad", vad, catalog=catalog)
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
    selection = _backend_selection("vad", backend_name, backend)
    if backend_name == "auto":
        # ``auto`` is satisfiable by ANY backend in the create_vad union, so it
        # is only a blocking gap when none of the probe modules is importable —
        # otherwise the static ``silero-vad`` extra would falsely block a server
        # that ``create_vad`` would happily run on TEN or Krisp. Mirror the union.
        if any(_module_available(m) for m in _AUTO_VAD_PROBE_MODULES):
            selection = replace(selection, extra=None)
        else:
            selection = replace(selection, extra=_AUTO_VAD_INSTALL_EXTRA)
    return selection


def _select_noise_reducer(config: EasyConfig, *, catalog: Any) -> ProviderSelection:
    enabled = bool(config.enable_noise_reduction or config.noise_reduction is not None)
    if not enabled:
        return ProviderSelection(
            role="noise_reducer",
            provider="off",
            model=None,
            config_type="NoiseReducerConfig",
            extra=None,
            required_env=None,
            capabilities=frozenset({"disabled"}),
        )
    backend_name = DEFAULT_NOISE_REDUCER
    fallback_policy = "passthrough"
    cfg = config.noise_reduction
    if cfg is not None:
        if catalog.is_config_instance(cfg):
            return _select_catalog_role("noise_reducer", cfg, catalog=catalog)
        if isinstance(cfg, str):
            provider, _model = _split_shortcut(cfg)
            if provider in catalog.providers:
                return _select_catalog_role("noise_reducer", cfg, catalog=catalog)
            backend_name = provider
        elif callable(getattr(cfg, "process", None)):
            return _injected_selection("noise_reducer", cfg)
        elif hasattr(cfg, "backend"):
            backend_name = cfg.backend
        fallback_policy = str(getattr(cfg, "fallback_policy", "passthrough"))
    # An UNKNOWN backend RAISES rather than silently falling back to the default
    # while keeping the bad name (the same parity rule as ``_select_vad``):
    # ``create_noise_reducer`` rejects an unknown backend (``ValueError``), so the
    # planner must too — otherwise ``/plan`` / ``/health/ready`` would report a
    # CLEAN plan for a config that crashes on the first connection.
    backend = NOISE_REDUCER_BACKENDS.get(backend_name)
    if backend is None:
        allowed = ", ".join(sorted(NOISE_REDUCER_BACKENDS))
        raise ValueError(
            f"Unknown noise reducer backend {backend_name!r}. Expected one of: {allowed}."
        )
    selection = _backend_selection("noise_reducer", backend_name, backend)
    # ``create_noise_reducer`` only degrades gracefully in AUTO mode: it tries
    # Krisp -> RNNoise and, when neither is installed, honors ``fallback_policy``
    # ("passthrough" => no-op reducer, "error" => raise). An explicit
    # ``backend="rnnoise"`` calls ``RNNoiseReducer()`` directly and still raises
    # when the extra is missing, so only auto+non-error degrades. Mirror that (as
    # ``_echo_canceller_selection`` does) so a missing ``rnnoise`` extra is a
    # WARNING — not a blocking gap — exactly when ``create_session`` would degrade
    # to passthrough rather than raise.
    if backend_name == "auto" and fallback_policy != "error":
        selection = replace(
            selection, capabilities=selection.capabilities | {"degrades_to_passthrough"}
        )
    return selection


def _echo_canceller_selection(*, enabled: bool, fallback_policy: str) -> ProviderSelection:
    """Resolve the echo-canceller role, honoring graceful passthrough fallback.

    A missing ``aec`` extra is only a BLOCKING gap when ``fallback_policy ==
    "error"``: with the default ``"passthrough"`` policy ``create_session``
    degrades to :class:`~easycat.echo_cancellation.PassthroughAEC` instead of
    raising (see ``create_echo_canceller``), so the planner tags the selection
    with the ``"degrades_to_passthrough"`` capability and
    :func:`build_provider_plan` reports a missing extra as a WARNING rather than
    blocking ``/health/ready`` for an otherwise-deployable browser server.
    """
    backend_name = "livekit" if enabled else DEFAULT_ECHO_CANCELLER
    backend = ECHO_CANCELLER_BACKENDS[backend_name]
    capabilities = backend.capabilities
    if enabled and fallback_policy != "error":
        capabilities = capabilities | {"degrades_to_passthrough"}
    return ProviderSelection(
        role="echo_canceller",
        provider=backend_name,
        model=None,
        config_type=backend.config_type,
        extra=backend.extra,
        required_env=backend.required_env,
        capabilities=capabilities,
    )


def _select_echo_canceller(config: EasyConfig, *, catalog: Any) -> ProviderSelection:
    cfg = config.echo_cancellation
    if catalog.is_config_instance(cfg):
        return _select_catalog_role("echo_canceller", cfg, catalog=catalog)
    if isinstance(cfg, str):
        provider, _model = _split_shortcut(cfg)
        if provider in catalog.providers:
            return _select_catalog_role("echo_canceller", cfg, catalog=catalog)
    if (
        cfg is not None
        and callable(getattr(cfg, "process", None))
        and callable(getattr(cfg, "feed_reference", None))
    ):
        return _injected_selection("echo_canceller", cfg)
    enabled = bool(getattr(cfg, "enabled", False))
    fallback_policy = str(getattr(cfg, "fallback_policy", "passthrough"))
    return _echo_canceller_selection(enabled=enabled, fallback_policy=fallback_policy)


def _select_agent(config: EasyConfig) -> ProviderSelection:
    backend_name = "python" if config.agent is not None else DEFAULT_AGENT
    backend = AGENT_BACKENDS[backend_name]
    return _backend_selection("agent", backend_name, backend)


# ── Incompatibility detection ────────────────────────────────────────


def _incompatibility_warnings(selected: Mapping[str, ProviderSelection]) -> tuple[str, ...]:
    """Detect incompatible provider/transport combos (parity-anchored).

    Conservative by design: a combo is only flagged when there is a real
    constraint in the tree. The parity test is the arbiter — anything
    ``create_session`` tolerates is at most a warning, never a blocking error.
    """
    warnings: list[str] = []
    transport = selected.get("transport")
    stt = selected.get("stt")
    # Telephony is 8 kHz mu-law; an STT/TTS pinned to a non-telephony sample rate
    # is auto-aligned by ``align_tts_config_to_transport``, so it is at most a
    # note. We surface a single conservative compatibility note rather than
    # second-guessing the aligner.
    if transport is not None and transport.provider == "twilio" and stt is not None:
        warnings.append("transport_twilio_audio_format_auto_aligned")
    return tuple(warnings)


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
    env = dict(environ) if environ is not None else dict(os.environ)

    from easycat.echo_cancellation import _CATALOG as echo_canceller_catalog
    from easycat.noise_reduction import _CATALOG as noise_reducer_catalog
    from easycat.stt.factory import _CATALOG as stt_catalog
    from easycat.tts.factory import _CATALOG as tts_catalog
    from easycat.vad.factory import _CATALOG as vad_catalog

    selected: dict[str, ProviderSelection] = {}
    if _is_voice_profile(config):
        selected = _select_from_profile(
            config,
            stt_catalog=stt_catalog,
            tts_catalog=tts_catalog,
            vad_catalog=vad_catalog,
        )
    else:
        easy_config: EasyConfig = config  # type: ignore[assignment]
        selected["stt"] = _select_catalog_role("stt", easy_config.stt, catalog=stt_catalog)
        selected["tts"] = _select_catalog_role("tts", easy_config.tts, catalog=tts_catalog)
        selected["vad"] = _select_vad(easy_config.vad, catalog=vad_catalog)
        selected["transport"] = _select_transport(easy_config.transport)
        selected["agent"] = _select_agent(easy_config)
        selected["noise_reducer"] = _select_noise_reducer(
            easy_config, catalog=noise_reducer_catalog
        )
        selected["echo_canceller"] = _select_echo_canceller(
            easy_config, catalog=echo_canceller_catalog
        )

    missing_env: set[str] = set()
    missing_extras: set[str] = set()
    degraded_extras: list[str] = []
    for role in _ROLE_ORDER:
        choice = selected[role]
        if choice.required_env and not env.get(choice.required_env):
            missing_env.add(choice.required_env)
        if _extra_is_missing(choice):
            assert choice.extra is not None
            # A role that degrades gracefully when its extra is absent (the AEC
            # passthrough fallback) is a WARNING, not a blocking gap:
            # ``create_session`` still runs, so ``/health/ready`` must stay
            # ready. Anything ``create_session`` would refuse stays blocking.
            if "degrades_to_passthrough" in choice.capabilities:
                degraded_extras.append(f"{choice.role}_extra_{choice.extra}_missing_degraded")
            else:
                missing_extras.add(choice.extra)

    warnings = _incompatibility_warnings(selected) + tuple(sorted(degraded_extras))

    return ProviderPlan(
        profile=profile,
        selected=selected,
        missing_env=tuple(sorted(missing_env)),
        missing_extras=tuple(sorted(missing_extras)),
        warnings=warnings,
    )


def _select_catalog_string(
    role: Role, spec: str | None, *, catalog: Any, default_provider: str
) -> ProviderSelection:
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
        provider, model = _split_shortcut(spec)
        provider = catalog.validate_name(provider)
    _, config_cls = catalog.providers[provider]
    return ProviderSelection(
        role=role,
        provider=provider,
        model=model,
        config_type=config_cls.__name__,
        extra=catalog.extras.get(provider) or None,
        required_env=catalog.env_vars.get(provider),
        capabilities=catalog.capabilities_for(provider, model=model),
    )


def _select_from_profile(
    profile: VoiceProfile,
    *,
    stt_catalog: Any,
    tts_catalog: Any,
    vad_catalog: Any,
) -> dict[str, ProviderSelection]:
    """Resolve all seven roles DIRECTLY from a manifest ``VoiceProfile``.

    Reads the profile's raw shortcut strings (stt/tts/vad/transport/agent)
    without constructing an ``EasyConfig`` (which would call ``parse_stt_string``
    and raise on a missing key). The default-to-OpenAI behavior mirrors
    ``EasyConfig``: an unset stt defaults to ``openai-realtime``, an unset tts to
    ``openai``.
    """
    selected: dict[str, ProviderSelection] = {}
    selected["stt"] = _select_catalog_string(
        "stt", profile.stt, catalog=stt_catalog, default_provider="openai-realtime"
    )
    selected["tts"] = _select_catalog_string(
        "tts", profile.tts, catalog=tts_catalog, default_provider="openai"
    )
    selected["vad"] = _select_vad(profile.vad, catalog=vad_catalog)

    # Transport: map the manifest shortcut to its backend.
    transport_backend = TRANSPORT_BACKENDS.get(profile.transport)
    if transport_backend is not None:
        selected["transport"] = _backend_selection(
            "transport", profile.transport, transport_backend
        )
    else:
        selected["transport"] = ProviderSelection(
            role="transport",
            provider=profile.transport,
            model=None,
            config_type=profile.transport,
            extra=None,
            required_env=None,
            capabilities=frozenset(),
        )

    # Agent: present in the manifest -> the python resolver backend.
    agent_backend = AGENT_BACKENDS["python" if profile.agent is not None else DEFAULT_AGENT]
    selected["agent"] = _backend_selection(
        "agent", "python" if profile.agent is not None else DEFAULT_AGENT, agent_backend
    )

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
    selected["echo_canceller"] = _echo_canceller_selection(
        enabled=echo_enabled, fallback_policy="passthrough"
    )
    selected["noise_reducer"] = ProviderSelection(
        role="noise_reducer",
        provider="off",
        model=None,
        config_type="NoiseReducerConfig",
        extra=None,
        required_env=None,
        capabilities=frozenset({"disabled"}),
    )
    return selected


def _is_voice_profile(config: Any) -> TypeGuard[VoiceProfile]:
    """Whether ``config`` is a manifest :class:`VoiceProfile`."""
    from easycat.project.schema import VoiceProfile

    return isinstance(config, VoiceProfile)


__all__ = [
    "ProviderPlan",
    "ProviderSelection",
    "Role",
    "build_provider_plan",
]
