from __future__ import annotations

import importlib.util
import sys
import types

import pytest

from easycat import EasyCatConfig, WebSocketTransportConfig, create_session


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


def test_build_openai_agents_adapter_prefers_responses_websocket(monkeypatch: pytest.MonkeyPatch):
    from examples.common import build_openai_agents_adapter

    class DummyAgent:
        def __init__(self, name: str, instructions: str) -> None:
            self.name = name
            self.instructions = instructions

    class DummyOpenAIProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class DummyRunConfig:
        def __init__(self, *, model_provider):
            self.model_provider = model_provider

    fake_agents = types.SimpleNamespace(
        Agent=DummyAgent,
        OpenAIProvider=DummyOpenAIProvider,
        RunConfig=DummyRunConfig,
    )
    monkeypatch.setitem(sys.modules, "agents", fake_agents)

    adapter = build_openai_agents_adapter(instructions="hello")
    provider_kwargs = adapter._run_config.model_provider.kwargs
    assert provider_kwargs["use_responses"] is True
    assert provider_kwargs["use_responses_websocket"] is True


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
        def __init__(self, *, model_provider):
            self.model_provider = model_provider

    called: list[str] = []

    def set_default_openai_api(api: str) -> None:
        called.append(api)

    fake_agents = types.SimpleNamespace(
        Agent=DummyAgent,
        OpenAIProvider=FailingProvider,
        RunConfig=DummyRunConfig,
        set_default_openai_api=set_default_openai_api,
    )
    monkeypatch.setitem(sys.modules, "agents", fake_agents)

    adapter = build_openai_agents_adapter(instructions="hello")
    assert adapter._run_config is None
    assert called == ["responses"]


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
