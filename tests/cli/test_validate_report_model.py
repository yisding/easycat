from __future__ import annotations

import json
from pathlib import Path

from easycat.validation.report import (
    ProviderCheck,
    ProviderCheckState,
    ValidationCheck,
    ValidationEnvironment,
    ValidationFailure,
    ValidationSkip,
)

from ._validation_helpers import _validation_run

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_validation_run_serializes_required_fields_deterministically() -> None:
    run = _validation_run()

    payload = run.to_dict()

    assert payload["schema_version"] == 1
    assert payload["redaction_version"] == 1
    assert payload["kind"] == "validation_run"
    assert payload["run_id"] == "20260521T120000Z-quick-12345"
    assert payload["command"] == ["uv", "run", "pytest", "-q"]
    assert payload["started_at"] == "2026-05-21T12:00:00Z"
    assert payload["finished_at"] == "2026-05-21T12:00:03Z"
    assert payload["duration_s"] == 3.25
    assert payload["status"] == "pass"
    assert payload["exit_code"] == 0
    assert payload["tool_exit_codes"] == {"pytest": 0}
    assert payload["git"] == {"branch": "feature/validation", "dirty": True, "sha": "abc123"}
    assert payload["environment"]["env_vars"] == {
        "DEEPGRAM_API_KEY": False,
        "OPENAI_API_KEY": True,
    }
    assert payload["checks"][0]["artifacts"]["junit"] == {
        "kind": "junit",
        "path": ".easycat/validation/runs/20260521T120000Z-quick-12345/junit.xml",
    }
    assert payload["skips"] == []
    assert payload["failures"] == []
    assert payload["latency"] is None
    assert payload["providers"] == []
    assert payload["provider_reports"] == []
    assert payload["extras"] == []
    assert payload["artifacts"] == {}

    assert run.to_json() == run.to_json()
    assert json.loads(run.to_json()) == payload


def test_validation_report_redacts_secret_like_and_unsafe_values() -> None:
    secret = "sk-" + ("a" * 32)
    run = _validation_run(
        command=[
            "uv",
            "run",
            "pytest",
            f"--api-key={secret}",
            "https://api.example.test/v1?token=hidden-token",
        ],
        environment=ValidationEnvironment(
            python="3.12.13",
            platform="Linux",
            ci=False,
            env_vars={"OPENAI_API_KEY": True},
        ),
        failures=[
            ValidationFailure(
                name="pytest.quick",
                message=(
                    f"Authorization: Bearer {secret}; request req_123456789; "
                    "phone +1 (415) 555-2671; file /home/alice/project/test.py"
                ),
            )
        ],
    )

    serialized = run.to_json()

    assert secret not in serialized
    assert "hidden-token" not in serialized
    assert "https://api.example.test" not in serialized
    assert "+1 (415) 555-2671" not in serialized
    assert "req_123456789" not in serialized
    assert "/home/alice" not in serialized
    assert "OPENAI_API_KEY" in serialized
    assert "env_vars" in serialized


def test_validation_schema_represents_pass_fail_and_expected_skip() -> None:
    run = _validation_run(
        status="fail",
        exit_code=1,
        tool_exit_codes={"pytest": 1},
        checks=[
            ValidationCheck(name="pytest.quick", status="pass", duration_s=1.0),
            ValidationCheck(name="pytest.socket", status="fail", duration_s=1.0),
            ValidationCheck(name="provider.openai", status="skip", duration_s=0.0),
        ],
        skips=[ValidationSkip(name="provider.openai", reason="OPENAI_API_KEY missing")],
        failures=[ValidationFailure(name="pytest.socket", message="1 test failed")],
    )

    payload = run.to_dict()

    assert payload["status"] == "fail"
    assert [check["status"] for check in payload["checks"]] == ["pass", "fail", "skip"]
    assert payload["skips"] == [
        {"expected": True, "name": "provider.openai", "reason": "OPENAI_API_KEY missing"}
    ]
    assert payload["failures"] == [{"message": "1 test failed", "name": "pytest.socket"}]


