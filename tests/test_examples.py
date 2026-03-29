from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from easycat import EasyCatConfig, WebSocketTransportConfig, create_session

REPO_ROOT = Path(__file__).resolve().parents[1]


class _DummyAgent:
    async def run(self, text: str) -> str:
        return text


def test_local_chat_example_imports():
    import examples.local_chat as local_chat

    assert callable(local_chat.main)


def test_ws_server_example_imports():
    import examples.ws_server as ws_server

    assert callable(ws_server.main)


def test_pydantic_ai_example_imports():
    import examples.pydantic_ai_voice as pydantic_example

    assert callable(pydantic_example.main)


def test_webrtc_observability_example_imports():
    import examples.webrtc_observability_server as webrtc_observability

    assert callable(webrtc_observability.main)


def test_build_openai_agents_adapter_prefers_responses_websocket(monkeypatch: pytest.MonkeyPatch):
    from examples.common import build_openai_agents_adapter

    class DummyAgent:
        def __init__(self, name: str, instructions: str) -> None:
            self.name = name
            self.instructions = instructions

    class DummyOpenAIProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class DummyModelSettings:
        def __init__(self, *, reasoning=None, verbosity=None):
            self.reasoning = reasoning
            self.verbosity = verbosity

    class DummyRunConfig:
        def __init__(self, *, model_provider, model_settings=None):
            self.model_provider = model_provider
            self.model_settings = model_settings

    fake_agents = types.SimpleNamespace(
        Agent=DummyAgent,
        OpenAIProvider=DummyOpenAIProvider,
        RunConfig=DummyRunConfig,
        ModelSettings=DummyModelSettings,
    )
    monkeypatch.setitem(sys.modules, "agents", fake_agents)

    adapter = build_openai_agents_adapter(instructions="hello")
    provider_kwargs = adapter._run_config.model_provider.kwargs
    assert provider_kwargs["use_responses"] is True
    assert provider_kwargs["use_responses_websocket"] is True
    assert adapter._run_config.model_settings.verbosity == "low"
    reasoning = adapter._run_config.model_settings.reasoning
    effort = (
        reasoning.get("effort")
        if isinstance(reasoning, dict)
        else getattr(reasoning, "effort", None)
    )
    assert effort == "none"


def test_build_openai_agents_adapter_falls_back_to_responses_api_toggle(
    monkeypatch: pytest.MonkeyPatch,
):
    from examples.common import build_openai_agents_adapter

    class DummyAgent:
        def __init__(self, name: str, instructions: str) -> None:
            self.name = name
            self.instructions = instructions

    class FailingProvider:
        def __init__(self, **kwargs):
            raise TypeError("unsupported")

    class DummyRunConfig:
        def __init__(self, *, model_provider, model_settings=None):
            self.model_provider = model_provider
            self.model_settings = model_settings

    called: list[str] = []

    def set_default_openai_api(api: str) -> None:
        called.append(api)

    fake_agents = types.SimpleNamespace(
        Agent=DummyAgent,
        OpenAIProvider=FailingProvider,
        RunConfig=DummyRunConfig,
        ModelSettings=lambda **kwargs: kwargs,
        set_default_openai_api=set_default_openai_api,
    )
    monkeypatch.setitem(sys.modules, "agents", fake_agents)

    adapter = build_openai_agents_adapter(instructions="hello")
    assert adapter._run_config is None
    assert called == ["responses"]


def _python_executable() -> str:
    candidate = sys.executable or ""
    if candidate:
        return candidate
    for name in ("python3", "python"):
        resolved = shutil.which(name)
        if resolved:
            return resolved
    pytest.skip("No python executable available for subprocess test")


@pytest.mark.parametrize(
    "script_path",
    [
        "examples/local_chat.py",
        "examples/ws_server.py",
        "examples/ws_browser_example.py",
        "examples/webrtc_server.py",
        "examples/webrtc_observability_server.py",
        "examples/pydantic_ai_voice.py",
    ],
)
def test_examples_can_run_as_scripts_without_package_import_errors(script_path: str):
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)

    completed = subprocess.run(
        [_python_executable(), script_path],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "ModuleNotFoundError" not in completed.stderr
    assert "OPENAI_API_KEY is required." in completed.stderr


def test_twilio_example_factory():
    if importlib.util.find_spec("fastapi") is None:
        pytest.skip("fastapi not installed")
    if importlib.util.find_spec("agents") is None:
        pytest.skip("openai-agents not installed")
    import examples.twilio_app as twilio_app

    app = twilio_app.create_app(api_key="test-key", stream_url="wss://example.com/stream")
    assert app is not None


def test_example_session_smoke():
    config = EasyCatConfig(
        openai_api_key="test-key",
        transport=WebSocketTransportConfig(),
        agent=_DummyAgent(),
    )
    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise
    assert session is not None
