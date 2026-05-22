from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from easycat.integrations.agents._responses_api_events import translate_sse_event
from easycat.integrations.agents.base import NULL_RECORDER

pytestmark = [
    pytest.mark.contract,
    pytest.mark.provider("cassette"),
    pytest.mark.agent_bridge,
    pytest.mark.surface_agent,
]

_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9_.-]+"),
    re.compile(r"\b(?:req|resp|response)_[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"https://[^/\s]+\.openai\.com"),
]


def test_sse_cassette_replays_remote_response_text_delta_and_done() -> None:
    path = Path("tests/cassettes/sse/remote-responses-api.json")
    raw = path.read_text()
    payload = json.loads(raw)

    assert payload["schema_version"] == 1
    assert payload["redaction_version"] == 1
    assert payload["protocol"] == "sse"
    assert payload["provider_api_version"] == "responses-api"
    assert not _secret_pattern_matches(raw)
    translated = [
        translate_sse_event(event["event"], event["data"], NULL_RECORDER)
        for event in payload["events"]
    ]

    assert [event.kind for event in translated if event is not None] == ["text_delta"]
    assert translated[0].text == "hello"
    assert translated[1] is None


def test_sse_cassette_redaction_detector_fails_on_injected_secret() -> None:
    raw = '{"headers":{"authorization":"Authorization: Bearer sk-testsecret123456"}}'

    assert _secret_pattern_matches(raw)


def _secret_pattern_matches(raw: str) -> bool:
    return any(pattern.search(raw) for pattern in _SECRET_PATTERNS)
