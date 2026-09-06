"""Focused CLI ergonomics tests for ``easycat plan``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.errors import EASYCAT_E501
from easycat.planning import selection


def test_plan_help_renders_profile_table_literally(cli: CliRunner) -> None:
    result = cli.invoke(app, ["plan", "--help"])
    help_text = " ".join(result.stdout.split())

    assert result.exit_code == 0
    assert "Voice profile table to plan" in help_text
    assert "voice.default" in help_text
    assert "The to plan" not in help_text


def _write_manifest(tmp_path: Path, body: str) -> Path:
    manifest = tmp_path / "easycat.toml"
    manifest.write_text(
        '[project]\nname = "plan-cli"\n\n[voice.default]\ntransport = "webrtc"\n' + body,
        encoding="utf-8",
    )
    return manifest


def test_plan_uses_the_shared_profile_resolution(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-1: ``plan`` no longer owns a private exception -> code mapping."""
    manifest = _write_manifest(tmp_path, "")

    def sentinel(*_args: object, **_kwargs: object) -> None:
        raise EASYCAT_E501(code="dx2-sentinel")

    monkeypatch.setattr(selection, "plan_selected_profile", sentinel)

    result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"])

    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "EASYCAT_E501"


def test_plan_unresolvable_backend_reports_e602(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-2: redaction must not eat the useful part of the planner message."""
    manifest = _write_manifest(tmp_path, 'vad = "silro"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"])

    payload = json.loads(result.stdout)
    assert payload["code"] == "EASYCAT_E602"
    assert payload["context"]["path"] == "[voice.default]"
    assert "silro" in payload["message"]


def test_plan_unknown_provider_still_exits_two_with_e104(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-3: a coded planner error keeps its own code and exit."""
    manifest = _write_manifest(tmp_path, 'stt = "opnai"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "EASYCAT_E104"
    assert "Traceback" not in result.stdout


def test_plan_error_redacts_a_secret_shaped_manifest_value(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-4: a secret inside an exception cause must not reach stdout."""
    secret = "sk-live-abcdef1234567890abcdef"
    manifest = _write_manifest(tmp_path, f'vad = "{secret}"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"])

    payload = json.loads(result.stdout)
    assert payload["code"] == "EASYCAT_E602"
    assert secret not in result.stdout
    assert "[REDACTED_SECRET]" in payload["message"]