def test_provider_states_distinguish_expected_skip_from_required_secret_failure() -> None:
    assert {state.value for state in ProviderCheckState} == {
        "not_requested",
        "skipped_missing_secret",
        "failed_missing_required_secret",
        "passed",
        "failed",
    }

    run = _validation_run(
        providers=[
            ProviderCheck(
                provider="openai",
                surface="stt",
                state=ProviderCheckState.SKIPPED_MISSING_SECRET,
                credential_env="OPENAI_API_KEY",
            ),
            ProviderCheck(
                provider="deepgram",
                surface="stt",
                state=ProviderCheckState.FAILED_MISSING_REQUIRED_SECRET,
                credential_env="DEEPGRAM_API_KEY",
                required=True,
            ),
        ]
    )

    assert run.to_dict()["providers"] == [
        {
            "credential_env": "OPENAI_API_KEY",
            "provider": "openai",
            "required": False,
            "state": "skipped_missing_secret",
            "surface": "stt",
        },
        {
            "credential_env": "DEEPGRAM_API_KEY",
            "provider": "deepgram",
            "required": True,
            "state": "failed_missing_required_secret",
            "surface": "stt",
        },
    ]


def test_validation_tasks_v02_current_state_tracks_report_model_contract() -> None:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V0.2 Define Validation Report Model", 1)[1].split(
        "### V0.3 Create `scripts/validate.py quick/socket`",
        1,
    )[0]
    report_source = (REPO_ROOT / "src/easycat/validation/report.py").read_text(encoding="utf-8")
    redaction_source = (REPO_ROOT / "src/easycat/validation/redaction.py").read_text(
        encoding="utf-8"
    )
    test_source = (REPO_ROOT / "tests/cli/test_validate_report_model.py").read_text(
        encoding="utf-8"
    )
    redaction_property_source = (
        REPO_ROOT / "tests/validation/test_redaction_property.py"
    ).read_text(encoding="utf-8")

    assert "Current verified state:" in section
    for symbol in (
        "ValidationRun",
        "ValidationCheck",
        "ValidationSkip",
        "ValidationFailure",
        "ArtifactRef",
        "GitMetadata",
        "ValidationEnvironment",
        "ProviderCheck",
        "ProviderCheckState",
        "schema_version",
        "redaction_version",
        "run_id",
        "command",
        "tool_exit_codes",
        "git",
        "environment",
        "checks",
        "skips",
        "failures",
        "latency",
        "reliability",
        "providers",
        "provider_reports",
        "extras",
        "artifacts",
        "not_requested",
        "skipped_missing_secret",
        "failed_missing_required_secret",
        "passed",
        "failed",
    ):
        assert symbol in report_source
        assert f"`{symbol}`" in section
    for symbol in (
        "redact_text",
        "redact_runtime_secrets",
        "redact_value",
        "redact_command",
        "UNSAFE_TEXT_FIELDS",
    ):
        assert symbol in redaction_source
        assert f"`{symbol}`" in section
    for test_name in (
        "test_validation_run_serializes_required_fields_deterministically",
        "test_validation_report_redacts_secret_like_and_unsafe_values",
        "test_validation_schema_represents_pass_fail_and_expected_skip",
        "test_provider_states_distinguish_expected_skip_from_required_secret_failure",
    ):
        assert test_name in test_source
    for test_name in (
        "test_redact_text_is_idempotent",
        "test_runtime_secret_is_removed",
        "test_key_based_redaction_catches_short_secret_values",
        "test_unsafe_text_fields_use_domain_specific_placeholders",
        "test_shared_detector_flags_cassette_sensitive_patterns",
        "test_redact_command_redacts_split_secret_flags",
    ):
        assert test_name in redaction_property_source
    for token in (
        "src/easycat/validation/report.py",
        "src/easycat/validation/redaction.py",
        "tests/cli/test_validate_report_model.py",
        "tests/validation/test_redaction_property.py",
    ):
        assert f"`{token}`" in section
