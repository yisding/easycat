from __future__ import annotations

import json
from datetime import UTC, datetime

from easycat.validation.report import (
    ProviderCheck,
    ValidationCheck,
    ValidationEnvironment,
    ValidationFailure,
    ValidationRun,
)


def test_validation_run_serialization_redacts_keyed_secrets_and_unsafe_text() -> None:
    run = ValidationRun(
        run_id="run-redaction",
        command=["easycat", "validate", "live", "--api-key", "short-secret"],
        started_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 6, 6, 12, 1, tzinfo=UTC),
        duration_s=60.0,
        status="fail",
        exit_code=1,
        environment=ValidationEnvironment(
            python="3.12.0",
            platform="/home/alice/build/linux",
            ci=False,
            env_vars={"OPENAI_API_KEY": True},
        ),
        checks=[
            ValidationCheck(
                name="redaction-boundary",
                status="fail",
                duration_s=0.1,
                details={
                    "api_key": "short",
                    "access_token": "short-token",
                    "headers": {
                        "Authorization": "Bearer short-bearer",
                        "xi-api-key": "short-xi",
                    },
                    "prompt": "repeat the user's private prompt",
                    "transcript": "customer phone call text",
                },
            )
        ],
        failures=[
            ValidationFailure(
                name="provider",
                message="provider returned req_abc123456789 from /Users/alice/project",
                details={"signed_url": "https://example.test/path?token=short"},
            )
        ],
        providers=[
            ProviderCheck(
                provider="openai",
                surface="stt",
                state="failed",
                credential_env="OPENAI_API_KEY",
                required=True,
                details={"client_secret": "short-client-secret"},
            )
        ],
    )

    payload = run.to_dict()
    raw_json = json.dumps(payload, sort_keys=True)

    for leaked in (
        "short-secret",
        "short-token",
        "short-bearer",
        "short-xi",
        "repeat the user's private prompt",
        "customer phone call text",
        "short-client-secret",
        "https://example.test",
        "/Users/alice",
        "req_abc123456789",
    ):
        assert leaked not in raw_json

    assert payload["command"] == [
        "easycat",
        "validate",
        "live",
        "--api-key",
        "[REDACTED_SECRET]",
    ]
    assert payload["environment"]["env_vars"] == {"OPENAI_API_KEY": True}
    assert payload["checks"][0]["details"] == {
        "access_token": "[REDACTED_SECRET]",
        "api_key": "[REDACTED_SECRET]",
        "headers": {
            "Authorization": "[REDACTED_SECRET]",
            "xi-api-key": "[REDACTED_SECRET]",
        },
        "prompt": "[REDACTED_PROMPT]",
        "transcript": "[REDACTED_TRANSCRIPT]",
    }
    assert payload["failures"][0]["details"] == {"signed_url": "[REDACTED_SECRET]"}
    assert payload["providers"][0]["credential_env"] == "OPENAI_API_KEY"
    assert payload["providers"][0]["details"] == {
        "client_secret": "[REDACTED_SECRET]",
    }
