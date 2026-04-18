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
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import app


def _assert_envelope(payload: dict, command: str, status: str = "ok") -> None:
    assert payload.get("schema_version") == 1, payload
    assert payload.get("command") == command, payload
    assert payload.get("status") == status, payload


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
    assert payload["query"] == "E999"


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
    result = cli.invoke(app, ["init", "_", "--list-templates", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "init")
    assert isinstance(payload["templates"], list)


def test_init_error_envelope(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = cli.invoke(app, ["init", "demo", "--config", "not json", "--json"])
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "init", status="error")
    assert payload["code"] == "EASYCAT_E102"
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
