from __future__ import annotations

import pytest

from easycat import EasyCatConfig, create_session
from easycat.agent_runner import AgentRunner
from easycat.agents import OpenAIAgentsAdapter, PydanticAIAdapter
from easycat.config import TelephonyConfig
from easycat.events import DTMFAggregated
from easycat.stt.openai_provider import OpenAISTTConfig
from easycat.telephony.dtmf import emit_twilio_dtmf
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig


class _DummyAgent:
    async def run(self, text: str) -> str:
        return text


def test_easycat_config_requires_stt_tts():
    with pytest.raises(ValueError):
        EasyCatConfig()


def test_easycat_config_openai_defaults():
    config = EasyCatConfig(openai_api_key="test-key")
    assert isinstance(config.stt, OpenAISTTConfig)
    assert isinstance(config.tts, OpenAITTSConfig)


def test_easycat_config_wraps_agent():
    class DummyAgent:
        async def run(self, text: str) -> str:
            return text

    config = EasyCatConfig(openai_api_key="test-key", agent=DummyAgent())
    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise
    assert isinstance(session.agent, AgentRunner)


def test_create_session_auto_adapts_openai_agents():
    agents_mod = pytest.importorskip("agents")

    raw = agents_mod.Agent(name="test", instructions="hi")
    config = EasyCatConfig(openai_api_key="test-key", agent=raw)
    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise

    assert isinstance(session.agent, AgentRunner)
    assert isinstance(session.agent._agent, OpenAIAgentsAdapter)


def test_create_session_auto_adapts_pydantic_agents():
    pydantic_ai_mod = pytest.importorskip("pydantic_ai")

    raw = pydantic_ai_mod.Agent("openai:gpt-4o-mini")
    config = EasyCatConfig(openai_api_key="test-key", agent=raw)
    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise

    assert isinstance(session.agent, AgentRunner)
    assert isinstance(session.agent._agent, PydanticAIAdapter)


def test_create_session_does_not_mutate_turn_taking_config():
    turn_cfg = TurnManagerConfig(endpoint_detector=None)
    config = EasyCatConfig(
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


@pytest.mark.asyncio
async def test_telephony_helpers_are_managed_by_session_lifecycle():
    config = EasyCatConfig(
        openai_api_key="test-key",
        telephony=TelephonyConfig(enable_dtmf_aggregator=True),
        agent=_DummyAgent(),
    )
    config.smart_turn.enabled = False

    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise
    bus = session.event_bus
    aggregated: list[DTMFAggregated] = []
    bus.subscribe(DTMFAggregated, lambda e: aggregated.append(e))

    # Telephony helpers must be started (normally done by session.start())
    for helper in session._telephony_helpers:
        helper.start()

    await emit_twilio_dtmf({"event": "dtmf", "dtmf": {"digit": "1"}}, bus)
    await emit_twilio_dtmf({"event": "dtmf", "dtmf": {"digit": "#"}}, bus)
    assert aggregated

    for helper in session._telephony_helpers:
        helper.stop()

    aggregated.clear()
    await emit_twilio_dtmf({"event": "dtmf", "dtmf": {"digit": "1"}}, bus)
    await emit_twilio_dtmf({"event": "dtmf", "dtmf": {"digit": "#"}}, bus)
    assert not aggregated
