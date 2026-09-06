"""Focused CLI ergonomics tests for ``easycat plan``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import app


def test_plan_help_renders_profile_table_literally(cli: CliRunner) -> None:
    result = cli.invoke(app, ["plan", "--help"])
    help_text = " ".join(result.stdout.split())

    assert result.exit_code == 0
    assert "Voice profile table to plan" in help_text
    assert "voice.default" in help_text
    assert "The to plan" not in help_text


def _write_manifest(tmp_path: Path, *, vad: str) -> Path:
    """A profile with no install-extra dependency other than the VAD backend.

    ``transport = "websocket"`` on purpose: the default ``local`` transport pulls
    an unrelated ``local`` extra on a dev-group-only machine, which would put a
    second red line in the output and make the assertions ambiguous.
    """
    manifest = tmp_path / "easycat.toml"
    manifest.write_text(
        "\n".join(
            [
                "[project]",
                'name = "plan-human-test"',
                "",
                "[voice.default]",
                'transport = "websocket"',
                'stt = "openai/realtime"',
                'tts = "openai"',
                f'vad = "{vad}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return manifest


def test_plan_human_output_prints_missing_backends(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selected backend the session cannot construct gets its own red line.

    Krisp ships no PyPI package, so there is no extra to name on the
    ``missing extras:`` line — without a line of its own the operator would see a
    ``blocked`` status with nothing explaining it.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setattr(
        "easycat.planning._resolution._default_module_available",
        lambda name: name != "krisp_audio",
    )

    result = cli.invoke(app, ["plan", "--manifest", str(_write_manifest(tmp_path, vad="krisp"))])

    assert result.exit_code == 0, result.stdout
    output = " ".join(result.stdout.split())
    assert "missing backends: vad:krisp" in output
    assert "status: blocked" in output


def test_plan_human_output_omits_missing_backends_when_there_are_none(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the line is conditional, like its missing-extras sibling."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setattr(
        "easycat.planning._resolution._default_module_available", lambda _name: True
    )

    result = cli.invoke(app, ["plan", "--manifest", str(_write_manifest(tmp_path, vad="krisp"))])

    assert result.exit_code == 0, result.stdout
    output = " ".join(result.stdout.split())
    assert "missing backends:" not in output
    assert "status: ready" in output
