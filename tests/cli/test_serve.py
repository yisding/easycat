"""Tests for ``easycat serve`` — the browser voice playground launcher."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

import easycat.cli.serve as serve_mod
from easycat.cli.serve import _playground_url


class _StubSession:
    pass


@pytest.fixture
def stub_runtime(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the session build/run pair so the CLI surface is testable offline."""
    calls: dict[str, Any] = {"build": None, "ran": []}

    def fake_build(**kwargs: Any) -> _StubSession:
        calls["build"] = kwargs
        return _StubSession()

    def fake_run(session: Any) -> None:
        calls["ran"].append(session)

    monkeypatch.setattr(serve_mod, "_build_serve_session", fake_build)
    monkeypatch.setattr(serve_mod, "_run_serve", fake_run)
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    return calls


def test_serve_help_describes_playground(cli: CliRunner, typer_app) -> None:
    result = cli.invoke(typer_app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "playground" in result.stdout.lower()
    assert "--token" in result.stdout


def test_serve_prints_open_url_and_runs_session(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    result = cli.invoke(typer_app, ["serve", "--port", "9123"])

    assert result.exit_code == 0
    assert "Open http://localhost:9123" in result.stdout
    assert stub_runtime["build"]["host"] == "127.0.0.1"
    assert stub_runtime["build"]["port"] == 9123
    assert stub_runtime["build"]["token"] is None
    assert len(stub_runtime["ran"]) == 1


def test_serve_refuses_non_loopback_host_without_token(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    result = cli.invoke(typer_app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    assert "token" in result.output.lower()
    assert stub_runtime["ran"] == []


def test_serve_allows_non_loopback_host_with_token(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    result = cli.invoke(typer_app, ["serve", "--host", "0.0.0.0", "--token", "sekrit"])

    assert result.exit_code == 0
    assert "?token=sekrit" in result.stdout
    assert stub_runtime["build"]["token"] == "sekrit"
    assert stub_runtime["build"]["host"] == "0.0.0.0"


def test_serve_reads_token_from_env(
    cli: CliRunner,
    typer_app,
    stub_runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", "envtoken")

    result = cli.invoke(typer_app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 0
    assert stub_runtime["build"]["token"] == "envtoken"


def test_serve_passes_agent_options_through(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    result = cli.invoke(
        typer_app,
        ["serve", "--agent-model", "gpt-4.1-mini", "--instructions", "Be terse."],
    )

    assert result.exit_code == 0
    assert stub_runtime["build"]["agent_model"] == "gpt-4.1-mini"
    assert stub_runtime["build"]["instructions"] == "Be terse."


def test_playground_url_shapes() -> None:
    assert _playground_url("127.0.0.1", 8080, None) == "http://localhost:8080"
    assert _playground_url("0.0.0.0", 8443, "t") == "http://0.0.0.0:8443/?token=t"
