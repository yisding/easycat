from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from easycat.validation.provider_capabilities import (
    ProviderCapabilities,
    ProviderCapabilityReport,
    ProviderIdentifier,
)

Surface = Literal["stt", "tts", "vad", "transport", "agent_bridge"]
LiveStatus = Literal[
    "not_requested",
    "expected_skip",
    "failed_missing_required_secret",
    "passed",
    "failed",
]


@dataclass(frozen=True)
class ProviderSurfaceSpec:
    provider: str
    surface: Surface
    adapter: str
    protocol: str
    mode: str
    model_api_version: str
    required_extra: str
    credential_env_var: str
    contract_status: str = "pass"
    schema_status: str = "unknown"
    live_canary_status: str = "required"
    live_pytest_target: str = ""

    @property
    def artifact_key(self) -> str:
        return f"provider_{_safe_key(self.provider)}_{_safe_key(self.surface)}"


LIVE_PROVIDER_SURFACES: tuple[ProviderSurfaceSpec, ...] = (
    ProviderSurfaceSpec(
        provider="openai",
        surface="stt",
        adapter="easycat.stt.openai_provider.OpenAISTT",
        protocol="http",
        mode="batch",
        model_api_version="whisper-1",
        required_extra="openai",
        credential_env_var="OPENAI_API_KEY",
        schema_status="unchanged",
        live_pytest_target="tests/stt/test_stt_openai.py::test_live_openai_stt",
    ),
    ProviderSurfaceSpec(
        provider="openai-realtime",
        surface="stt",
        adapter="easycat.stt.openai_realtime_provider.OpenAIRealtimeSTT",
        protocol="websocket",
        mode="realtime",
        model_api_version="gpt-4o-transcribe",
        required_extra="openai",
        credential_env_var="OPENAI_API_KEY",
        schema_status="unchanged",
        live_pytest_target=(
            "tests/stt/test_stt_openai_realtime.py::test_live_openai_realtime_stt"
        ),
    ),
    ProviderSurfaceSpec(
        provider="deepgram",
        surface="stt",
        adapter="easycat.stt.deepgram_provider.DeepgramSTT",
        protocol="websocket",
        mode="realtime",
        model_api_version="nova-3",
        required_extra="deepgram",
        credential_env_var="DEEPGRAM_API_KEY",
        live_pytest_target="tests/stt/test_stt_deepgram.py::test_live_deepgram_stt",
    ),
    ProviderSurfaceSpec(
        provider="elevenlabs",
        surface="stt",
        adapter="easycat.stt.elevenlabs_provider.ElevenLabsSTT",
        protocol="http/websocket",
        mode="batch+realtime",
        model_api_version="scribe_v1",
        required_extra="elevenlabs",
        credential_env_var="ELEVENLABS_API_KEY",
        live_pytest_target="tests/stt/test_stt_elevenlabs.py::test_live_elevenlabs_stt_realtime",
    ),
    ProviderSurfaceSpec(
        provider="cartesia",
        surface="stt",
        adapter="easycat.stt.cartesia_provider.CartesiaSTT",
        protocol="websocket",
        mode="realtime",
        model_api_version="ink-whisper",
        required_extra="cartesia",
        credential_env_var="CARTESIA_API_KEY",
        live_pytest_target="tests/stt/test_stt_cartesia.py::test_live_cartesia_stt",
    ),
    ProviderSurfaceSpec(
        provider="openai",
        surface="tts",
        adapter="easycat.tts.openai_tts.OpenAITTS",
        protocol="http",
        mode="streaming",
        model_api_version="gpt-4o-mini-tts",
        required_extra="openai",
        credential_env_var="OPENAI_API_KEY",
        live_pytest_target="tests/tts/test_tts_openai.py::TestOpenAITTS::test_live_openai_tts",
    ),
    ProviderSurfaceSpec(
        provider="deepgram",
        surface="tts",
        adapter="easycat.tts.deepgram_tts.DeepgramTTS",
        protocol="websocket",
        mode="streaming",
        model_api_version="aura-2",
        required_extra="deepgram",
        credential_env_var="DEEPGRAM_API_KEY",
        live_pytest_target=(
            "tests/tts/test_tts_deepgram.py::TestDeepgramTTS::test_live_deepgram_tts"
        ),
    ),
    ProviderSurfaceSpec(
        provider="elevenlabs",
        surface="tts",
        adapter="easycat.tts.elevenlabs_tts.ElevenLabsTTS",
        protocol="http/websocket",
        mode="streaming",
        model_api_version="eleven_v3",
        required_extra="elevenlabs",
        credential_env_var="ELEVENLABS_API_KEY",
        live_pytest_target=(
            "tests/tts/test_tts_elevenlabs.py::TestElevenLabsTTSGeneral::test_live_elevenlabs_tts"
        ),
    ),
    ProviderSurfaceSpec(
        provider="cartesia",
        surface="tts",
        adapter="easycat.tts.cartesia_tts.CartesiaTTS",
        protocol="websocket",
        mode="streaming",
        model_api_version="sonic-2",
        required_extra="cartesia",
        credential_env_var="CARTESIA_API_KEY",
        live_pytest_target="tests/tts/test_tts_cartesia.py::TestCartesiaTTS::test_live_cartesia_tts",
    ),
    ProviderSurfaceSpec(
        provider="openai-agents",
        surface="agent_bridge",
        adapter="easycat.integrations.agents.openai_agents.OpenAIAgentsBridge",
        protocol="python-sdk",
        mode="streaming",
        model_api_version="openai-agents",
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
        api_version=spec.model_api_version,
        api_version_header_behavior=_api_version_header_behavior(spec),
        capabilities=_surface_capabilities(spec),
        contract_status=spec.contract_status,
        schema_status=spec.schema_status,
        status=_capability_status(live_status, failure_class),
        live_checked_at=live_checked_at,
        models=(ProviderIdentifier(spec.model_api_version, safe=True),),
        latency=latency,
        failure_class=failure_class,
    )


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
        return ProviderCapabilities(
            input_audio_formats=("text",),
            output_audio_formats=("pcm16",),
            streaming=streaming,
            streaming_behavior="streamed_audio_chunks",
            finalization_behavior="audio_stream_exhaustion",
            markers=False,
            alignment=spec.provider in {"elevenlabs", "cartesia"},
            ssml=spec.provider == "elevenlabs",
        )
    if spec.surface == "agent_bridge":
        return ProviderCapabilities(
            streaming=streaming,
            streaming_behavior="agent_event_stream",
            finalization_behavior="agent_done_event",
            markers=False,
            alignment=False,
            ssml=False,
        )
    if spec.surface == "transport":
        return ProviderCapabilities(
            input_audio_formats=("pcm16",),
            output_audio_formats=("pcm16",),
            streaming=True,
            streaming_behavior="duplex_audio_frames",
            finalization_behavior="transport_close",
            markers=True,
            alignment=False,
            ssml=False,
        )
    return ProviderCapabilities(
        input_audio_formats=("pcm16",),
        output_audio_formats=("speech_segments",),
        streaming=True,
        streaming_behavior="frame_analysis",
        finalization_behavior="speech_boundary",
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
    return "failure" if str(live_status).startswith("failed") else str(live_status)


def _api_version_header_behavior(spec: ProviderSurfaceSpec) -> str:
    if spec.surface in {"transport", "vad"}:
        return "not_applicable"
    return "provider_default"


def _adapter_version(spec: ProviderSurfaceSpec) -> str:
    return spec.adapter.rsplit(".", maxsplit=1)[-1]


def _safe_key(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value.lower())
