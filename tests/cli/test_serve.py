"""Tests for ``easycat serve`` — the VoiceApp-driven voice playground launcher."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

import easycat.cli.serve as serve_mod
import easycat.config as config_mod
import easycat.integrations.agents.responses_api as responses_mod
from easycat.cli.serve import _playground_url


class _StubVoiceApp:
    """Records the config_factory it was built with and its run() calls."""

    def __init__(self, *, config_factory: Any) -> None:
        self.config_factory = config_factory
        self.runs: list[dict[str, Any]] = []

    def run(self, mode: str, **kwargs: Any) -> None:
        self.runs.append({"mode": mode, **kwargs})


@pytest.fixture
def stub_runtime(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the VoiceApp build/run pair so the CLI surface is testable offline."""
    calls: dict[str, Any] = {"build": None, "app": None, "ran": []}

    def fake_build(*, agent_model: str, instructions: str) -> _StubVoiceApp:
        factory = serve_mod._playground_config_factory(
            agent_model=agent_model, instructions=instructions
        )
        app = _StubVoiceApp(config_factory=factory)
        calls["build"] = {"agent_model": agent_model, "instructions": instructions}
        calls["app"] = app
        return app

    def fake_run(app: Any, *, mode: str, host: str, port: int, token: str | None) -> None:
        calls["ran"].append({"app": app, "mode": mode, "host": host, "port": port, "token": token})

    monkeypatch.setattr(serve_mod, "_build_voice_app", fake_build)
    monkeypatch.setattr(serve_mod, "_run_voice_app", fake_run)
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    # ``serve`` eagerly validates the playground config before announcing, which
    # requires the OpenAI key; provide one so these tests exercise the happy path.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return calls


