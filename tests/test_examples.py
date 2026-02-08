from __future__ import annotations

import importlib.util

import pytest

from easycat import EasyCatConfig, WebSocketTransportConfig, create_session


def test_local_chat_example_imports():
    import examples.local_chat as local_chat

    assert callable(local_chat.main)


def test_ws_server_example_imports():
    import examples.ws_server as ws_server

    assert callable(ws_server.main)

def test_pydantic_ai_example_imports():
    import examples.pydantic_ai_voice as pydantic_example

    assert callable(pydantic_example.main)


def test_twilio_example_factory():
    if importlib.util.find_spec("fastapi") is None:
        pytest.skip("fastapi not installed")
    import examples.twilio_app as twilio_app

    app = twilio_app.create_app(api_key="test-key", stream_url="wss://example.com/stream")
    assert app is not None


def test_example_session_smoke():
    config = EasyCatConfig(
        openai_api_key="test-key",
        transport=WebSocketTransportConfig(),
    )
    session = create_session(config)
    assert session is not None
