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
                "elapsed_ms": 12.346,
                "sequence": 42,
                "record_ref": "cp_42",
                "transcript": "customer said a secret",
                "tool_arguments": {"password": "secret"},
            },
            "input_ref": "a" * 64,
            "error": {
                "type": "ProviderError",
                "code": "rate_limit",
                "message": "Bearer secret-token",
                "traceback": "/home/user/app.py",
                "notes": (
                    "stage=agent\nprovider=openai\nelapsed_ms=12.346\n"
                    "sequence=42\nrecord_key=cp_42"
                ),
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
        "data": {
            "provider": "openai",
            "stage": "agent",
        },
        "omitted_data_fields": 5,
        "framework": "openai-agents",
        "refs": {"input_ref": "a" * 64},
        "error": {
            "code": "rate_limit",
            "notes": {
                "stage": "agent",
                "provider": "openai",
                "elapsed_ms": 12.346,
                "sequence": 42,
                "record_key": "cp_42",
            },
            "type": "ProviderError",
            "omitted_error_fields": 2,
        },
        "tags": ["provider", "retry"],
    }


def test_context_projection_keeps_only_machine_generated_error_note_lines() -> None:
    projected = project_context_record(
        {
            "sequence": 7,
            "data": {
                "stage": "tts",
                "provider": "openaitts",
                "elapsed_ms": 0.125,
                "sequence": 7,
                "record_ref": "cp_7",
            },
            "error": {
                "type": "ProviderError",
                "notes": (
                    "stage=tts\n"
                    "provider=openaitts\n"
                    "elapsed_ms=0.125\n"
                    "sequence=7\n"
                    "record_key=cp_7\n"
                    "prompt: customer said my SSN is 123-45-6789\n"
                    "provider=duplicate"
                ),
            },
        }
    )

    assert projected == {
        "sequence": 7,
        "data": {
            "provider": "openaitts",
            "stage": "tts",
        },
        "omitted_data_fields": 3,
        "error": {
            "type": "ProviderError",
            "notes": {
                "stage": "tts",
                "provider": "openaitts",
                "elapsed_ms": 0.125,
                "sequence": 7,
                "record_key": "cp_7",
            },
            "omitted_error_note_lines": 2,
        },
    }


def test_context_projection_matches_rounded_machine_elapsed_note() -> None:
    projected = project_context_record(
        {
            "sequence": 7,
            "data": {"elapsed_ms": 12.3456},
            "error": {
                "type": "ProviderError",
                "notes": "elapsed_ms=12.346",
            },
        }
    )

    assert projected["error"]["notes"]["elapsed_ms"] == 12.346


def test_context_projection_rejects_valid_syntax_that_conflicts_with_structured_context() -> None:
    projected = project_context_record(
        {
            "sequence": 7,
            "data": {"stage": "agent", "provider": "openai"},
            "error": {
                "type": "ProviderError",
                "notes": "stage=agent\nprovider=customerAccountABC123",
            },
        }
    )

    assert projected == {
        "sequence": 7,
        "data": {"provider": "openai", "stage": "agent"},
        "error": {
            "type": "ProviderError",
            "notes": {"stage": "agent"},
            "omitted_error_note_lines": 1,
        },
    }


def test_context_projection_rejects_spoofed_machine_error_notes() -> None:
    projected = project_context_record(
        {
            "sequence": 7,
            "error": {
                "notes": (
                    "stage=agent response\n"
                    "provider=https://sensitive.example\n"
                    "elapsed_ms=nan\n"
                    "sequence=07\n"
                    "record_key=cp_-1"
                ),
            },
        }
    )

    assert projected == {
        "sequence": 7,
        "error": {"omitted_error_fields": 1},
    }


def test_context_projection_counts_non_mapping_payload_as_omitted() -> None:
    assert project_context_record({"sequence": 1, "data": "raw provider payload"}) == {
        "sequence": 1,
        "omitted_data_fields": 1,
    }


def test_context_projection_ignores_empty_allowed_and_disallowed_fields() -> None:
    projected = project_context_record(
        {
            "sequence": 1,
            "data": {
                "provider": "",
                "stage": "agent",
                "transcript": "",
                "tool_arguments": {"query": "sensitive"},
            },
            "error": {
                "type": "",
                "code": "provider_error",
                "message": "",
                "notes": "sensitive detail",
            },
        }
    )

    assert projected == {
        "sequence": 1,
        "data": {"stage": "agent"},
        "omitted_data_fields": 1,
        "error": {
            "code": "provider_error",
            "omitted_error_fields": 1,
        },
    }
