from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from easycat import EasyCatConfig, WebSocketTransportConfig, create_session

REPO_ROOT = Path(__file__).resolve().parents[1]


class _DummyAgent:
    async def run(self, text: str) -> str:
        return text


def test_openai_agents_voice_example_imports():
    import examples.openai_agents_voice as openai_agents_voice

    assert callable(openai_agents_voice.main)


def test_ws_server_example_imports():
    import examples.ws_server as ws_server

    assert callable(ws_server.main)


def test_ws_supervisor_server_example_imports():
    import examples.ws_supervisor_server as ws_supervisor_server

    assert callable(ws_supervisor_server.main)


def test_pydantic_ai_example_imports():
    import examples.pydantic_ai_voice as pydantic_example

    assert callable(pydantic_example.main)


def test_webrtc_observability_example_imports():
    import examples.webrtc_observability_server as webrtc_observability

    assert callable(webrtc_observability.main)


def test_function_tools_openai_example_imports():
    import examples.function_tools_openai as function_tools_openai

    assert callable(function_tools_openai.main)


def test_function_tools_pydantic_example_imports():
    import examples.function_tools_pydantic as function_tools_pydantic

    assert callable(function_tools_pydantic.main)


def test_push_to_talk_example_imports():
    import examples.push_to_talk as push_to_talk

    assert callable(push_to_talk.main)


def test_smart_turn_example_imports():
    import examples.smart_turn_demo as smart_turn_demo

    assert callable(smart_turn_demo.main)


def test_combined_providers_example_imports():
    import examples.combined_providers as combined_providers

    assert callable(combined_providers.main)


def test_custom_tts_provider_example_imports():
    import examples.custom_tts_provider as custom_tts_provider

    assert callable(custom_tts_provider.main)


def test_custom_vad_provider_example_imports():
    import examples.custom_vad_provider as custom_vad_provider

    assert callable(custom_vad_provider.main)


def test_custom_stt_provider_example_imports():
    import examples.custom_stt_provider as custom_stt_provider

    assert callable(custom_stt_provider.main)


def test_deepgram_stt_example_imports():
    import examples.deepgram_stt as deepgram_stt

    assert callable(deepgram_stt.main)


def test_elevenlabs_tts_example_imports():
    import examples.elevenlabs_tts as elevenlabs_tts

    assert callable(elevenlabs_tts.main)


def test_cartesia_voice_example_imports():
    import examples.cartesia_voice as cartesia_voice

    assert callable(cartesia_voice.main)


def test_debug_bundle_example_imports():
    import examples.debug_bundle as debug_bundle

    assert callable(debug_bundle.main)


def test_pydantic_ai_workflow_voice_example_imports():
    pytest.importorskip("pydantic")
    import examples.pydantic_ai_workflow_voice as pydantic_workflow

    assert callable(pydantic_workflow.main)


def test_session_actions_openai_example_imports():
    pytest.importorskip("agents")
    import examples.session_actions_openai as session_actions_openai

    assert callable(session_actions_openai.main)


def test_session_actions_pydantic_example_imports():
    import examples.session_actions_pydantic as session_actions_pydantic

    assert callable(session_actions_pydantic.main)


def test_langchain_voice_example_imports():
    import examples.langchain_voice as langchain_voice

    assert callable(langchain_voice.main)


def test_langgraph_voice_example_imports():
    import examples.langgraph_voice as langgraph_voice

    assert callable(langgraph_voice.main)


def test_function_tools_langchain_example_imports():
    import examples.function_tools_langchain as function_tools_langchain

    assert callable(function_tools_langchain.main)


def test_function_tools_langgraph_example_imports():
    import examples.function_tools_langgraph as function_tools_langgraph

    assert callable(function_tools_langgraph.main)


def test_session_actions_langchain_example_imports():
    import examples.session_actions_langchain as session_actions_langchain

    assert callable(session_actions_langchain.main)


def test_session_actions_langgraph_example_imports():
    import examples.session_actions_langgraph as session_actions_langgraph

    assert callable(session_actions_langgraph.main)


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
        "examples/openai_agents_voice.py",
        "examples/ws_server.py",
        "examples/ws_supervisor_server.py",
        "examples/ws_browser_example.py",
        "examples/webrtc_server.py",
        "examples/webrtc_observability_server.py",
        "examples/pydantic_ai_voice.py",
        "examples/function_tools_openai.py",
        "examples/function_tools_pydantic.py",
        "examples/session_actions_pydantic.py",
        "examples/push_to_talk.py",
        "examples/smart_turn_demo.py",
        "examples/combined_providers.py",
        "examples/cartesia_voice.py",
        "examples/deepgram_stt.py",
        "examples/elevenlabs_tts.py",
        "examples/debug_bundle.py",
        "examples/custom_stt_provider.py",
        "examples/custom_tts_provider.py",
        "examples/custom_vad_provider.py",
        "examples/langchain_voice.py",
        "examples/langgraph_voice.py",
        "examples/function_tools_langchain.py",
        "examples/function_tools_langgraph.py",
        "examples/session_actions_langchain.py",
        "examples/session_actions_langgraph.py",
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
