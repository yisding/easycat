from __future__ import annotations

from tests.examples._examples_helpers import (
    REPO_ROOT,
    EasyConfig,
    WebSocketTransportConfig,
    _DummyAgent,
    _load_slim_example,
    _skip_unless_langchain_v0,
    _top_level_example_names,
    _visible_code_line_count,
    ast,
    create_session,
    importlib,
    pytest,
)


def test_openai_agents_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.openai_agents_voice", framework="agents")


def test_top_level_examples_do_not_alias_easycat_imports() -> None:
    aliased: list[str] = []

    for example_name in sorted(_top_level_example_names()):
        path = REPO_ROOT / "examples" / example_name
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and alias.name.startswith("easycat"):
                        aliased.append(f"{example_name}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module == "easycat":
                for alias in node.names:
                    if alias.asname:
                        aliased.append(
                            f"{example_name}:{node.lineno}: from easycat import {alias.name}"
                        )

    assert not aliased, "Top-level examples should teach EasyCat names without aliases: " + (
        "; ".join(aliased)
    )


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


def test_session_actions_pydantic_model_can_be_overridden_by_env() -> None:
    path = REPO_ROOT / "examples/session_actions_pydantic.py"
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    agent_calls = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Agent"
    ]

    assert len(agent_calls) == 1
    model_arg = agent_calls[0].args[0]
    assert isinstance(model_arg, ast.Call)
    assert isinstance(model_arg.func, ast.Attribute)
    assert isinstance(model_arg.func.value, ast.Name)
    assert model_arg.func.value.id == "os"
    assert model_arg.func.attr == "getenv"
    assert [arg.value for arg in model_arg.args if isinstance(arg, ast.Constant)] == [
        "PYDANTIC_AI_MODEL",
        "openai:gpt-5.2",
    ]


def test_pydantic_ai_workflow_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.pydantic_ai_workflow_voice", framework="pydantic_ai")


def test_push_to_talk_example_imports():
    from examples import push_to_talk

    assert callable(push_to_talk.main)


def test_custom_tts_provider_example_imports():
    from examples import custom_tts_provider

    assert callable(custom_tts_provider.main)


def test_custom_vad_provider_example_imports():
    from examples import custom_vad_provider

    assert callable(custom_vad_provider.main)


def test_custom_stt_provider_example_imports():
    from examples import custom_stt_provider

    assert callable(custom_stt_provider.main)


def test_custom_transport_example_imports():
    from examples import custom_transport

    assert callable(custom_transport.main)


def test_custom_transport_preserves_local_optional_capabilities() -> None:
    from easycat.transports import LocalTransport
    from examples.custom_transport import CountingTransport

    inner = LocalTransport()
    wrapped = CountingTransport(inner)

    for attribute in (
        "transport_kind",
        "send_audio_is_nonblocking",
        "reports_audio_delivery",
        "drain_aec_reference_frames",
        "pending_playout_ms",
        "set_event_bus",
        "set_session_id",
        "set_runtime_scope",
    ):
        assert getattr(wrapped, attribute) == getattr(inner, attribute)


def test_agent_event_subscription_example_imports():
    pytest.importorskip("agents")
    from examples import agent_event_subscription

    assert callable(agent_event_subscription.main)


def test_vad_backends_example_imports():
    from examples import vad_backends

    assert callable(vad_backends.main)


def test_reconnecting_ws_client_example_imports():
    from examples import reconnecting_ws_client

    assert callable(reconnecting_ws_client.main)


def test_telephony_helpers_example_imports():
    from examples import telephony_helpers

    assert callable(telephony_helpers.main)


def test_debug_bundle_example_imports():
    from examples import debug_bundle

    assert callable(debug_bundle.main)


def test_journal_ui_example_imports():
    pytest.importorskip("agents")
    from examples import journal_ui

    assert callable(journal_ui.main)


def test_langchain_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    _load_slim_example(monkeypatch, "examples.langchain_voice", framework="langchain_openai")


