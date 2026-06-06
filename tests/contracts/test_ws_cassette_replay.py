from __future__ import annotations

import json
from pathlib import Path

import pytest

from easycat.validation.redaction import contains_unredacted_sensitive_text
from tests.contracts.provider_surface_matrix import PROVIDER_SURFACE_CONTRACTS

pytestmark = [
    pytest.mark.contract,
    pytest.mark.provider("cassette"),
    pytest.mark.surface_stt,
]
REPO_ROOT = Path(__file__).resolve().parents[2]


def _has_key(value: object, target_key: str) -> bool:
    if isinstance(value, dict):
        return target_key in value or any(_has_key(item, target_key) for item in value.values())
    if isinstance(value, list):
        return any(_has_key(item, target_key) for item in value)
    return False


def test_websocket_cassette_schema_and_replay_order() -> None:
    path = Path("tests/cassettes/ws/openai-realtime-stt.json")
    raw = path.read_text()
    payload = json.loads(raw)

    assert payload["schema_version"] == 1
    assert payload["redaction_version"] == 1
    assert payload["protocol"] == "websocket"
    assert payload["provider_api_version"] == "realtime"
    assert payload["capabilities_ref"] == "tests/contracts/provider_surface_matrix.py"
    assert not contains_unredacted_sensitive_text(raw)
    assert [(frame["direction"], frame["kind"]) for frame in payload["frames"]] == [
        ("client", "session.update"),
        ("server", "session.updated"),
        ("client", "input_audio_buffer.append"),
        ("client", "input_audio_buffer.commit"),
        ("server", "conversation.item.input_audio_transcription.completed"),
    ]
    assert payload["frames"][4]["payload_assertion"]["normalized_event_kind"] == "final_transcript"
    for frame in payload["frames"]:
        assert frame["opcode"] in {"text", "binary"}
        assert frame["kind"]
        assert "payload_assertion" in frame
    assert payload["frames"][0]["payload_assertion"]["session_audio_input_fields"] == [
        "format",
        "transcription",
        "turn_detection",
    ]
    assert payload["frames"][2]["payload_assertion"]["redacted_fields"] == ["audio"]
    assert not _has_key(payload["frames"], "audio")
    assert payload["frames"][3]["payload_assertion"]["requires_prior_append"] is True


def test_validation_tasks_v36_current_state_tracks_websocket_cassette_scope() -> None:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V3.6 Add WebSocket Cassette Proof Of Concept", 1)[1].split(
        "### V3.7 Add Schema Drift Fingerprints", 1
    )[0]
    normalized_section = " ".join(section.split())
    source = (REPO_ROOT / "tests/contracts/test_ws_cassette_replay.py").read_text(encoding="utf-8")
    fixture_path = REPO_ROOT / "tests/cassettes/ws/openai-realtime-stt.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    ws_rows = [
        row
        for row in PROVIDER_SURFACE_CONTRACTS
        if row.cassette_path.startswith("tests/cassettes/ws/")
    ]
    required_ws_paths = {row.cassette_path for row in ws_rows if row.cassette_status == "required"}
    openai_realtime_row = next(row for row in ws_rows if row.provider == "openai-realtime")

    assert required_ws_paths == {"tests/cassettes/ws/openai-realtime-stt.json"}
    assert openai_realtime_row.surface == payload["surface"] == "stt"
    assert openai_realtime_row.protocol == payload["protocol"] == "websocket"
    assert openai_realtime_row.cassette_status == "required"
    assert any(
        row.cassette_status == "deferred" for row in ws_rows if row is not openai_realtime_row
    )
    assert [(frame["direction"], frame["kind"]) for frame in payload["frames"]] == [
        ("client", "session.update"),
        ("server", "session.updated"),
        ("client", "input_audio_buffer.append"),
        ("client", "input_audio_buffer.commit"),
        ("server", "conversation.item.input_audio_transcription.completed"),
    ]
    assert payload["frames"][2]["payload_assertion"]["redacted_fields"] == ["audio"]
    assert not _has_key(payload["frames"], "audio")
    assert "contains_unredacted_sensitive_text(raw)" in source

    assert "Current verified state:" in section
    for token in (
        "tests/contracts/test_ws_cassette_replay.py",
        "tests/cassettes/ws/openai-realtime-stt.json",
        "schema_version=1",
        "redaction_version=1",
        "protocol=websocket",
        "provider_api_version=realtime",
        "capabilities_ref=tests/contracts/provider_surface_matrix.py",
        "openai-realtime",
        "session.update",
        "session.updated",
        "input_audio_buffer.append",
        "input_audio_buffer.commit",
        "conversation.item.input_audio_transcription.completed",
        "direction",
        "opcode",
        "kind",
        "payload_assertion",
        "text",
        "binary",
        "format",
        "transcription",
        "turn_detection",
        "requires_prior_append=True",
        "normalized_event_kind=final_transcript",
        'redacted_fields=["audio"]',
        "contains_unredacted_sensitive_text",
        "tests/contracts/provider_surface_matrix.py",
        "cassette_status=required",
        "deferred",
        "error",
    ):
        assert f"`{token}`" in section
    for phrase in (
        "checked-in",
        "happy-path frame order",
        "stores no generated audio payload",
        "other WebSocket provider rows",
        "schema fingerprint tests separately pin",
        "happy-path parser-compatibility smoke proof",
        "not an error-frame cassette",
    ):
        assert phrase in normalized_section
