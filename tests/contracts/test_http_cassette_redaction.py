from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.provider("cassette"), pytest.mark.surface_stt]

_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9_.-]+"),
    re.compile(r"\b(?:req|resp|response)_[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"https://[^/\s]+\.openai\.com"),
]


def test_http_cassette_schema_and_redaction() -> None:
    path = Path("tests/cassettes/http/openai-stt.json")
    payload = json.loads(path.read_text())
    raw = path.read_text()

    assert payload["schema_version"] == 1
    assert payload["redaction_version"] == 1
    assert payload["protocol"] == "http"
    assert payload["interactions"]
    assert payload["interactions"][0]["request"]["headers"]["authorization"] == "[REDACTED_SECRET]"
    assert not _secret_pattern_matches(raw)


def test_cassette_redaction_detector_fails_on_injected_secret() -> None:
    raw = '{"headers":{"authorization":"Authorization: Bearer sk-testsecret123456"}}'

    assert _secret_pattern_matches(raw)


def _secret_pattern_matches(raw: str) -> bool:
    return any(pattern.search(raw) for pattern in _SECRET_PATTERNS)
