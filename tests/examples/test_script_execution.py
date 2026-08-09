from __future__ import annotations

from tests.examples._examples_helpers import (
    _REQUIRES_AGENTS,
    _REQUIRES_LANGCHAIN,
    _REQUIRES_LANGCHAIN_OPENAI,
    _REQUIRES_LANGGRAPH,
    _REQUIRES_PYDANTIC_AI,
    REPO_ROOT,
    _python_executable,
    _skip_unless_langchain_v0,
    os,
    pytest,
    subprocess,
)


@pytest.mark.parametrize(
    "script_path",
    [
        "examples/voice_app.py",
        "examples/voice_app_twilio.py",
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
        "examples/custom_transport.py",
        "examples/output_processors.py",
        "examples/agent_event_subscription.py",
        "examples/vad_backends.py",
        "examples/noise_reduction_backends.py",
        "examples/responses_api_bridge.py",
        "examples/echo_cancellation.py",
        "examples/journal_ui.py",
        "examples/langchain_voice.py",
        "examples/langgraph_voice.py",
        "examples/function_tools_langchain.py",
        "examples/function_tools_langgraph.py",
        "examples/session_actions_langchain.py",
        "examples/session_actions_langgraph.py",
    ],
)
def test_examples_can_run_as_scripts_without_package_import_errors(script_path: str):
    if script_path in _REQUIRES_AGENTS:
        pytest.importorskip("agents")
    if script_path in _REQUIRES_PYDANTIC_AI:
        pytest.importorskip("pydantic_ai")
    if script_path in _REQUIRES_LANGCHAIN_OPENAI:
        pytest.importorskip("langchain_openai")
    if script_path in _REQUIRES_LANGCHAIN:
        _skip_unless_langchain_v0()
    if script_path in _REQUIRES_LANGGRAPH:
        pytest.importorskip("langgraph")

    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)

    completed = subprocess.run(
        [_python_executable(), script_path],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode != 0
    assert "ModuleNotFoundError" not in completed.stderr
    # Examples using ``easycat.run(...)`` / ``EasyConfig.mic|browser|phone()``
    # now route the no-key path through the error catalog, failing config
    # validation with ``EASYCAT_E203: Missing API key: OPENAI_API_KEY`` when
    # no provider env var is set. A few still surface the bare
    # "STT configuration is required." (e.g. an explicit non-key config gap),
    # and others call ``require_env`` and emit "OPENAI_API_KEY is required."
    # PydanticAI validates the key at ``Agent(...)`` construction and raises
    # its own provider-specific message before EasyCat config validation runs.
    assert (
        "OPENAI_API_KEY is required." in completed.stderr
        or "STT configuration is required." in completed.stderr
        or "Missing API key: OPENAI_API_KEY" in completed.stderr
        or "set the `OPENAI_API_KEY` or `OPENAI_ADMIN_KEY` environment variable"
        in completed.stderr
        or "Set the `OPENAI_API_KEY` environment variable" in completed.stderr
    )
