from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from easycat.tts.input import TTSInputPolicy
from easycat.validation.provider_capabilities import (
    ProviderCapabilities,
    ProviderCapabilityReport,
    ProviderIdentifier,
)

Surface = Literal["stt", "tts", "agent_bridge"]
LiveStatus = Literal[
    "not_requested",
    "expected_skip",
    "failed_missing_required_secret",
    "passed",
    "failed",
]

_TTS_DEFAULT_OUTPUT_AUDIO_FORMATS = ("pcm16/24000/mono",)
_TTS_NATIVE_MARKER_PROVIDERS = frozenset({"cartesia", "elevenlabs"})


@dataclass(frozen=True)
class ProviderSurfaceSpec:
    provider: str
    surface: Surface
    adapter: str
    protocol: str
    mode: str
    required_extra: str
    credential_env_var: str
    contract_status: str = "pass"
    schema_status: str = "unknown"
    live_canary_status: str = "required"
    live_pytest_target: str = ""
    # Public/documented default voice identifiers for TTS surfaces. Empty for
    # non-TTS surfaces (and for TTS providers that expose no catalogable voice).
    default_voices: tuple[str, ...] = ()

    @property
    def artifact_key(self) -> str:
        return f"provider_{_safe_key(self.provider)}_{_safe_key(self.surface)}"


def _registry_adapter(surface: Surface, provider: str) -> str:
    """Dotted path of the registered provider class for ``(surface, provider)``.

    Sourced from the central STT/TTS registries so the surface table never
    drifts from the runtime provider classes.
    """
    from easycat.stt.factory import _PROVIDER_TO_CONFIG as _STT_REGISTRY
    from easycat.tts.factory import _PROVIDER_TO_CONFIG as _TTS_REGISTRY

    registry = _STT_REGISTRY if surface == "stt" else _TTS_REGISTRY
    cls = registry[provider][0]
    return f"{cls.__module__}.{cls.__qualname__}"


def _registry_env_var(surface: Surface, provider: str) -> str:
    """Credential env var for ``(surface, provider)`` from the central registries."""
    from easycat.stt.factory import _PROVIDER_ENV_VAR as _STT_ENV
    from easycat.tts.factory import _PROVIDER_ENV_VAR as _TTS_ENV

    env_vars = _STT_ENV if surface == "stt" else _TTS_ENV
    env_var = env_vars[provider]
    if env_var is None:
        raise ValueError(f"{provider}/{surface} is credential-free")
    return env_var


def _default_model_api_version(surface: Surface, provider: str) -> str:
    """Resolve the model identifier from the registered config's real default."""
    if surface not in {"stt", "tts"}:
        raise ValueError(f"No provider config registry for surface {surface!r}")

    from easycat.stt.factory import _PROVIDER_TO_CONFIG as _STT_REGISTRY
    from easycat.tts.factory import _PROVIDER_TO_CONFIG as _TTS_REGISTRY

    registry = _STT_REGISTRY if surface == "stt" else _TTS_REGISTRY
    config_cls = registry[provider][1]
    config = config_cls()
    resolved_model = getattr(config, "resolved_model", None)
    if resolved_model is not None:
        return str(resolved_model)
    model_field = getattr(config_cls, "MODEL_FIELD", "model")
    model = getattr(config, model_field, None)
    if model is None:
        raise ValueError(
            f"{provider}/{surface} config {config_cls.__name__} has no resolved default model"
        )
    return str(model)


def _model_api_version(spec: ProviderSurfaceSpec) -> str:
    if spec.surface in {"stt", "tts"}:
        return _default_model_api_version(spec.surface, spec.provider)
    return spec.provider


