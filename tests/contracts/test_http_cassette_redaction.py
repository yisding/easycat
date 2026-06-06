from __future__ import annotations

import json
from pathlib import Path

import pytest

from easycat.validation.redaction import contains_unredacted_sensitive_text
from tests.contracts.provider_surface_matrix import PROVIDER_SURFACE_CONTRACTS

pytestmark = [pytest.mark.contract, pytest.mark.provider("cassette"), pytest.mark.surface_stt]
REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_validation_tasks_v35_current_state_tracks_http_sse_cassette_scope() -> None:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V3.5 Add HTTP/SSE Cassette Proof Of Concept", 1)[1].split(
        "### V3.6 Add WebSocket Cassette Proof Of Concept", 1
    )[0]
    normalized_section = " ".join(section.split())
    http_source = (REPO_ROOT / "tests/contracts/test_http_cassette_redaction.py").read_text(
        encoding="utf-8"
    )
    sse_source = (REPO_ROOT / "tests/contracts/test_sse_cassette_replay.py").read_text(
        encoding="utf-8"
    )
    required_http_sse_paths = {
        row.cassette_path
        for row in PROVIDER_SURFACE_CONTRACTS
        if row.cassette_status == "required"
        and row.cassette_path.startswith(("tests/cassettes/http/", "tests/cassettes/sse/"))
    }
    expected_required_paths = {
        "tests/cassettes/http/openai-stt.json",
        "tests/cassettes/sse/remote-responses-api.json",
    }

    assert required_http_sse_paths == expected_required_paths
    for cassette_path in expected_required_paths:
        assert (REPO_ROOT / cassette_path).exists()
    assert "contains_unredacted_sensitive_text(raw)" in http_source
    assert "contains_unredacted_sensitive_text(raw)" in sse_source
    assert "sk-testsecret123456" in http_source
    assert "sk-testsecret123456" in sse_source
    assert "translate_sse_event" in sse_source

    assert "Current verified state:" in section
    for token in (
        "pytest-recording",
        "tests/contracts/test_http_cassette_redaction.py",
        "tests/contracts/test_sse_cassette_replay.py",
        "tests/cassettes/http/openai-stt.json",
        "tests/cassettes/sse/remote-responses-api.json",
        "protocol=http",
        "protocol=sse",
        "provider_api_version=responses-api",
        "contains_unredacted_sensitive_text",
        "translate_sse_event",
        "tests/contracts/provider_surface_matrix.py",
        "cassette_path",
        "cassette_status",
        "deferred",
        "not_applicable",
    ):
        assert f"`{token}`" in section
    for phrase in (
        "offline cassette replay/redaction proof",
        "checked-in JSON cassettes",
        "does not use a live recording harness",
        "redacted authorization headers",
        "fake `Authorization: Bearer sk-testsecret123456`",
        "required HTTP/SSE rows",
        "other rows are marked",
    ):
        assert phrase in normalized_section
