from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.provider("cassette"),
    pytest.mark.surface_stt,
]


def test_websocket_cassette_schema_and_replay_order() -> None:
    payload = json.loads(Path("tests/cassettes/ws/openai-realtime-stt.json").read_text())

    assert payload["schema_version"] == 1
    assert payload["redaction_version"] == 1
    assert payload["protocol"] == "websocket"
    assert payload["provider_api_version"] == "realtime"
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
    assert payload["frames"][3]["payload_assertion"]["requires_prior_append"] is True