def test_langgraph_voice_example_imports(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("langgraph")
    _load_slim_example(monkeypatch, "examples.langgraph_voice", framework="langchain_openai")


def test_function_tools_langchain_example_imports(monkeypatch: pytest.MonkeyPatch):
    _skip_unless_langchain_v0()
    _load_slim_example(
        monkeypatch, "examples.function_tools_langchain", framework="langchain_openai"
    )


def test_function_tools_langgraph_example_imports(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("langgraph")
    _load_slim_example(
        monkeypatch, "examples.function_tools_langgraph", framework="langchain_openai"
    )


def test_session_actions_langchain_example_imports(monkeypatch: pytest.MonkeyPatch):
    _skip_unless_langchain_v0()
    _load_slim_example(
        monkeypatch, "examples.session_actions_langchain", framework="langchain_openai"
    )


def test_session_actions_langgraph_example_imports(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("langgraph")
    _load_slim_example(
        monkeypatch, "examples.session_actions_langgraph", framework="langchain_openai"
    )


def test_twilio_example_factory(monkeypatch: pytest.MonkeyPatch):
    if importlib.util.find_spec("fastapi") is None:
        pytest.skip("fastapi not installed")
    if importlib.util.find_spec("agents") is None:
        pytest.skip("openai-agents not installed")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twilio-test-token")
    from examples import twilio_app

    app = twilio_app.create_app(api_key="test-key", stream_url="wss://example.com/stream")
    assert app is not None


def test_twilio_example_uses_manager_feedback_lifecycle():
    path = REPO_ROOT / "examples" / "twilio_app.py"
    source = path.read_text(encoding="utf-8")

    # Authentication preflight, bounded capacity, and idempotent call-bound
    # grants are intentionally visible in this maintained production example.
    assert _visible_code_line_count(path) <= 180
    assert "WebSocketSessionRuntime(" in source
    assert "runtime_feedback=True" in source
    assert "TwilioCallSessionIndex" in source
    assert "session_slots" not in source
    assert "twilio_websocket_signature_process_request" in source
    assert "twilio_app_settings_from_env" in source
    assert "twilio_form_items_from_request" in source
    assert "twilio_stream_parameters_from_form" in source
    assert "os.getenv" not in source
    assert "parse_qsl" not in source
    assert "validate_twilio_webhook_signature" not in source
    assert "attach_runtime_feedback" not in source


def test_twilio_example_missing_openai_key_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
):
    from examples import twilio_app

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        twilio_app.create_app(stream_url="wss://example.com/stream")

    message = str(exc_info.value)
    assert "OPENAI_API_KEY is required." in message
    assert "uv run easycat doctor" in message
    assert "uv run easycat doctor --env-file .env" in message


def test_twilio_example_missing_auth_token_is_actionable(monkeypatch: pytest.MonkeyPatch):
    from examples import twilio_app

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="TWILIO_AUTH_TOKEN is required"):
        twilio_app.create_app(stream_url="wss://example.com/stream")


def test_example_session_smoke():
    config = EasyConfig(
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


def test_function_tool_timezone_examples_handle_malformed_zoneinfo_keys() -> None:
    examples = [
        "agent_event_subscription.py",
        "function_tools_langchain.py",
        "function_tools_langgraph.py",
        "function_tools_openai.py",
        "function_tools_pydantic.py",
    ]

    for example in examples:
        path = REPO_ROOT / "examples" / example
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        handlers: set[str] | None = None
        for node in ast.walk(module):
            if not isinstance(node, ast.Try):
                continue
            calls_zoneinfo = any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "ZoneInfo"
                for child in ast.walk(node)
            )
            if calls_zoneinfo:
                handlers = set()
                for handler in node.handlers:
                    exc_type = handler.type
                    if isinstance(exc_type, ast.Name):
                        handlers.add(exc_type.id)
                    elif isinstance(exc_type, ast.Tuple):
                        handlers.update(
                            elt.id for elt in exc_type.elts if isinstance(elt, ast.Name)
                        )
                break

        assert handlers is not None, f"{example} should guard ZoneInfo(timezone_name)"
        assert {"ZoneInfoNotFoundError", "ValueError"} <= handlers
