"""Shared fixtures for eval tests."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from easycat.cli._app import _register_commands


@pytest.fixture
def cli() -> CliRunner:
    """Typer test runner with CLI commands registered once per test."""
    _register_commands()
    return CliRunner()
