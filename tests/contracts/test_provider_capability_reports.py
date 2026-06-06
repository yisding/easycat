from __future__ import annotations

from pathlib import Path

import pytest

from tests.contracts.schema_fingerprints import (
    DirectionalSchemaRule,
    SchemaFingerprintRule,
    compare_schema_fingerprint,
)

pytestmark = [pytest.mark.contract, pytest.mark.provider("schema"), pytest.mark.surface_stt]
REPO_ROOT = Path(__file__).resolve().parents[2]

OPENAI_REALTIME_INBOUND_EVENT_TYPES = frozenset(
    {
        "conversation.item.input_audio_transcription.delta",
        "conversation.item.input_audio_transcription.completed",
        "error",
        "session.created",
        "session.updated",
        "transcription_session.updated",
    }
)


def test_schema_fingerprint_pins_openai_realtime_inbound_event_enum() -> None:
    rule = SchemaFingerprintRule(
        inbound=DirectionalSchemaRule(
            required_fields=frozenset({"type"}),
            optional_fields=frozenset({"delta", "error", "session", "transcript"}),
            enum_fields={"type": OPENAI_REALTIME_INBOUND_EVENT_TYPES},
        )
    )

    for event_type in sorted(OPENAI_REALTIME_INBOUND_EVENT_TYPES):
        result = compare_schema_fingerprint(
            {"type": event_type},
            rule,
            direction="inbound",
        )

        assert result["status"] == "unchanged"


def test_schema_fingerprint_unchanged() -> None:
    rule = SchemaFingerprintRule(
        inbound=DirectionalSchemaRule(
            required_fields=frozenset({"type", "transcript"}),
            enum_fields={
                "type": frozenset({"conversation.item.input_audio_transcription.completed"})
            },
        )
    )

    result = compare_schema_fingerprint(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "hello",
        },
        rule,
        direction="inbound",
    )

    assert result["status"] == "unchanged"


def test_schema_fingerprint_additive_unknown_field_warns() -> None:
    rule = SchemaFingerprintRule(
        inbound=DirectionalSchemaRule(required_fields=frozenset({"type", "transcript"}))
    )

    result = compare_schema_fingerprint(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "hello",
            "confidence": 0.98,
        },
        rule,
        direction="inbound",
    )

    assert result["status"] == "additive_warning"
    assert result["additive_fields"] == ["confidence"]


def test_schema_fingerprint_missing_required_field_fails() -> None:
    rule = SchemaFingerprintRule(
        inbound=DirectionalSchemaRule(required_fields=frozenset({"type", "transcript"}))
    )

    result = compare_schema_fingerprint(
        {"type": "conversation.item.input_audio_transcription.completed"},
        rule,
        direction="inbound",
    )

    assert result["status"] == "breaking_failure"
    assert result["missing_required_fields"] == ["transcript"]


def test_schema_fingerprint_provider_enum_change_fails() -> None:
    rule = SchemaFingerprintRule(
        inbound=DirectionalSchemaRule(
            required_fields=frozenset({"type"}),
            enum_fields={"type": OPENAI_REALTIME_INBOUND_EVENT_TYPES},
        )
    )

    result = compare_schema_fingerprint(
        {"type": "conversation.item.input_audio_transcription.done"},
        rule,
        direction="inbound",
    )

    assert result["status"] == "breaking_failure"
    assert result["enum_failures"] == {"type": "conversation.item.input_audio_transcription.done"}


def test_schema_fingerprint_content_type_change_fails() -> None:
    rule = SchemaFingerprintRule(
        inbound=DirectionalSchemaRule(
            required_fields=frozenset({"content_type"}),
            enum_fields={"content_type": frozenset({"application/json"})},
        )
    )

    result = compare_schema_fingerprint(
        {"content_type": "text/event-stream"},
        rule,
        direction="inbound",
    )

    assert result["status"] == "breaking_failure"
    assert result["enum_failures"] == {"content_type": "text/event-stream"}


def test_schema_fingerprint_error_shape_change_fails() -> None:
    rule = SchemaFingerprintRule(
        inbound=DirectionalSchemaRule(
            required_fields=frozenset({"type"}),
            enum_fields={"type": OPENAI_REALTIME_INBOUND_EVENT_TYPES},
            object_required_fields={"error": frozenset({"code", "message"})},
        )
    )

    result = compare_schema_fingerprint(
        {"type": "error", "error": {"message": "bad request"}},
        rule,
        direction="inbound",
    )

    assert result["status"] == "breaking_failure"
    assert result["object_shape_failures"] == {"error": {"missing_required_fields": ["code"]}}


