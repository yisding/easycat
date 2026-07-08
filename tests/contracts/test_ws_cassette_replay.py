from __future__ import annotations

import json
from pathlib import Path

import pytest

from easycat.validation.redaction import contains_unredacted_sensitive_text

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
