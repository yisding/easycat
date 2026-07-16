"""Keep Chapter 15's doctor exercise aligned with the JSON contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import _register_commands, app

CHAPTER = Path(__file__).resolve().parents[2] / "docs" / "teaching" / "15-operate-in-production"
PROVIDER_KEYS = (
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "CARTESIA_API_KEY",
)


def _checks(result) -> tuple[dict, dict[str, dict]]:
    payload = json.loads(result.stdout)
    return payload, {check["name"]: check for check in payload["checks"]}


def test_scoped_production_reports_distinguish_credentials_from_liveness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EASYCAT_DATA_DIR", str(tmp_path / ".easycat"))
    _register_commands()
    args = ["doctor", "--provider", "openai", "--environment", "production", "--json"]

    missing = CliRunner().invoke(app, args)
    missing_payload, missing_checks = _checks(missing)

    assert missing.exit_code == 1
    assert missing_payload["status"] == "error"
    assert list(missing_checks) == [
        "python_version",
        "easycat_version",
        "env_openai",
        "onnxruntime",
        "journal_writable",
        "disk_space",
    ]
    assert missing_checks["env_openai"]["status"] == "fail"
    assert missing_checks["env_openai"]["code"] == "EASYCAT_E203"
    assert "reach_openai" not in missing_checks
    assert "microphone" not in missing_checks

    requests: list[tuple[str, dict[str, object]]] = []

    def fake_head(url: str, **kwargs: object):
        requests.append((url, kwargs))

        class Response:
            status_code = 401

        return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setattr("httpx.head", fake_head)
    configured = CliRunner().invoke(app, args)
    configured_payload, configured_checks = _checks(configured)

    assert configured.exit_code == 0, configured.stderr
    assert configured_payload["status"] == "ok"
    assert configured_checks["env_openai"]["status"] == "ok"
    assert configured_checks["reach_openai"] == {
        "name": "reach_openai",
        "status": "ok",
        "detail": "openai reachable (401)",
    }
    assert requests and requests[0][0] == "https://api.openai.com/v1"
    assert "headers" not in requests[0][1]


def test_doctor_exercise_names_the_live_check_families() -> None:
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(exercises.split())

    assert "checks five things" not in exercises
    assert "does not probe the noise-reduction or echo-cancellation extras" in normalized
    for name in (
        "Python version",
        "EasyCat version",
        "provider environment variables",
        "provider reachability",
        "`onnxruntime`",
        "microphone",
        "journal writability",
        "disk space",
    ):
        assert name in exercises
    assert "--provider openai --environment production --json" in normalized
    assert "`--environment production`" in readme
    assert "`--provider <name>`" in readme
