from __future__ import annotations

import json
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from easycat.tts.input import TTSInputPolicy
from easycat.validation.provider_capabilities import (
    ProviderCapabilities,
    ProviderCapabilityReport,
    ProviderIdentifier,
)
from easycat.validation.redaction import REDACTION_VERSION

pytestmark = [
    pytest.mark.contract,
    pytest.mark.provider("capability-report"),
    pytest.mark.surface_tts,
]
REPO_ROOT = Path(__file__).resolve().parents[2]


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
            tts_input_policy=TTSInputPolicy.plain_text(),
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
    assert payload["redaction_version"] == REDACTION_VERSION
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
        "tts_input_policy": {
            "accepted_formats": ["plain"],
            "supports_ssml": False,
            "unsupported_ssml": "strip",
            "streaming_boundary": "sentence",
            "pause_support": "none",
            "pronunciation_support": "none",
            "marker_support": "none",
        },
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
            tts_input_policy=TTSInputPolicy.plain_text(
                provider_options={"endpoint": "https://api.test", "api_key": "short"}
            ),
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
    assert payload["capabilities"]["tts_input_policy"]["provider_options"] == {
        "api_key": "[REDACTED_SECRET]",
        "endpoint": "[REDACTED_URL]",
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


def test_validation_tasks_v41_current_state_tracks_provider_capability_model() -> None:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V4.1 Add Provider Capability Report Model", 1)[1].split(
        "### V4.2 Implement `easycat validate live`", 1
    )[0]
    normalized_section = " ".join(section.split())
    model_source = (REPO_ROOT / "src/easycat/validation/provider_capabilities.py").read_text(
        encoding="utf-8"
    )
    exports_source = (REPO_ROOT / "src/easycat/validation/__init__.py").read_text(encoding="utf-8")
    test_source = (
        REPO_ROOT / "tests/contracts/test_provider_capability_report_model.py"
    ).read_text(encoding="utf-8")
    report_fields = {field.name for field in fields(ProviderCapabilityReport)}
    expected_report_fields = {
        "provider",
        "surface",
        "adapter",
        "protocol",
        "mode",
        "adapter_version",
        "required_extra",
        "credential_env_var",
        "credential_env_var_present",
        "api_version",
        "api_version_header_behavior",
        "capabilities",
        "contract_status",
        "schema_status",
        "status",
        "live_checked_at",
        "models",
        "voices",
        "latency",
        "failure_class",
        "schema_version",
        "redaction_version",
        "kind",
    }

    assert expected_report_fields <= report_fields
    for symbol in (
        "ProviderCapabilityStatus",
        "ProviderContractStatus",
        "ProviderSchemaStatus",
        "ProviderCapabilityReport",
        "ProviderCapabilities",
        "ProviderIdentifier",
        "_REDACTED_PROVIDER_IDENTIFIER",
        "REDACTION_VERSION",
        "redact_text",
        "redact_value",
    ):
        assert symbol in model_source
    for symbol in ("ProviderCapabilityReport", "ProviderCapabilities", "ProviderIdentifier"):
        assert symbol in exports_source
    for status in (
        "pass",
        "expected_skip",
        "auth_failure",
        "quota_failure",
        "provider_drift",
        "failure",
    ):
        assert status in model_source
    for test_name in (
        "test_provider_capability_report_serializes_required_json_shape",
        "test_provider_capability_report_redacts_secret_like_values_inside_capabilities",
        "test_provider_capability_report_represents_required_outcomes",
    ):
        assert test_name in test_source

    assert "Current verified state:" in section
    for token in (
        "src/easycat/validation/provider_capabilities.py",
        "ProviderCapabilityReport",
        "ProviderCapabilities",
        "ProviderIdentifier",
        "easycat.validation",
        "ProviderCapabilityStatus",
        "ProviderContractStatus",
        "ProviderSchemaStatus",
        "pass",
        "expected_skip",
        "auth_failure",
        "quota_failure",
        "provider_drift",
        "failure",
        "kind=provider_capability_report",
        "schema_version",
        "redaction_version",
        "provider",
        "surface",
        "adapter",
        "protocol",
        "mode",
        "adapter_version",
        "required_extra",
        "live_checked_at",
        "api_version",
        "auth",
        "capabilities",
        "models",
        "voices",
        "contract_status",
        "schema_status",
        "latency",
        "failure_class",
        "status",
        "streaming",
        "streaming_behavior",
        "finalization_behavior",
        "markers",
        "alignment",
        "ssml",
        "tts_input_policy",
        "api_version_header_behavior",
        "provider_options",
        "redact_text",
        "[REDACTED_PROVIDER_IDENTIFIER]",
        "redact_value",
        "tests/contracts/test_provider_capability_report_model.py",
        "to_json()",
    ):
        assert f"`{token}`" in section
    for phrase in (
        "protocol-free model types",
        "input/output audio formats",
        "safe low-cardinality identifiers",
        "unsafe provider-specific identifiers",
        "nested capability provider options",
        "required JSON shape",
        "UTC `live_checked_at` serialization",
        "TTS input-policy serialization",
        "safe model ID preservation",
        "unsafe voice ID suppression",
        "pass / expected-skip / auth-failure / quota-failure / provider-drift",
    ):
        assert phrase in normalized_section