# Validation-only provider surface metadata. The ``adapter`` and
# ``credential_env_var`` of every STT/TTS row and the model identifier emitted
# in reports are derived from the central STT/TTS registries
# (``stt/factory.py``/``tts/factory.py``) so they never drift. The remaining
# fields (protocol/mode/required_extra/pytest target/default voices/
# contract+schema status) and the entire ``agent_bridge`` row have no registry
# source and are intentionally held here as validation-only metadata.
LIVE_PROVIDER_SURFACES: tuple[ProviderSurfaceSpec, ...] = (
    ProviderSurfaceSpec(
        provider="openai",
        surface="stt",
        adapter=_registry_adapter("stt", "openai"),
        protocol="http",
        mode="batch",
        required_extra="openai",
        credential_env_var=_registry_env_var("stt", "openai"),
        schema_status="unchanged",
        live_pytest_target="tests/stt/test_stt_openai.py::test_live_openai_stt",
    ),
    ProviderSurfaceSpec(
        provider="openai-realtime",
        surface="stt",
        adapter=_registry_adapter("stt", "openai-realtime"),
        protocol="websocket",
        mode="realtime",
        required_extra="openai",
        credential_env_var=_registry_env_var("stt", "openai-realtime"),
        schema_status="unchanged",
        live_pytest_target=(
            "tests/stt/test_stt_openai_realtime.py::test_live_openai_realtime_stt"
        ),
    ),
    ProviderSurfaceSpec(
        provider="deepgram",
        surface="stt",
        adapter=_registry_adapter("stt", "deepgram"),
        protocol="websocket",
        mode="realtime",
        required_extra="deepgram",
        credential_env_var=_registry_env_var("stt", "deepgram"),
        live_pytest_target="tests/stt/test_stt_deepgram.py::test_live_deepgram_stt",
    ),
    ProviderSurfaceSpec(
        provider="elevenlabs",
        surface="stt",
        adapter=_registry_adapter("stt", "elevenlabs"),
        protocol="http/websocket",
        mode="batch+realtime",
        required_extra="elevenlabs",
        credential_env_var=_registry_env_var("stt", "elevenlabs"),
        live_pytest_target="tests/stt/test_stt_elevenlabs.py::test_live_elevenlabs_stt_realtime",
    ),
    ProviderSurfaceSpec(
        provider="cartesia",
        surface="stt",
        adapter=_registry_adapter("stt", "cartesia"),
        protocol="websocket",
        mode="realtime",
        required_extra="cartesia",
        credential_env_var=_registry_env_var("stt", "cartesia"),
        live_pytest_target="tests/stt/test_stt_cartesia.py::test_live_cartesia_stt",
    ),
    ProviderSurfaceSpec(
        provider="openai",
        surface="tts",
        adapter=_registry_adapter("tts", "openai"),
        protocol="http",
        mode="streaming",
        required_extra="openai",
        credential_env_var=_registry_env_var("tts", "openai"),
        live_pytest_target="tests/tts/test_tts_openai.py::TestOpenAITTS::test_live_openai_tts",
        default_voices=("alloy",),
    ),
    ProviderSurfaceSpec(
        provider="deepgram",
        surface="tts",
        adapter=_registry_adapter("tts", "deepgram"),
        protocol="websocket",
        mode="streaming",
        required_extra="deepgram",
        credential_env_var=_registry_env_var("tts", "deepgram"),
        live_pytest_target=(
            "tests/tts/test_tts_deepgram.py::TestDeepgramTTS::test_live_deepgram_tts"
        ),
    ),
    ProviderSurfaceSpec(
        provider="elevenlabs",
        surface="tts",
        adapter=_registry_adapter("tts", "elevenlabs"),
        protocol="http/websocket",
        mode="streaming",
        required_extra="elevenlabs",
        credential_env_var=_registry_env_var("tts", "elevenlabs"),
        live_pytest_target=(
            "tests/tts/test_tts_elevenlabs.py::TestElevenLabsTTSGeneral::test_live_elevenlabs_tts"
        ),
        default_voices=("EXAVITQu4vr4xnSDxMaL",),
    ),
    ProviderSurfaceSpec(
        provider="cartesia",
        surface="tts",
        adapter=_registry_adapter("tts", "cartesia"),
        protocol="websocket",
        mode="streaming",
        required_extra="cartesia",
        credential_env_var=_registry_env_var("tts", "cartesia"),
        live_pytest_target="tests/tts/test_tts_cartesia.py::TestCartesiaTTS::test_live_cartesia_tts",
        default_voices=("6ccbfb76-1fc6-48f7-b71d-91ac6298247b",),
    ),
    ProviderSurfaceSpec(
        provider="openai-agents",
        surface="agent_bridge",
        # No name-keyed registry for agent bridges (auto_adapt_agent uses
        # framework detection), so this row stays fully hand-maintained.
        adapter="easycat.integrations.agents.openai_agents.OpenAIAgentsBridge",
        protocol="python-sdk",
        mode="streaming",
        required_extra="openai-agents",
        credential_env_var="OPENAI_API_KEY",
        live_pytest_target=(
            "tests/e2e/test_plan_4_interruption_matrix.py::test_live_openai_agents_barge_in"
        ),
    ),
)


def select_provider_surfaces(
    *,
    providers: Sequence[str] | None = None,
    surfaces: Sequence[str] | None = None,
) -> tuple[ProviderSurfaceSpec, ...]:
    provider_filter = {
        provider.strip().lower() for provider in providers or () if provider.strip()
    }
    surface_filter = {surface.strip().lower() for surface in surfaces or () if surface.strip()}
    return tuple(
        spec
        for spec in LIVE_PROVIDER_SURFACES
        if (not provider_filter or spec.provider in provider_filter)
        and (not surface_filter or spec.surface in surface_filter)
    )