def test_serve_help_describes_playground(cli: CliRunner, typer_app) -> None:
    result = cli.invoke(typer_app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "playground" in result.stdout.lower()
    assert "--token" in result.stdout
    assert "--mode" in result.stdout


def test_serve_prints_open_url_and_runs_app(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    result = cli.invoke(typer_app, ["serve", "--port", "9123"])

    assert result.exit_code == 0
    assert "Open http://localhost:9123" in result.stdout
    assert len(stub_runtime["ran"]) == 1
    run = stub_runtime["ran"][0]
    assert run["mode"] == "browser"
    assert run["host"] == "127.0.0.1"
    assert run["port"] == 9123
    assert run["token"] is None
    # The VoiceApp was constructed with a config_factory (per-connection path).
    assert stub_runtime["app"].config_factory is not None


def test_serve_refuses_non_loopback_host_without_token(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    result = cli.invoke(typer_app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    assert "token" in result.output.lower()
    assert stub_runtime["ran"] == []


def test_serve_local_mode_ignores_non_loopback_host(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    """Local/mic mode opens no listener, so the non-loopback bind-token guard
    must not block ``serve --mode local --host 0.0.0.0`` (host is unused there)."""
    result = cli.invoke(typer_app, ["serve", "--mode", "local", "--host", "0.0.0.0"])

    assert result.exit_code == 0
    assert stub_runtime["ran"][0]["mode"] == "local"


def test_serve_allows_non_loopback_host_with_token(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    result = cli.invoke(typer_app, ["serve", "--host", "0.0.0.0", "--token", "sekrit"])

    assert result.exit_code == 0
    assert "?token=sekrit" in result.stdout
    run = stub_runtime["ran"][0]
    assert run["token"] == "sekrit"
    assert run["host"] == "0.0.0.0"


def test_serve_reads_token_from_env(
    cli: CliRunner,
    typer_app,
    stub_runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", "envtoken")

    result = cli.invoke(typer_app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 0
    assert stub_runtime["ran"][0]["token"] == "envtoken"


def test_serve_requires_openai_key_before_listening(
    cli: CliRunner,
    typer_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing OPENAI_API_KEY fails serve at startup, before announce/listen."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)
    ran: list[Any] = []
    # The eager validation must short-circuit before the app is built or run; if
    # it does not, these stubs make the test fail loudly rather than serve.
    monkeypatch.setattr(
        serve_mod, "_build_voice_app", lambda **_: pytest.fail("built app despite missing key")
    )
    monkeypatch.setattr(serve_mod, "_run_voice_app", lambda *a, **k: ran.append(k))

    result = cli.invoke(typer_app, ["serve", "--port", "9123"])

    assert result.exit_code != 0
    assert "EASYCAT_E203" in result.output
    assert "OPENAI_API_KEY" in result.output
    # The Open URL must not be announced and the server must not start.
    assert "Open http" not in result.output
    assert ran == []


def test_serve_websocket_mode_routes_through_voice_app(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    result = cli.invoke(typer_app, ["serve", "--mode", "websocket", "--port", "8765"])

    assert result.exit_code == 0
    run = stub_runtime["ran"][0]
    assert run["mode"] == "websocket"
    assert run["host"] == "127.0.0.1"
    assert run["port"] == 8765
    # websocket is a per-connection mode; the app must carry a config_factory.
    assert stub_runtime["app"].config_factory is not None
    # The browser playground URL / page hint must NOT print for non-browser modes.
    assert "Open http" not in result.stdout
    assert "The page shows" not in result.stdout


def test_serve_local_mode_omits_browser_output(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    result = cli.invoke(typer_app, ["serve", "--mode", "local"])

    assert result.exit_code == 0
    assert stub_runtime["ran"][0]["mode"] == "local"
    assert "Open http" not in result.stdout
    assert "The page shows" not in result.stdout


def test_serve_websocket_mode_prints_ws_endpoint(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    """WebSocket mode starts a raw WS listener — print a ws:// endpoint, not an HTTP page."""
    result = cli.invoke(typer_app, ["serve", "--mode", "websocket", "--port", "8765"])

    assert result.exit_code == 0
    assert "ws://localhost:8765" in result.stdout
    # The browser playground URL/message must NOT appear for websocket mode.
    assert "http://" not in result.stdout
    assert "transcript" not in result.stdout.lower()


def test_serve_local_mode_prints_mic_hint_not_url(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    """Local mode has no listener — never print a URL the user cannot use."""
    result = cli.invoke(typer_app, ["serve", "--mode", "local"])

    assert result.exit_code == 0
    assert "microphone" in result.stdout.lower()
    assert "http://" not in result.stdout
    assert "ws://" not in result.stdout


def test_serve_rejects_unknown_mode(
    cli: CliRunner, typer_app, stub_runtime: dict[str, Any]
) -> None:
    result = cli.invoke(typer_app, ["serve", "--mode", "telepathy"])

    assert result.exit_code == 2
    assert "telepathy" in result.output
    assert stub_runtime["ran"] == []


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


def test_voice_app_built_with_per_connection_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real seam builds a VoiceApp carrying a config_factory, not a static config."""
    captured: dict[str, Any] = {}

    class StubVoiceApp:
        def __init__(self, *, config_factory: Any = None, config: Any = None) -> None:
            captured["config_factory"] = config_factory
            captured["config"] = config

    # The build seam now validates the config up front, which needs a key.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("easycat.voice_app.VoiceApp", StubVoiceApp)

    app = serve_mod._build_voice_app(agent_model="gpt-test", instructions="Hi.")

    assert isinstance(app, StubVoiceApp)
    assert captured["config_factory"] is not None
    assert captured["config"] is None


def test_build_voice_app_fails_fast_without_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing OPENAI_API_KEY raises EASYCAT_E203 BEFORE the listener binds."""
    from easycat.errors import EasyCatError

    captured: dict[str, Any] = {}

    class StubVoiceApp:
        def __init__(self, *, config_factory: Any = None, config: Any = None) -> None:
            captured["built"] = True

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("easycat.voice_app.VoiceApp", StubVoiceApp)

    with pytest.raises(EasyCatError) as excinfo:
        serve_mod._build_voice_app(agent_model="gpt-test", instructions="Hi.")

    assert excinfo.value.code == "EASYCAT_E203"
    # The VoiceApp/listener was never constructed — validation ran first.
    assert "built" not in captured


def test_serve_cli_fails_fast_without_openai_key(
    cli: CliRunner, typer_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``easycat serve`` exits non-zero on a missing key instead of binding."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EASYCAT_SERVE_TOKEN", raising=False)

    ran: list[Any] = []
    monkeypatch.setattr(serve_mod, "_run_voice_app", lambda *a, **k: ran.append((a, k)))

    result = cli.invoke(typer_app, ["serve"])

    # EASYCAT_E203 maps to CLI exit code 3; the listener never ran.
    assert result.exit_code == 3
    assert ran == []


def test_playground_factory_wires_browser_transport_and_playground_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

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
        captured.setdefault("browser_calls", []).append(kwargs)
        from types import SimpleNamespace

        return SimpleNamespace(kind="browser-config", kwargs=kwargs)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(responses_mod, "RemoteResponsesAPIBridge", StubRemoteResponsesAPIBridge)
    monkeypatch.setattr(config_mod.EasyConfig, "browser", staticmethod(fake_browser))

    factory = serve_mod._playground_config_factory(
        agent_model="gpt-test", instructions="Speak plainly."
    )

    transport_a = object()
    transport_b = object()
    config_a = factory(transport_a)
    config_b = factory(transport_b)

    # The factory is invoked once per connection and yields a fresh config.
    assert config_a is not config_b
    assert config_a.kind == "browser-config"
    assert captured["browser_calls"][0]["transport"] is transport_a
    assert captured["browser_calls"][1]["transport"] is transport_b

    agent = captured["browser_calls"][0]["agent"]
    assert isinstance(agent, StubRemoteResponsesAPIBridge)
    assert agent.base_url == "https://api.openai.com"
    assert agent.model == "gpt-test"
    assert agent.api_key == "sk-test"

    from types import SimpleNamespace

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
    assert (
        _playground_url("0.0.0.0", 8443, "t") == "http://0.0.0.0:8443/webrtc_client.html?token=t"
    )
    assert (
        _playground_url("0.0.0.0", 8443, "token with/slash")
        == "http://0.0.0.0:8443/webrtc_client.html?token=token+with%2Fslash"
    )
