from __future__ import annotations

import json
from pathlib import Path

import pytest

from easycat.validation.redaction import contains_unredacted_sensitive_text

pytestmark = [pytest.mark.contract, pytest.mark.provider("cassette"), pytest.mark.surface_stt]


def test_http_cassette_schema_and_redaction() -> None:
    path = Path("tests/cassettes/http/openai-stt.json")
    payload = json.loads(path.read_text())
    raw = path.read_text()

    assert payload["schema_version"] == 1
    assert payload["redaction_version"] == 1
    assert payload["protocol"] == "http"
    assert payload["interactions"]
    assert payload["interactions"][0]["request"]["headers"]["authorization"] == "[REDACTED_SECRET]"
    assert not contains_unredacted_sensitive_text(raw)


def test_cassette_redaction_detector_fails_on_injected_secret() -> None:
    raw = '{"headers":{"authorization":"Authorization: Bearer sk-testsecret123456"}}'

    assert contains_unredacted_sensitive_text(raw)
