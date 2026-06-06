"""Plan 11 — JSON envelope stability.

Every ``--json`` output shares a versioned envelope:

    {"schema_version": 1, "command": "...", "status": "ok"|"error", ...}

These tests walk every CLI command that accepts ``--json`` and check
the envelope shape against a single schema. Drift here is a breaking
change for coding-agent consumers.

See ``TEST_PLANS.md`` §11.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.validation.report import (
    GitMetadata,
    ValidationCheck,
    ValidationEnvironment,
    ValidationRun,
)
from easycat.validation.runner import ValidationRunResult


def _assert_envelope(payload: dict, command: str, status: str = "ok") -> None:
    assert payload.get("schema_version") == 1, payload
    assert payload.get("command") == command, payload
    assert payload.get("status") == status, payload


def _validation_run() -> ValidationRun:
    return ValidationRun(
        run_id="20260521T120000Z-quick-12345",
        command=["uv", "run", "pytest", "-q"],
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 21, 12, 0, 3, tzinfo=UTC),
        duration_s=3.25,
        status="pass",
        exit_code=0,
        tool_exit_codes={"pytest": 0},
        git=GitMetadata(sha="abc123", branch="feature/validation", dirty=True),
        environment=ValidationEnvironment(
            python="3.12.13",
            platform="Linux",
            ci=False,
            env_vars={"OPENAI_API_KEY": True},
        ),
        checks=[
            ValidationCheck(
                name="pytest.quick",
                status="pass",
                duration_s=2.75,
                command=["uv", "run", "pytest", "-q"],
            )
        ],
    )


def test_explain_single_code_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "E101", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "explain")
    assert payload["code"] == "EASYCAT_E101"
    # Required docs fields.
    for key in ("headline", "cause", "fix"):
        assert key in payload


def test_explain_list_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "--list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "explain")
    assert isinstance(payload["codes"], list)
    assert isinstance(payload["meta"], list)
    assert all("code" in c for c in payload["codes"])


def test_explain_meta_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "init-schema", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "explain")
    assert payload["slug"] == "init-schema"


def test_explain_unknown_code_error_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "E999", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "explain", status="error")
    assert payload["code"] == "EASYCAT_E501"
    assert "Unknown error code" in payload["message"]
    assert "easycat explain --list" in payload["fix"]
    assert payload["context"] == {"code": "E999"}
    assert payload["query"] == "E999"
    assert payload["exit_code"] == 2


def test_init_envelope(cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = cli.invoke(
        app,
        [
            "init",
            "demo",
            "--config",
            json.dumps({"schema_version": 1, "template": "text-chat"}),
            "--no-git",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "init")
    assert payload["template"] == "text-chat"
    assert isinstance(payload["files"], list)
    assert isinstance(payload["agent_lines"], int)
    assert isinstance(payload["git"], bool)


def test_init_list_templates_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["init", "--list-templates", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "init")
    assert isinstance(payload["templates"], list)
    assert isinstance(payload["catalog"], list)
    required_keys = {"name", "mode", "transport", "framework", "description"}
    assert all(required_keys <= set(entry) for entry in payload["catalog"])


def test_init_error_envelope(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = cli.invoke(app, ["init", "demo", "--config", "not json", "--json"])
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "init", status="error")
    assert payload["code"] == "EASYCAT_E102"
    assert payload["fix"]
    assert "easycat explain init-schema" in payload["fix"]
    assert payload["exit_code"] == 4


def test_doctor_ok_envelope(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("NO_COLOR", "1")

    def fake_head(url, **kw):  # noqa: ANN001
        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr("httpx.head", fake_head)
    result = cli.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "doctor")
    assert payload["environment"] == "dev"
    # Every check row has the required shape.
    for check in payload["checks"]:
        assert "name" in check and "status" in check and "detail" in check


def test_doctor_error_envelope(
    cli: CliRunner, empty_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_head(url, **kw):  # noqa: ANN001
        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr("httpx.head", fake_head)
    result = cli.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "doctor", status="error")


def test_docs_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "docs")
    assert isinstance(payload["entries"], list)
    entries = [{key: entry[key] for key in ("label", "path")} for entry in payload["entries"]]
    assert {"label": "CLI and scaffolds", "path": "README.md#cli"} in entries
    assert {"label": "Docs map", "path": "docs/README.md"} in entries
    assert {"label": "Examples", "path": "examples/README.md"} in entries
    assert {"label": "Contributing", "path": "CONTRIBUTING.md"} in entries
    assert {"label": "Deployment", "path": "docs/deployment/docker.md"} in entries
    assert {"label": "Observability", "path": "docs/observability.md"} in entries
    assert {"label": "Validation reference", "path": "plan/validation/reference.md"} in entries
    assert all(isinstance(entry.get("description"), str) for entry in payload["entries"])
    assert all(isinstance(entry.get("url"), str) for entry in payload["entries"])
    assert all(entry["url"].startswith(payload["source_url"]) for entry in payload["entries"])


def test_validate_quick_envelope(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_validation_slice(slice_name: str, **kwargs: object) -> ValidationRunResult:
        assert slice_name == "quick"
        run = _validation_run()
        result_report = tmp_path / "run" / "report.json"
        result_report.parent.mkdir()
        result_report.write_text(run.to_json())
        return ValidationRunResult(
            run=run,
            run_dir=result_report.parent,
            report_path=result_report,
            exit_code=0,
        )

    monkeypatch.setattr("easycat.cli.validate.run_validation_slice", fake_run_validation_slice)

    result = cli.invoke(app, ["validate", "quick", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "validate quick")
    assert payload["exit_code"] == 0
    assert payload["validation"]["kind"] == "validation_run"


def test_validate_report_envelope(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(_validation_run().to_json())

    result = cli.invoke(app, ["validate", "report", str(report_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "validate report")
    assert payload["report_path"] == str(report_path)
    assert payload["exit_code"] == 0
    assert payload["validation"]["kind"] == "validation_run"


def test_stdout_is_parseable_json_even_with_stderr_noise(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract is ``stdout = json, stderr = logs``.  A consumer
    should be able to ``| jq`` the output without parsing errors even
    if stderr carries progress/logs.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    result = cli.invoke(app, ["explain", "E101", "--json"])
    # Pure JSON stdout — json.loads must succeed without stripping.
    json.loads(result.stdout)
