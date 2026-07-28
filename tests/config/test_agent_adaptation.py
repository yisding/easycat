from __future__ import annotations

import pytest

from easycat import (
    EasyConfig,
    create_session,
)
from easycat.config import TextSessionConfig, _factory
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.stubs import NoopAgent
from easycat.turn_manager import TurnManagerConfig
from tests.config._helpers import (
    _DummyAgent,
    _stub_audio_backends,
)


def test_easycat_config_wraps_agent():
    class DummyAgent:
        async def run(self, text: str) -> str:
            return text

    config = EasyConfig(openai_api_key="test-key", agent=DummyAgent())
    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise
    assert isinstance(session.agent, AgentRunner)


def test_create_session_auto_adapts_openai_agents():
    agents_mod = pytest.importorskip("agents")
    from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

    raw = agents_mod.Agent(name="test", instructions="hi")
    config = EasyConfig(openai_api_key="test-key", agent=raw)
    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise

    assert isinstance(session.agent, AgentRunner)
    assert isinstance(session.agent._agent, OpenAIAgentsBridge)


def test_create_session_auto_adapts_pydantic_agents():
    pydantic_ai_mod = pytest.importorskip("pydantic_ai")
    from pydantic_ai.models.test import TestModel

    from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

    raw = pydantic_ai_mod.Agent(TestModel(custom_output_text="ok"))
    config = EasyConfig(openai_api_key="test-key", agent=raw)
    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise

    assert isinstance(session.agent, AgentRunner)
    assert isinstance(session.agent._agent, PydanticAIBridge)


def test_inject_agent_runtime_uses_configure_runtime_surface():
    """A bridge declaring configure_runtime gets settings via that method."""
    from easycat.config import _inject_agent_runtime

    class _Bridge:
        def __init__(self) -> None:
            self.seen: dict[str, object] = {}

        def configure_runtime(self, *, mcp_servers=None, model=None, api_key=None):
            self.seen = {"mcp_servers": mcp_servers, "model": model, "api_key": api_key}

    bridge = _Bridge()
    _inject_agent_runtime(
        bridge,
        mcp_servers=("stdio://srv",),
        agent_model="gpt-x",
        remote_agent_api_key="key-123",
    )
    assert bridge.seen == {
        "mcp_servers": ["stdio://srv"],
        "model": "gpt-x",
        "api_key": "key-123",
    }


def test_inject_agent_runtime_falls_back_to_private_attrs():
    """A bridge without configure_runtime still gets _mcp_servers (back-compat)."""
    from easycat.config import _inject_agent_runtime

    class _LegacyBridge:
        def __init__(self) -> None:
            self._mcp_servers = None

    bridge = _LegacyBridge()
    _inject_agent_runtime(bridge, mcp_servers=("stdio://srv",))
    assert bridge._mcp_servers == ["stdio://srv"]


def test_inject_agent_runtime_clears_mcp_when_empty():
    """An empty mcp_servers must overwrite a stale list (no leak across sessions)."""
    from easycat.config import _inject_agent_runtime

    class _Bridge:
        def __init__(self) -> None:
            self.mcp = ["stale"]

        def configure_runtime(self, *, mcp_servers=None, model=None, api_key=None):
            if mcp_servers is not None:
                self.mcp = list(mcp_servers)

    bridge = _Bridge()
    _inject_agent_runtime(bridge, mcp_servers=())
    assert bridge.mcp == []


def test_shared_agent_resolver_preserves_audio_and_text_defaults():
    audio_config = EasyConfig(openai_api_key="test-key", agent=None)
    text_config = TextSessionConfig(agent=None)

    assert _factory._resolve_agent(audio_config, ()) is None

    text_agent = _factory._resolve_agent(
        text_config,
        (),
        default_agent=NoopAgent(),
    )
    assert isinstance(text_agent, AgentRunner)
    assert isinstance(text_agent._agent, NoopAgent)

    unwrapped_text_agent = _factory._resolve_agent(
        TextSessionConfig(agent=None, wrap_agent=False),
        (),
        default_agent=NoopAgent(),
    )
    assert isinstance(unwrapped_text_agent, NoopAgent)


def test_shared_agent_resolver_configures_text_agent_before_wrapping():
    class RuntimeAwareAgent:
        def __init__(self) -> None:
            self.runtime: dict[str, object] = {}

        async def run(self, text: str) -> str:
            return text

        def configure_runtime(self, *, mcp_servers=None, model=None, api_key=None):
            self.runtime = {
                "mcp_servers": mcp_servers,
                "model": model,
                "api_key": api_key,
            }

    raw = RuntimeAwareAgent()
    config = TextSessionConfig(
        agent=raw,
        agent_model="gpt-test",
        remote_agent_api_key="secret",
        mcp_servers=["stdio://server"],
    )

    resolved = _factory._resolve_agent(config, ("stdio://server",))

    assert isinstance(resolved, AgentRunner)
    assert resolved._agent is raw
    assert raw.runtime == {
        "mcp_servers": ["stdio://server"],
        "model": "gpt-test",
        "api_key": "secret",
    }


def test_create_session_does_not_mutate_turn_taking_config():
    turn_cfg = TurnManagerConfig(endpoint_detector=None)
    config = EasyConfig(
        openai_api_key="test-key",
        turn_taking=turn_cfg,
        agent=_DummyAgent(),
    )
    try:
        create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise

    assert config.turn_taking.endpoint_detector is None


def test_create_session_rejects_bogus_agent(monkeypatch: pytest.MonkeyPatch):
    from easycat.config import EasyConfigError

    _stub_audio_backends(monkeypatch)
    config = EasyConfig(openai_api_key="test-key", agent=object())
    with pytest.raises(EasyConfigError, match=r"async run"):
        create_session(config)


def test_create_session_rejects_sync_run_agent(monkeypatch: pytest.MonkeyPatch):
    from easycat.config import EasyConfigError

    _stub_audio_backends(monkeypatch)

    class SyncRunAgent:
        def run(self, text: str) -> str:
            return text

    config = EasyConfig(openai_api_key="test-key", agent=SyncRunAgent())
    with pytest.raises(EasyConfigError, match=r"async run"):
        create_session(config)


def test_create_session_accepts_valid_async_run_agent(monkeypatch: pytest.MonkeyPatch):
    _stub_audio_backends(monkeypatch)
    config = EasyConfig(openai_api_key="test-key", agent=_DummyAgent())
    session = create_session(config)
    assert isinstance(session.agent, AgentRunner)


def test_create_session_skips_agent_check_when_wrap_agent_false(monkeypatch: pytest.MonkeyPatch):
    # wrap_agent=False is the deliberate custom-bridge escape hatch — the
    # shape check is skipped so a non-Agent object passes construction
    # without raising EasyConfigError.
    _stub_audio_backends(monkeypatch)
    config = EasyConfig(openai_api_key="test-key", agent=object(), wrap_agent=False)
    session = create_session(config)
    assert session is not None