def known_live_providers() -> frozenset[str]:
    return frozenset(spec.provider for spec in LIVE_PROVIDER_SURFACES)


def known_live_surfaces() -> frozenset[str]:
    return frozenset(spec.surface for spec in LIVE_PROVIDER_SURFACES)


def build_provider_capability_report(
    spec: ProviderSurfaceSpec,
    *,
    live_checked_at: datetime,
    credential_present: bool,
    live_status: LiveStatus | str,
    failure_class: str | None = None,
    latency: Mapping[str, Any] | None = None,
) -> ProviderCapabilityReport:
    model_api_version = _model_api_version(spec)
    return ProviderCapabilityReport(
        provider=spec.provider,
        surface=spec.surface,
        adapter=spec.adapter,
        protocol=spec.protocol,
        mode=spec.mode,
        adapter_version=_adapter_version(spec),
        required_extra=spec.required_extra,
        credential_env_var=spec.credential_env_var,
        credential_env_var_present=credential_present,
        api_version=model_api_version,
        # Not provider-specific today: every live surface pins its model via
        # the model id (``api_version``) rather than a version header.
        api_version_header_behavior="provider_default",
        capabilities=_surface_capabilities(spec),
        contract_status=spec.contract_status,
        schema_status=spec.schema_status,
        status=_capability_status(live_status, failure_class),
        live_checked_at=live_checked_at,
        models=(ProviderIdentifier(model_api_version, safe=True),),
        voices=_spec_voices(spec),
        latency=latency,
        failure_class=failure_class,
    )


def _spec_voices(spec: ProviderSurfaceSpec) -> tuple[ProviderIdentifier, ...]:
    """Catalog the documented default voice identifiers for a TTS surface.

    Only TTS surfaces expose voices. The identifiers are framework-documented
    public defaults, so they are marked ``safe=True`` (redacted but preserved)
    rather than fully suppressed.
    """
    if spec.surface != "tts":
        return ()
    return tuple(ProviderIdentifier(voice, safe=True) for voice in spec.default_voices)


def _surface_capabilities(spec: ProviderSurfaceSpec) -> ProviderCapabilities:
    streaming = "streaming" in spec.mode or "realtime" in spec.mode
    if spec.surface == "stt":
        return ProviderCapabilities(
            input_audio_formats=("pcm16",),
            output_audio_formats=("text",),
            streaming=streaming,
            streaming_behavior="websocket_stream" if streaming else "http_upload",
            finalization_behavior="final_transcript_event" if streaming else "batch_result",
            markers=False,
            alignment=False,
            ssml=False,
        )
    if spec.surface == "tts":
        input_policy = TTSInputPolicy.plain_text()
        native_markers = spec.provider in _TTS_NATIVE_MARKER_PROVIDERS
        return ProviderCapabilities(
            input_audio_formats=("text",),
            output_audio_formats=_TTS_DEFAULT_OUTPUT_AUDIO_FORMATS,
            streaming=streaming,
            streaming_behavior="streamed_audio_chunks",
            finalization_behavior="audio_stream_exhaustion",
            markers=native_markers,
            alignment=native_markers,
            ssml=input_policy.supports_ssml,
            tts_input_policy=input_policy,
        )
    return ProviderCapabilities(
        streaming=streaming,
        streaming_behavior="agent_event_stream",
        finalization_behavior="agent_done_event",
        markers=False,
        alignment=False,
        ssml=False,
    )


def _capability_status(live_status: LiveStatus | str, failure_class: str | None) -> str:
    if live_status in {"passed", "pass"}:
        return "pass"
    if live_status in {"expected_skip", "skipped_missing_secret"}:
        return "expected_skip"
    if live_status == "failed_missing_required_secret":
        return "auth_failure"
    if failure_class == "provider_quota":
        return "quota_failure"
    if failure_class == "auth_or_quota":
        return "auth_failure"
    if failure_class == "provider_drift":
        return "provider_drift"
    # Any remaining status (including 'failed*', 'not_requested', or an
    # unrecognized/typo'd value) collapses to the closed-Literal 'failure'
    # rather than echoing an out-of-contract string into the status field.
    return "failure"


def _adapter_version(spec: ProviderSurfaceSpec) -> str:
    return spec.adapter.rsplit(".", maxsplit=1)[-1]


def _safe_key(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value.lower())
