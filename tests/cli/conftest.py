"""Shared fixtures for CLI tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from easycat.cli._app import _register_commands, app


@pytest.fixture
def cli() -> CliRunner:
    """Typer test runner.  Commands are registered once per test."""
    _register_commands()
    return CliRunner()


@pytest.fixture
def typer_app():
    """Access the shared Typer app with commands pre-registered."""
    _register_commands()
    return app


@pytest.fixture
def empty_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Scrub API-key env vars so doctor reports deterministic state."""
    for var in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    yield
