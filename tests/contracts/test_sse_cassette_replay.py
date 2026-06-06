from __future__ import annotations

import json
from pathlib import Path

import pytest

from easycat.integrations.agents._responses_api_events import translate_sse_event
from easycat.integrations.agents.base import NULL_RECORDER
from easycat.validation.redaction import contains_unredacted_sensitive_text

pytestmark = [
    pytest.mark.contract,
    pytest.mark.provider("cassette"),
    pytest.mark.agent_bridge,
    pytest.mark.surface_agent,
]


def test_sse_cassette_replays_remote_response_text_delta_and_done() -> None:
    path = Path("tests/cassettes/sse/remote-responses-api.json")
    raw = path.read_text()
    payload = json.loads(raw)

    assert payload["schema_version"] == 1
    assert payload["redaction_version"] == 1
    assert payload["protocol"] == "sse"
    assert payload["provider_api_version"] == "responses-api"
    assert not contains_unredacted_sensitive_text(raw)
    translated = [
        translate_sse_event(event["event"], event["data"], NULL_RECORDER)
        for event in payload["events"]
    ]

    assert [event.kind for event in translated if event is not None] == ["text_delta"]
    assert translated[0].text == "hello"
    assert translated[1] is None


def test_sse_cassette_redaction_detector_fails_on_injected_secret() -> None:
    raw = '{"headers":{"authorization":"Authorization: Bearer sk-testsecret123456"}}'

    assert contains_unredacted_sensitive_text(raw)
