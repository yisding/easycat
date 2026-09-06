"""Declarative metadata for built-in non-STT/TTS planner backends.

Transport and agent resolution are table-driven. VAD, noise reduction, and echo
cancellation retain table metadata for their built-in fallback chains while
third-party implementations use
:class:`~easycat._provider_catalog.ProviderCatalog`. This module lets the
planner report built-in ``extra`` / ``required_env`` / ``capabilities`` without
instantiating a provider.

Capabilities are declared NET-NEW here. ``validation/provider_capabilities.py``
is a LIVE-derived report (it probes a running provider), NOT a static table, so
the planner must not import it — the static capability sets below are the plan's
source of truth.

The catalog stores the pyproject EXTRA NAME (e.g. ``"silero-vad"``), which is
not an importable module. :data:`EXTRA_PROBE_MODULE` maps every extra name
(stt/tts catalog extras plus the five roles here) to an importable probe module
so :func:`importlib.util.find_spec` works uniformly — the planner detects a
missing extra via ``find_spec`` (no import, no side effects), never
``require_module`` (which imports).

Import weight: this module pulls only ``dataclasses`` / ``typing``. It does NOT
import ``EasyConfig`` / ``create_session`` / aiohttp / any heavy provider SDK,
so ``import easycat.planning`` stays light and side-effect-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── Extra name -> importable probe module ────────────────────────────
#
# The catalog (and the tables below) store the pyproject EXTRA NAME. To detect a
# missing extra via ``importlib.util.find_spec`` we need an importable module
# name. Providers that need only core dependencies (OpenAI/Deepgram/
# ElevenLabs/Cartesia use ``websockets``/``httpx`` directly; Krisp ships no
# PyPI package) map to ``None`` — there is no additional runtime import to
# probe, so they are never reported as a missing extra.
EXTRA_PROBE_MODULE: dict[str, str | None] = {
    # STT/TTS catalog extras.
    "openai": None,
    "deepgram": None,
    "elevenlabs": None,
    "cartesia": None,
    # Transport extras.
    "webrtc": "aiortc",
    "telephony": "twilio",
    # ``telnyx`` reuses the webrtc/webtransport ``cryptography`` install, so a
    # shared wheel may already satisfy it (same overlap heuristic as webrtc).
    "telnyx": "cryptography",
    "local": "sounddevice",
    "webtransport": "aioquic",
    # VAD extras.
    "silero-vad": "onnxruntime",
    "ten-vad": "ten_vad",
    "funasr-vad": "onnxruntime",
    # Noise reduction.
    "rnnoise": "pyrnnoise",
    # Echo cancellation.
    "aec": "livekit",
}


@dataclass(frozen=True)
class RoleBackend:
    """Declarative metadata for one built-in planner backend.

    ``config_type`` is the dataclass/preset name the role resolves to in
    ``create_session`` (e.g. ``"WebRTCTransportConfig"`` / ``"VADConfig"``);
    ``extra`` is the optional pyproject install extra (``None`` when no extra is
    needed); ``required_env`` is the env var the backend needs (``None`` for the
    local/offline backends here); ``capabilities`` is the NET-NEW declared
    capability set; ``probe_module`` names the SDK a backend needs when it has
    no pip extra at all (a commercial SDK), so the planner can still report that
    ``create_session`` would refuse to build it.
    """

    config_type: str
    extra: str | None = None
    required_env: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    # The importable module this backend needs even when it has NO pip extra; the
    # planner reports a selected backend whose probe is absent as an unbuildable
    # gap (``missing_backends``) rather than a missing extra. This mirrors the
    # concept ``ProviderCatalog.probe_modules`` already exposes for registered
    # providers. A backend that DOES declare an ``extra`` keeps the ordinary
    # missing-extra path (its probe comes from :func:`probe_module_for_extra`), so
    # this field is only read when ``extra is None``. ``None`` means the backend
    # needs no import at all — see
    # ``tests/planning/test_transport_registry.py::
    # test_every_backend_without_an_extra_declares_a_probe_or_needs_none``, which
    # lists the entries that legitimately need none.
    probe_module: str | None = None
    # For TRANSPORT backends only: the AEC default the MANIFEST path resolves for
    # this transport — i.e. what ``ProjectManifest.to_easyconfig`` (and thus
    # ``create_session``) ends up with, which is the transport's preset value, NOT
    # always the bare transport-config ClassVar. (``WebRTCTransportConfig``'s own
    # ClassVar is False, but a manifest ``webrtc`` profile routes through
    # ``EasyConfig.browser`` which forces AEC on, so this is True.) Read by
    # ``_resolution.resolve_from_profile``; tied to the resolved EasyConfig by
    # ``test_transport_aec_defaults_match_manifest_resolved_easyconfig`` so it
    # cannot silently drift from the preset. Ignored for non-transport roles.
    default_echo_cancellation_enabled: bool = False


# ── TRANSPORT ────────────────────────────────────────────────────────
#
# Keyed by the manifest transport shortcut (``schema.TRANSPORT_SHORTCUTS``). The
# config-type names mirror ``config/_factory.py::_transport_factories`` dispatch.
# ``webrtc`` needs the ``webrtc`` extra (aiortc + aiohttp); ``websocket`` is
# stdlib (``websockets`` is a core dep, no extra); ``twilio`` needs
# ``telephony``; ``telnyx`` needs ``telnyx``; ``local`` needs ``local``
# (sounddevice).
TRANSPORT_BACKENDS: dict[str, RoleBackend] = {
    "webrtc": RoleBackend(
        config_type="WebRTCTransportConfig",
        extra="webrtc",
        capabilities=frozenset({"browser", "duplex_audio", "echo_loopback"}),
        default_echo_cancellation_enabled=True,
    ),
    "websocket": RoleBackend(
        config_type="WebSocketTransportConfig",
        extra=None,
        capabilities=frozenset({"server", "duplex_audio"}),
        default_echo_cancellation_enabled=False,
    ),
    "twilio": RoleBackend(
        config_type="TwilioTransportConfig",
        extra="telephony",
        capabilities=frozenset({"telephony", "mulaw", "8khz"}),
        default_echo_cancellation_enabled=False,
    ),
    "telnyx": RoleBackend(
        config_type="TelnyxTransportConfig",
        extra="telnyx",
        capabilities=frozenset({"telephony", "l16", "16khz"}),
        default_echo_cancellation_enabled=False,
    ),
    "local": RoleBackend(
        config_type="LocalTransportConfig",
        extra="local",
        capabilities=frozenset({"microphone", "duplex_audio"}),
        default_echo_cancellation_enabled=True,
    ),
}

# Default transport when a profile/config does not pin one — ``EasyConfig``
# defaults to the local microphone transport.
DEFAULT_TRANSPORT = "local"

# Reverse map: concrete transport config-type name -> shortcut. Lets the planner
# resolve a transport selection from an ``EasyConfig.transport`` instance
# (whose type name it reads) back to its declarative backend entry.
TRANSPORT_CONFIG_TYPE_TO_SHORTCUT: dict[str, str] = {
    backend.config_type: shortcut for shortcut, backend in TRANSPORT_BACKENDS.items()
}
# WebTransport is a config-type the session factory dispatches but has no
# manifest shortcut; declare it so a config carrying it still resolves.
TRANSPORT_CONFIG_TYPE_TO_SHORTCUT["WebTransportTransportConfig"] = "webtransport"
TRANSPORT_BACKENDS_BY_CONFIG_TYPE: dict[str, RoleBackend] = {
    backend.config_type: backend for backend in TRANSPORT_BACKENDS.values()
}
TRANSPORT_BACKENDS_BY_CONFIG_TYPE["WebTransportTransportConfig"] = RoleBackend(
    config_type="WebTransportTransportConfig",
    extra="webtransport",
    capabilities=frozenset({"browser", "duplex_audio"}),
    default_echo_cancellation_enabled=True,
)


# ── VAD ──────────────────────────────────────────────────────────────
#
# Backends mirror ``vad/_base.py::VADBackend`` Literal. ``auto`` tries silero
# first (its extra is the planner's reported extra for ``auto`` so a no-VAD
# install is flagged); ``silero``/``funasr`` need an onnxruntime extra;
# ``ten`` needs ``ten-vad``; ``krisp`` is a commercial SDK with no PyPI extra, so
# it declares a ``probe_module`` instead and a missing SDK is reported as an
# unbuildable backend rather than a missing extra.
VAD_BACKENDS: dict[str, RoleBackend] = {
    "auto": RoleBackend(
        config_type="VADConfig",
        extra="silero-vad",
        capabilities=frozenset({"endpointing", "auto_fallback"}),
    ),
    "silero": RoleBackend(
        config_type="VADConfig",
        extra="silero-vad",
        capabilities=frozenset({"endpointing", "bundled_model"}),
    ),
    "funasr": RoleBackend(
        config_type="VADConfig",
        extra="funasr-vad",
        capabilities=frozenset({"endpointing"}),
    ),
    "ten": RoleBackend(
        config_type="VADConfig",
        extra="ten-vad",
        capabilities=frozenset({"endpointing"}),
    ),
    "krisp": RoleBackend(
        config_type="VADConfig",
        extra=None,
        probe_module="krisp_audio",
        capabilities=frozenset({"endpointing", "commercial"}),
    ),
}
DEFAULT_VAD = "auto"


# ── NOISE_REDUCER ────────────────────────────────────────────────────
#
# Backends mirror ``noise_reduction.py::NoiseReducerBackend``. ``auto`` reports
# the ``rnnoise`` extra (the open-source fallback); ``krisp`` is commercial and
# declares a ``probe_module`` for the SDK it has no pip extra for.
NOISE_REDUCER_BACKENDS: dict[str, RoleBackend] = {
    "auto": RoleBackend(
        config_type="NoiseReducerConfig",
        extra="rnnoise",
        capabilities=frozenset({"noise_reduction", "auto_fallback"}),
    ),
    "rnnoise": RoleBackend(
        config_type="NoiseReducerConfig",
        extra="rnnoise",
        capabilities=frozenset({"noise_reduction", "open_source"}),
    ),
    "krisp": RoleBackend(
        config_type="NoiseReducerConfig",
        extra=None,
        probe_module="krisp_audio",
        capabilities=frozenset({"noise_reduction", "commercial"}),
    ),
}
DEFAULT_NOISE_REDUCER = "auto"


# ── ECHO_CANCELLER ───────────────────────────────────────────────────
#
# A single LiveKit-backed backend behind the ``aec`` extra, plus the passthrough
# (no-op) default when AEC is disabled. ``EchoCancellationConfig`` is the
# config-type ``create_session`` resolves.
ECHO_CANCELLER_BACKENDS: dict[str, RoleBackend] = {
    "passthrough": RoleBackend(
        config_type="EchoCancellationConfig",
        extra=None,
        capabilities=frozenset({"passthrough"}),
    ),
    "livekit": RoleBackend(
        config_type="EchoCancellationConfig",
        extra="aec",
        capabilities=frozenset({"echo_cancellation", "webrtc_aec3"}),
    ),
}
DEFAULT_ECHO_CANCELLER = "passthrough"


# ── AGENT ────────────────────────────────────────────────────────────
#
# The ``python:module:function`` resolver is itself net-new (M6a). The agent
# role carries no env/extra by default (the user's agent module pulls whatever
# framework extra it needs); capabilities are declared net-new.
AGENT_BACKENDS: dict[str, RoleBackend] = {
    "python": RoleBackend(
        config_type="python:module:function",
        extra=None,
        required_env=None,
        capabilities=frozenset({"custom_agent"}),
    ),
    "none": RoleBackend(
        config_type="NoopAgent",
        extra=None,
        required_env=None,
        capabilities=frozenset({"noop"}),
    ),
}
DEFAULT_AGENT = "none"


# The five roles with built-in metadata here. Registered VAD/noise/AEC
# extensions supplement (rather than replace) these built-in tables.
BUILTIN_BACKEND_ROLES: tuple[str, ...] = (
    "transport",
    "vad",
    "agent",
    "noise_reducer",
    "echo_canceller",
)
# Backward-compatible name from before audio-stage catalogs were introduced.
NON_CATALOG_ROLES = BUILTIN_BACKEND_ROLES


def probe_module_for_extra(
    extra: str | None,
    *,
    role: str | None = None,
    provider: str | None = None,
) -> str | None:
    """Return the importable probe module for ``extra`` (or ``None``).

    ``None`` means there is nothing to probe — either no extra is required, or
    the extra is an empty-dependency marker (deepgram/elevenlabs/cartesia/krisp)
    that installs no importable package. The planner treats a ``None`` probe as
    "extra is always satisfied" so it never falsely reports it missing.

    ``role`` plus ``provider`` preserves the selected catalog entry's identity
    when two roles happen to reuse an extra name. Generic callers retain the
    built-in role mapping first, then catalog metadata, then the historical
    name-as-module fallback.
    """
    if extra is None:
        return None
    from easycat._provider_registry import provider_catalogs, provider_probe_modules

    catalog_kind = (
        {
            "stt": "STT",
            "tts": "TTS",
            "vad": "VAD",
            "noise_reducer": "noise reducer",
            "echo_canceller": "echo canceller",
        }.get(role)
        if role is not None
        else None
    )
    if catalog_kind is not None and provider is not None:
        catalog = next(
            (candidate for candidate in provider_catalogs() if candidate.kind == catalog_kind),
            None,
        )
        if catalog is not None and catalog.extras.get(provider) == extra:
            declared_probe = catalog.probe_modules.get(provider)
            if declared_probe is not None:
                return declared_probe

    if extra in EXTRA_PROBE_MODULE:
        return EXTRA_PROBE_MODULE[extra]

    declared_probe = provider_probe_modules().get(extra)
    if declared_probe is not None:
        return declared_probe
    return extra


__all__ = [
    "AGENT_BACKENDS",
    "BUILTIN_BACKEND_ROLES",
    "DEFAULT_AGENT",
    "DEFAULT_ECHO_CANCELLER",
    "DEFAULT_NOISE_REDUCER",
    "DEFAULT_TRANSPORT",
    "DEFAULT_VAD",
    "ECHO_CANCELLER_BACKENDS",
    "EXTRA_PROBE_MODULE",
    "NOISE_REDUCER_BACKENDS",
    "NON_CATALOG_ROLES",
    "TRANSPORT_BACKENDS",
    "TRANSPORT_BACKENDS_BY_CONFIG_TYPE",
    "TRANSPORT_CONFIG_TYPE_TO_SHORTCUT",
    "VAD_BACKENDS",
    "RoleBackend",
    "probe_module_for_extra",
]
