from __future__ import annotations

from easycat.cli.debug._context_projection import project_context_record


def test_context_projection_keeps_only_allowlisted_diagnostics() -> None:
    projected = project_context_record(
        {
            "sequence": 7,
            "kind": "event",
            "name": "provider_failed",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "timing": {"wall_ns": 123},
            "framework": "openai-agents",
            "data": {
                "stage": "agent",
                "provider": "openai",
                "transcript": "customer said a secret",
                "tool_arguments": {"password": "secret"},
            },
            "input_ref": "a" * 64,
            "error": {
                "type": "ProviderError",
                "code": "rate_limit",
                "message": "Bearer secret-token",
                "traceback": "/home/user/app.py",
            },
            "tags": {"provider", "retry"},
        }
    )

    assert projected == {
        "sequence": 7,
        "kind": "event",
        "name": "provider_failed",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "wall_ns": 123,
        "data": {"provider": "openai", "stage": "agent"},
        "omitted_data_fields": 2,
        "framework": "openai-agents",
        "refs": {"input_ref": "a" * 64},
        "error": {
            "code": "rate_limit",
            "type": "ProviderError",
            "omitted_error_fields": 2,
        },
        "tags": ["provider", "retry"],
    }


def test_context_projection_counts_non_mapping_payload_as_omitted() -> None:
    assert project_context_record({"sequence": 1, "data": "raw provider payload"}) == {
        "sequence": 1,
        "omitted_data_fields": 1,
    }
