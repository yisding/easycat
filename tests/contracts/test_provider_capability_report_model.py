from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from easycat.validation.provider_capabilities import (
    ProviderCapabilities,
    ProviderCapabilityReport,
    ProviderIdentifier,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.provider("capability-report"),
    pytest.mark.surface_tts,
]


def test_provider_capability_report_serializes_required_json_shape() -> None:
    report = ProviderCapabilityReport(
        provider="openai",
        surface="tts",
        adapter="easycat.tts.openai_tts.OpenAITTS",
        protocol="http",
        mode="streaming",
        adapter_version="easycat-tts-openai-v1",
        required_extra="openai",
        credential_env_var="OPENAI_API_KEY",
        credential_env_var_present=True,
        live_checked_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
        api_version="audio-speech-v1",
        api_version_header_behavior="not_used",
        capabilities=ProviderCapabilities(
            input_audio_formats=(),
            output_audio_formats=("pcm16/24000/mono",),
            streaming=True,
            streaming_behavior="chunked_http_response",
            finalization_behavior="stream_exhaustion",
            markers=False,
            alignment=False,
            ssml=False,
        ),
        models=(ProviderIdentifier("gpt-4o-mini-tts", safe=True),),
        voices=(ProviderIdentifier("voice-user-specific-1234567890"),),
        contract_status="pass",
        schema_status="unchanged",
        latency={"p50_ms": 82.5, "sample_count": 5},
        failure_class=None,
        status="pass",
    )

    payload = report.to_dict()

    assert payload["kind"] == "provider_capability_report"
    assert payload["schema_version"] == 1
    assert payload["provider"] == "openai"
    assert payload["surface"] == "tts"
    assert payload["adapter"] == "easycat.tts.openai_tts.OpenAITTS"
    assert payload["live_checked_at"] == "2026-05-22T12:00:00Z"
    assert payload["api_version"] == "audio-speech-v1"
    assert payload["auth"] == {
        "credential_env_var": "OPENAI_API_KEY",
        "credential_env_var_present": True,
    }
    assert payload["capabilities"] == {
        "input_audio_formats": [],
        "output_audio_formats": ["pcm16/24000/mono"],
        "streaming": True,
        "streaming_behavior": "chunked_http_response",
        "finalization_behavior": "stream_exhaustion",
        "markers": False,
        "alignment": False,
        "ssml": False,
        "api_version_header_behavior": "not_used",
    }
    assert payload["models"] == ["gpt-4o-mini-tts"]
    assert payload["voices"] == ["[REDACTED_PROVIDER_IDENTIFIER]"]
    assert payload["contract_status"] == "pass"
    assert payload["schema_status"] == "unchanged"
    assert payload["latency"] == {"p50_ms": 82.5, "sample_count": 5}
    assert payload["failure_class"] is None
    assert json.loads(report.to_json()) == payload


def test_provider_capability_report_redacts_secret_like_values_inside_capabilities() -> None:
    report = ProviderCapabilityReport(
        provider="elevenlabs",
        surface="tts",
        adapter="easycat.tts.elevenlabs_tts.ElevenLabsTTS",
        protocol="websocket",
        mode="streaming",
        adapter_version="easycat-tts-elevenlabs-v1",
        required_extra="elevenlabs",
        credential_env_var="ELEVENLABS_API_KEY",
        credential_env_var_present=True,
        api_version="text-to-speech-v1",
        api_version_header_behavior="not_used",
        capabilities=ProviderCapabilities(
            output_audio_formats=("pcm_24000",),
            streaming=True,
            streaming_behavior="websocket_input_stream",
            finalization_behavior="empty_text_flush",
            markers=True,
            alignment=True,
            ssml=False,
            provider_options={"request_id": "req_abc123456789", "endpoint": "https://api.test"},
        ),
        models=(ProviderIdentifier("eleven_flash_v2_5", safe=True),),
        voices=(ProviderIdentifier("EXAVITQu4vr4xnSDxMaL"),),
        contract_status="pass",
        schema_status="unchanged",
        status="pass",
    )

    payload = report.to_dict()

    assert payload["capabilities"]["provider_options"] == {
        "endpoint": "[REDACTED_URL]",
        "request_id": "[REDACTED_REQUEST_ID]",
    }
    assert payload["voices"] == ["[REDACTED_PROVIDER_IDENTIFIER]"]


@pytest.mark.parametrize(
    ("status", "contract_status", "schema_status", "failure_class"),
    [
        ("pass", "pass", "unchanged", None),
        ("expected_skip", "expected_skip", "unknown", None),
        ("auth_failure", "fail", "unknown", "provider_auth"),
        ("quota_failure", "fail", "unknown", "provider_quota"),
        ("provider_drift", "pass", "breaking_failure", "provider_api_drift"),
    ],
)
def test_provider_capability_report_represents_required_outcomes(
    status: str,
    contract_status: str,
    schema_status: str,
    failure_class: str | None,
) -> None:
    report = ProviderCapabilityReport(
        provider="openai",
        surface="tts",
        adapter="easycat.tts.openai_tts.OpenAITTS",
        protocol="http",
        mode="streaming",
        adapter_version="easycat-tts-openai-v1",
        required_extra="openai",
        credential_env_var="OPENAI_API_KEY",
        credential_env_var_present=status not in {"expected_skip", "auth_failure"},
        api_version="audio-speech-v1",
        api_version_header_behavior="not_used",
        capabilities=ProviderCapabilities(output_audio_formats=("pcm16/24000/mono",)),
        contract_status=contract_status,
        schema_status=schema_status,
        failure_class=failure_class,
        status=status,
    )

    payload = report.to_dict()

    assert payload["status"] == status
    assert payload["contract_status"] == contract_status
    assert payload["schema_status"] == schema_status
    assert payload["failure_class"] == failure_class
