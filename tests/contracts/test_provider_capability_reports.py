from __future__ import annotations

import pytest

from tests.contracts.schema_fingerprints import (
    DirectionalSchemaRule,
    SchemaFingerprintRule,
    compare_schema_fingerprint,
)

pytestmark = [pytest.mark.contract, pytest.mark.provider("schema"), pytest.mark.surface_stt]

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
