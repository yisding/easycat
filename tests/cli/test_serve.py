"""Tests for ``easycat serve`` — the browser voice playground launcher."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import easycat.cli.serve as serve_mod
import easycat.config as config_mod
import easycat.integrations.agents.responses_api as responses_mod
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


def test_build_serve_session_wires_browser_transport_and_playground_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel_session = object()

    class StubRemoteResponsesAPIBridge:
        def __init__(self, base_url: str, model: str, *, api_key: str | None = None) -> None:
            self.base_url = base_url
            self.model = model
            self.api_key = api_key

        def _build_request_body(self, turn_input: Any) -> dict[str, Any]:
            return {
                "model": self.model,
                "input": [{"role": "user", "content": turn_input.text}],
                "stream": True,
                "metadata": {"parent": "kept"},
            }

    def fake_browser(**kwargs: Any) -> Any:
        captured["browser_kwargs"] = kwargs
        return SimpleNamespace(kind="browser-config", kwargs=kwargs)

    def fake_create_session(config: Any) -> object:
        captured["created_config"] = config
        return sentinel_session

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        responses_mod,
        "RemoteResponsesAPIBridge",
        StubRemoteResponsesAPIBridge,
    )
    monkeypatch.setattr(config_mod.EasyConfig, "browser", staticmethod(fake_browser))
    monkeypatch.setattr(config_mod, "create_session", fake_create_session)

    session = serve_mod._build_serve_session(
        host="0.0.0.0",
        port=9123,
        token="sekrit",
        agent_model="gpt-test",
        instructions="Speak plainly.",
    )

    assert session is sentinel_session
    assert captured["created_config"].kind == "browser-config"

    transport = captured["browser_kwargs"]["transport"]
    assert transport.host == "0.0.0.0"
    assert transport.port == 9123
    assert transport.auth_token == "sekrit"

    agent = captured["browser_kwargs"]["agent"]
    assert isinstance(agent, StubRemoteResponsesAPIBridge)
    assert agent.base_url == "https://api.openai.com"
    assert agent.model == "gpt-test"
    assert agent.api_key == "sk-test"

    body = agent._build_request_body(SimpleNamespace(text="hello"))
    assert body == {
        "model": "gpt-test",
        "input": [{"role": "user", "content": "hello"}],
        "stream": True,
        "metadata": {"parent": "kept"},
        "instructions": "Speak plainly.",
    }


def test_playground_url_shapes() -> None:
    assert _playground_url("127.0.0.1", 8080, None) == "http://localhost:8080"
    assert _playground_url("0.0.0.0", 8443, "t") == "http://0.0.0.0:8443/?token=t"
