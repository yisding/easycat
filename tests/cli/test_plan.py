"""Focused CLI ergonomics tests for ``easycat plan``."""

from __future__ import annotations

from typer.testing import CliRunner

from easycat.cli._app import app


def test_plan_help_renders_profile_table_literally(cli: CliRunner) -> None:
    result = cli.invoke(app, ["plan", "--help"])
    help_text = " ".join(result.stdout.split())

    assert result.exit_code == 0
    assert "Voice profile table to plan" in help_text
    assert "voice.default" in help_text
    assert "The to plan" not in help_text
