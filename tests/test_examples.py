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


def _load_slim_example(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    *,
    framework: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Import a slim example that runs ``easycat.run(...)`` at module scope.

    Stubs ``easycat.run`` so importing doesn't block on a real session,
    sets any env vars the example consumes via ``require_env`` at module
    scope, and evicts any cached copy so the fresh import sees the
    monkeypatched ``run``.
    """
    if framework:
        pytest.importorskip(framework)

    import easycat

    monkeypatch.setattr(easycat, "run", lambda config: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    sys.modules.pop(module_name, None)
    __import__(module_name)


# ── Slim examples (module-scope ``easycat.run(...)``) ────────────────


def test_openai_agents_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.openai_agents_voice", framework="agents")


def test_pydantic_ai_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.pydantic_ai_voice", framework="pydantic_ai")


def test_function_tools_openai_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.function_tools_openai", framework="agents")


def test_function_tools_pydantic_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.function_tools_pydantic", framework="pydantic_ai")


def test_smart_turn_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.smart_turn_demo", framework="agents")


def test_echo_cancellation_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.echo_cancellation", framework="agents")


def test_output_processors_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.output_processors", framework="agents")


def test_noise_reduction_backends_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.noise_reduction_backends", framework="agents")


def test_cartesia_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(
        monkeypatch,
        "examples.cartesia_voice",
        framework="agents",
        env={"CARTESIA_API_KEY": "test-key"},
    )


def test_deepgram_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(
        monkeypatch,
        "examples.deepgram_voice",
        framework="agents",
        env={"DEEPGRAM_API_KEY": "test-key"},
    )


def test_elevenlabs_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(
        monkeypatch,
        "examples.elevenlabs_voice",
        framework="agents",
        env={"ELEVENLABS_API_KEY": "test-key"},
    )


def test_combined_providers_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(
        monkeypatch,
        "examples.combined_providers",
        framework="agents",
        env={"DEEPGRAM_API_KEY": "test-key", "ELEVENLABS_API_KEY": "test-key"},
    )


def test_responses_api_bridge_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(
        monkeypatch,
        "examples.responses_api_bridge",
        env={
            "EASYCAT_REMOTE_AGENT_BASE_URL": "https://example.com",
            "EASYCAT_REMOTE_AGENT_API_KEY": "test-key",
            "EASYCAT_REMOTE_AGENT_MODEL": "test-model",
        },
    )


def test_session_actions_openai_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.session_actions_openai", framework="agents")


def test_session_actions_pydantic_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.session_actions_pydantic", framework="pydantic_ai")


def test_pydantic_ai_workflow_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.pydantic_ai_workflow_voice", framework="pydantic_ai")


# ── Examples that still use ``def main()`` ──────────────────────────


def test_ws_server_example_imports():
    import examples.ws_server as ws_server

    assert callable(ws_server.main)


def test_ws_supervisor_server_example_imports():
    import examples.ws_supervisor_server as ws_supervisor_server

    assert callable(ws_supervisor_server.main)


def test_webrtc_observability_example_imports():
    pytest.importorskip("agents")
    import examples.webrtc_observability_server as webrtc_observability

    assert callable(webrtc_observability.main)


def test_push_to_talk_example_imports():
    import examples.push_to_talk as push_to_talk

    assert callable(push_to_talk.main)


def test_custom_tts_provider_example_imports():
    import examples.custom_tts_provider as custom_tts_provider

    assert callable(custom_tts_provider.main)


def test_custom_vad_provider_example_imports():
    import examples.custom_vad_provider as custom_vad_provider

    assert callable(custom_vad_provider.main)


def test_custom_stt_provider_example_imports():
    import examples.custom_stt_provider as custom_stt_provider

    assert callable(custom_stt_provider.main)


def test_agent_event_subscription_example_imports():
    pytest.importorskip("agents")
    import examples.agent_event_subscription as agent_event_subscription

    assert callable(agent_event_subscription.main)


def test_vad_backends_example_imports():
    import examples.vad_backends as vad_backends

    assert callable(vad_backends.main)


def test_reconnecting_ws_client_example_imports():
    import examples.reconnecting_ws_client as reconnecting_ws_client

    assert callable(reconnecting_ws_client.main)


def test_telephony_helpers_example_imports():
    import examples.telephony_helpers as telephony_helpers

    assert callable(telephony_helpers.main)


def test_debug_bundle_example_imports():
    import examples.debug_bundle as debug_bundle

    assert callable(debug_bundle.main)


def test_journal_ui_example_imports():
    pytest.importorskip("agents")
    import examples.journal_ui as journal_ui

    assert callable(journal_ui.main)


# ── Subprocess smoke test ───────────────────────────────────────────

# Scripts that import an optional agent framework at module scope; the
# subprocess test must skip when the framework isn't installed,
# otherwise the script exits before reaching the OPENAI_API_KEY check.
_REQUIRES_AGENTS = frozenset(
    {
        "examples/openai_agents_voice.py",
        "examples/function_tools_openai.py",
        "examples/smart_turn_demo.py",
        "examples/combined_providers.py",
        "examples/cartesia_voice.py",
        "examples/deepgram_voice.py",
        "examples/elevenlabs_voice.py",
        "examples/output_processors.py",
        "examples/agent_event_subscription.py",
        "examples/noise_reduction_backends.py",
        "examples/echo_cancellation.py",
        "examples/session_actions_openai.py",
        "examples/journal_ui.py",
        "examples/webrtc_observability_server.py",
    }
)
_REQUIRES_PYDANTIC_AI = frozenset(
    {
        "examples/pydantic_ai_voice.py",
        "examples/function_tools_pydantic.py",
        "examples/session_actions_pydantic.py",
        "examples/pydantic_ai_workflow_voice.py",
    }
)


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
        "examples/session_actions_openai.py",
        "examples/session_actions_pydantic.py",
        "examples/pydantic_ai_workflow_voice.py",
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
        "examples/journal_ui.py",
    ],
)
def test_examples_can_run_as_scripts_without_package_import_errors(script_path: str):
    if script_path in _REQUIRES_AGENTS:
        pytest.importorskip("agents")
    if script_path in _REQUIRES_PYDANTIC_AI:
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
    # provider env var is set; others call ``require_env`` and emit
    # "OPENAI_API_KEY is required." — accept either.
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