def test_schema_fingerprint_error_shape_non_object_fails() -> None:
    rule = SchemaFingerprintRule(
        inbound=DirectionalSchemaRule(
            required_fields=frozenset({"type"}),
            object_required_fields={"error": frozenset({"code", "message"})},
        )
    )

    result = compare_schema_fingerprint(
        {"type": "error", "error": "bad request"},
        rule,
        direction="inbound",
    )

    assert result["status"] == "breaking_failure"
    assert result["object_shape_failures"] == {"error": {"expected": "object", "actual": "str"}}


def test_schema_fingerprint_observed_optional_fields_remain_unchanged() -> None:
    rule = SchemaFingerprintRule(
        inbound=DirectionalSchemaRule(
            required_fields=frozenset({"type", "transcript"}),
            optional_fields=frozenset({"item_id", "content_index"}),
        )
    )

    result = compare_schema_fingerprint(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "hello",
            "item_id": "item_redacted",
            "content_index": 0,
        },
        rule,
        direction="inbound",
    )

    assert result["status"] == "unchanged"


def test_schema_fingerprint_unknown_direction_is_explicit() -> None:
    rule = SchemaFingerprintRule(
        inbound=DirectionalSchemaRule(required_fields=frozenset({"type", "transcript"}))
    )

    result = compare_schema_fingerprint(
        {"type": "conversation.item.input_audio_transcription.completed"},
        rule,
        direction="sideways",
    )

    assert result["status"] == "unknown"


def test_schema_fingerprint_inbound_and_outbound_rules_are_independent() -> None:
    rule = SchemaFingerprintRule(
        inbound=DirectionalSchemaRule(required_fields=frozenset({"type", "transcript"})),
        outbound=DirectionalSchemaRule(required_fields=frozenset({"type", "audio"})),
    )

    inbound_result = compare_schema_fingerprint(
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "hi"},
        rule,
        direction="inbound",
    )
    outbound_result = compare_schema_fingerprint(
        {"type": "input_audio_buffer.append"},
        rule,
        direction="outbound",
    )

    assert inbound_result["status"] == "unchanged"
    assert outbound_result["status"] == "breaking_failure"
    assert outbound_result["missing_required_fields"] == ["audio"]


def test_validation_tasks_v37_current_state_tracks_schema_fingerprint_contracts() -> None:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V3.7 Add Schema Drift Fingerprints", 1)[1].split(
        "## V4: Live Canaries And Provider Reports", 1
    )[0]
    normalized_section = " ".join(section.split())
    helper_source = (REPO_ROOT / "tests/contracts/schema_fingerprints.py").read_text(
        encoding="utf-8"
    )
    test_source = (REPO_ROOT / "tests/contracts/test_provider_capability_reports.py").read_text(
        encoding="utf-8"
    )

    for symbol in (
        "SchemaDriftStatus",
        "DirectionalSchemaRule",
        "SchemaFingerprintRule",
        "compare_schema_fingerprint",
        "object_required_fields",
        "object_shape_failures",
    ):
        assert symbol in helper_source
    for test_name in (
        "test_schema_fingerprint_additive_unknown_field_warns",
        "test_schema_fingerprint_missing_required_field_fails",
        "test_schema_fingerprint_provider_enum_change_fails",
        "test_schema_fingerprint_content_type_change_fails",
        "test_schema_fingerprint_error_shape_change_fails",
        "test_schema_fingerprint_unknown_direction_is_explicit",
        "test_schema_fingerprint_inbound_and_outbound_rules_are_independent",
    ):
        assert test_name in test_source

    assert "Current verified state:" in section
    for token in (
        "tests/contracts/schema_fingerprints.py",
        "tests/contracts/test_provider_capability_reports.py",
        "SchemaDriftStatus",
        "DirectionalSchemaRule",
        "SchemaFingerprintRule",
        "compare_schema_fingerprint",
        "required_fields",
        "optional_fields",
        "enum_fields",
        "object_required_fields",
        "inbound",
        "outbound",
        "unchanged",
        "additive_warning",
        "breaking_failure",
        "unknown",
        "missing_required_fields",
        "enum_failures",
        "object_shape_failures",
        "content_type",
        "error",
        "conversation.item.input_audio_transcription.delta",
        "conversation.item.input_audio_transcription.completed",
        "session.created",
        "session.updated",
        "transcription_session.updated",
    ):
        assert f"`{token}`" in section
    for phrase in (
        "observed payload dictionaries",
        "OpenAI realtime inbound event enum",
        "additive unknown fields",
        "missing top-level required fields",
        "provider enum changes",
        "`content_type` enum changes",
        "nested error-object shape changes",
        "not a generated provider schema registry",
    ):
        assert phrase in normalized_section
