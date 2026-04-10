from __future__ import annotations

import logging

import pytest

from easycat import EasyCatConfig, create_session
from easycat.config import TelephonyConfig
from easycat.echo_cancellation import EchoCancellationConfig
from easycat.events import DTMFAggregated
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.smart_turn import SmartTurnConfig
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.openai_provider import OpenAISTTConfig
from easycat.telephony.dtmf import emit_twilio_dtmf
from easycat.telephony.session_actions import (
    TwilioSessionActionConfig,
    TwilioSessionActionExecutor,
)
from easycat.transports.local import LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransportConfig
from easycat.transports.webrtc import WebRTCTransportConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig, TurnMode


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


def test_easycat_config_echo_cancellation_defaults_for_local_and_websocket():
    local = EasyCatConfig(openai_api_key="test-key", transport=LocalTransportConfig())
    websocket = EasyCatConfig(openai_api_key="test-key", transport=WebSocketTransportConfig())

    assert local.echo_cancellation == EchoCancellationConfig(enabled=True)
    assert websocket.echo_cancellation == EchoCancellationConfig(enabled=True)


def test_easycat_config_echo_cancellation_defaults_off_for_other_transports():
    twilio = EasyCatConfig(openai_api_key="test-key", transport=TwilioTransportConfig())
    webrtc = EasyCatConfig(openai_api_key="test-key", transport=WebRTCTransportConfig())

    assert twilio.echo_cancellation == EchoCancellationConfig(enabled=False)
    assert webrtc.echo_cancellation == EchoCancellationConfig(enabled=False)


def test_easycat_config_echo_cancellation_respects_explicit_override():
    config = EasyCatConfig(
        openai_api_key="test-key",
        transport=LocalTransportConfig(),
        echo_cancellation=EchoCancellationConfig(enabled=False),
    )

    assert config.echo_cancellation == EchoCancellationConfig(enabled=False)


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
    from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim
    from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

    raw = agents_mod.Agent(name="test", instructions="hi")
    config = EasyCatConfig(openai_api_key="test-key", agent=raw)
    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise

    assert isinstance(session.agent, AgentRunner)
    assert isinstance(session.agent._agent, BridgeAdapterShim)
    assert isinstance(session.agent._agent.bridge, OpenAIAgentsBridge)


def test_create_session_auto_adapts_pydantic_agents():
    pydantic_ai_mod = pytest.importorskip("pydantic_ai")
    from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim
    from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

    raw = pydantic_ai_mod.Agent("openai:gpt-4o-mini")
    config = EasyCatConfig(openai_api_key="test-key", agent=raw)
    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise

    assert isinstance(session.agent, AgentRunner)
    assert isinstance(session.agent._agent, BridgeAdapterShim)
    assert isinstance(session.agent._agent.bridge, PydanticAIBridge)


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


def test_create_session_adds_twilio_action_executor_when_configured():
    config = EasyCatConfig(
        openai_api_key="test-key",
        agent=_DummyAgent(),
        telephony=TelephonyConfig(
            twilio_actions=TwilioSessionActionConfig(
                account_sid="AC123",
                auth_token="secret",
            )
        ),
    )

    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise

    assert any(
        isinstance(executor, TwilioSessionActionExecutor) for executor in session._action_executors
    )


def test_create_session_disables_vad_for_deepgram_flux(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "easycat.config.create_vad",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("create_vad should not be called")
        ),
    )

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    monkeypatch.setattr(
        "easycat.config.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
    )

    config = EasyCatConfig(
        stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
        tts=OpenAITTSConfig(api_key="test-key"),
        agent=_DummyAgent(),
    )

    session = create_session(config)

    assert session._enable_vad is False
    assert session._auto_turn_from_stt_final is True


def test_create_session_keeps_flux_auto_turn_disabled_for_push_to_talk(
    monkeypatch: pytest.MonkeyPatch,
):
    create_vad_called = False

    class _VAD:
        async def process(self, chunk):
            if False:
                yield chunk

        def configure(self, **kwargs):
            pass

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    def _create_vad(*_args, **_kwargs):
        nonlocal create_vad_called
        create_vad_called = True
        return _VAD()

    monkeypatch.setattr("easycat.config.create_vad", _create_vad)
    monkeypatch.setattr(
        "easycat.config.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
    )

    config = EasyCatConfig(
        stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
        tts=OpenAITTSConfig(api_key="test-key"),
        turn_taking=TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK),
        agent=_DummyAgent(),
    )

    session = create_session(config)

    assert create_vad_called is True
    assert session._enable_vad is True
    assert session._auto_turn_from_stt_final is False


def test_create_session_keeps_vad_enabled_for_flux_when_smart_turn_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    create_vad_called = False

    class _VAD:
        async def process(self, chunk):
            if False:
                yield chunk

        def configure(self, **kwargs):
            pass

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    def _create_vad(*_args, **_kwargs):
        nonlocal create_vad_called
        create_vad_called = True
        return _VAD()

    monkeypatch.setattr("easycat.config.create_vad", _create_vad)
    monkeypatch.setattr(
        "easycat.config.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
    )

    config = EasyCatConfig(
        stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
        tts=OpenAITTSConfig(api_key="test-key"),
        smart_turn=SmartTurnConfig(enabled=True),
        agent=_DummyAgent(),
    )

    session = create_session(config)

    assert create_vad_called is True
    assert session._enable_vad is True
    assert session._auto_turn_from_stt_final is False


def test_create_session_keeps_vad_enabled_for_flux_when_voicemail_detector_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    create_vad_called = False

    class _VAD:
        async def process(self, chunk):
            if False:
                yield chunk

        def configure(self, **kwargs):
            pass

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    def _create_vad(*_args, **_kwargs):
        nonlocal create_vad_called
        create_vad_called = True
        return _VAD()

    monkeypatch.setattr("easycat.config.create_vad", _create_vad)
    monkeypatch.setattr(
        "easycat.config.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
    )

    config = EasyCatConfig(
        stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
        tts=OpenAITTSConfig(api_key="test-key"),
        telephony=TelephonyConfig(enable_voicemail_detector=True),
        agent=_DummyAgent(),
    )

    session = create_session(config)

    assert create_vad_called is True
    assert session._enable_vad is True
    assert session._auto_turn_from_stt_final is False


# ── debug mode tests ──────────────────────────────────────────────────


def test_debug_mode_sets_easycat_logger_to_debug():
    EasyCatConfig(openai_api_key="test-key", debug=True)
    assert logging.getLogger("easycat").level == logging.DEBUG
