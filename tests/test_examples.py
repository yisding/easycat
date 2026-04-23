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


def test_openai_agents_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("agents")
    # The example uses ``easycat.run(...)`` at module scope; stub it so
    # importing the module doesn't block on a real voice session.  Evict
    # any cached copy first so the fresh import always runs under the
    # monkeypatched ``run`` — otherwise a prior test that imported the
    # example would leave a stale module object behind.
    import easycat

    monkeypatch.setattr(easycat, "run", lambda config: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    sys.modules.pop("examples.openai_agents_voice", None)

    import examples.openai_agents_voice  # noqa: F401


def test_ws_server_example_imports():
    import examples.ws_server as ws_server

    assert callable(ws_server.main)


def test_ws_supervisor_server_example_imports():
    import examples.ws_supervisor_server as ws_supervisor_server

    assert callable(ws_supervisor_server.main)


def test_pydantic_ai_example_imports(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("pydantic_ai")
    import easycat

    monkeypatch.setattr(easycat, "run", lambda config: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    sys.modules.pop("examples.pydantic_ai_voice", None)

    import examples.pydantic_ai_voice  # noqa: F401


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


def test_deepgram_voice_example_imports():
    import examples.deepgram_voice as deepgram_voice

    assert callable(deepgram_voice.main)


def test_elevenlabs_voice_example_imports():
    import examples.elevenlabs_voice as elevenlabs_voice

    assert callable(elevenlabs_voice.main)


def test_cartesia_voice_example_imports():
    import examples.cartesia_voice as cartesia_voice

    assert callable(cartesia_voice.main)


def test_output_processors_example_imports():
    import examples.output_processors as output_processors

    assert callable(output_processors.main)


def test_agent_event_subscription_example_imports():
    import examples.agent_event_subscription as agent_event_subscription

    assert callable(agent_event_subscription.main)


def test_vad_backends_example_imports():
    import examples.vad_backends as vad_backends

    assert callable(vad_backends.main)


def test_noise_reduction_backends_example_imports():
    import examples.noise_reduction_backends as noise_reduction_backends

    assert callable(noise_reduction_backends.main)


def test_responses_api_bridge_example_imports():
    import examples.responses_api_bridge as responses_api_bridge

    assert callable(responses_api_bridge.main)


def test_echo_cancellation_example_imports():
    import examples.echo_cancellation as echo_cancellation

    assert callable(echo_cancellation.main)


def test_reconnecting_ws_client_example_imports():
    import examples.reconnecting_ws_client as reconnecting_ws_client

    assert callable(reconnecting_ws_client.main)


def test_telephony_helpers_example_imports():
    import examples.telephony_helpers as telephony_helpers

    assert callable(telephony_helpers.main)


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
        "examples/deepgram_voice.py",
        "examples/elevenlabs_voice.py",
        "examples/debug_bundle.py",
        "examples/custom_stt_provider.py",
        "examples/custom_tts_provider.py",
        "examples/custom_vad_provider.py",
        "examples/output_processors.py",
        "examples/agent_event_subscription.py",
        "examples/vad_backends.py",
        "examples/noise_reduction_backends.py",
        "examples/responses_api_bridge.py",
        "examples/echo_cancellation.py",
    ],
)
def test_examples_can_run_as_scripts_without_package_import_errors(script_path: str):
    # Skip examples whose optional agent framework isn't installed —
    # this test is about EasyCat's own import graph, not the agent
    # framework's availability.
    if "openai_agents" in script_path or "function_tools_openai" in script_path:
        pytest.importorskip("agents")
    if "pydantic_ai" in script_path or "function_tools_pydantic" in script_path:
        pytest.importorskip("pydantic_ai")
    if "session_actions_pydantic" in script_path:
        pytest.importorskip("pydantic_ai")

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
    # Examples using ``easycat.run(...)`` / ``EasyCatConfig.*()`` fail at
    # config validation with "STT configuration is required." when no
    # provider env var is set; older examples still call ``require_env``
    # and emit "OPENAI_API_KEY is required." — accept either so the
    # smoke test stays meaningful while examples migrate to ``run()``.
    assert (
        "OPENAI_API_KEY is required." in completed.stderr
        or "STT configuration is required." in completed.stderr
    )


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
