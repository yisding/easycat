from __future__ import annotations

import logging

import pytest

from easycat import PCM16_MONO_16K, PCM16_MONO_24K, EasyConfig, create_session
from easycat.config import TelephonyConfig
from easycat.echo_cancellation import EchoCancellationConfig
from easycat.events import DTMFAggregated
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.session._types import CallIdentity
from easycat.smart_turn import SmartTurnConfig
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.openai_provider import OpenAISTTConfig  # noqa: F401  (re-exported symbol)
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.telephony.dtmf import emit_twilio_dtmf
from easycat.telephony.session_actions import (
    TwilioSessionActionConfig,
    TwilioSessionActionExecutor,
)
from easycat.transports.local import LocalTransportConfig
from easycat.transports.twilio_media import TwilioConnectionTransport, TwilioTransportConfig
from easycat.transports.webrtc import WebRTCTransportConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.tts.cartesia_tts import CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig, TurnMode


class _DummyAgent:
    async def run(self, text: str) -> str:
        return text


class _DummyWebSocket:
    async def send(self, _message):
        return None

    async def close(self):
        return None


def test_easycat_config_requires_stt_tts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        EasyConfig()


def test_easycat_config_openai_defaults():
    config = EasyConfig(openai_api_key="test-key")
    # Default STT is the streaming Realtime provider (sub-second
    # stop-to-final); the batch OpenAISTTConfig is still usable via
    # explicit override but is no longer the auto-wired default.
    assert isinstance(config.stt, OpenAIRealtimeSTTConfig)
    assert isinstance(config.tts, OpenAITTSConfig)


def test_easycat_config_auto_aligns_default_openai_tts_to_twilio_transport_instance():
    transport = TwilioConnectionTransport(_DummyWebSocket())

    config = EasyConfig(openai_api_key="test-key", transport=transport)

    assert isinstance(config.tts, OpenAITTSConfig)
    assert config.tts.output_format == transport.audio_format


@pytest.mark.asyncio
async def test_create_session_binds_twilio_connection_identity_sink():
    transport = TwilioConnectionTransport(_DummyWebSocket())
    session = create_session(
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
            transport=transport,
        )
    )

    await transport._handle_start(
        {
            "streamSid": "MZ1",
            "start": {
                "streamSid": "MZ1",
                "callSid": "CA1",
                "customParameters": {
                    "From": "+15551234567",
                    "To": "+15557654321",
                    "CallerName": "Alice Example",
                },
            },
        }
    )

    assert session.call_identity is transport.call_identity
    assert session.call_identity is not None
    assert session.call_identity.caller_number == "+15551234567"
    assert session.call_identity.called_number == "+15557654321"
    assert session.call_identity.display_name == "Alice Example"


@pytest.mark.asyncio
async def test_create_session_caller_id_off_keeps_twilio_identity_private():
    transport = TwilioConnectionTransport(_DummyWebSocket())
    session = create_session(
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
            transport=transport,
            caller_id_exposure="off",
        )
    )

    await transport._handle_start(
        {
            "streamSid": "MZ1",
            "start": {
                "streamSid": "MZ1",
                "callSid": "CA1",
                "customParameters": {
                    "From": "+15551234567",
                    "To": "+15557654321",
                },
            },
        }
    )

    assert transport.call_identity is not None
    assert session.call_identity is None
    assert session._call_identity is not None
    assert session._call_identity.caller_number == "+15551234567"


@pytest.mark.asyncio
async def test_create_session_twilio_identity_sink_merges_with_outbound_identity():
    transport = TwilioConnectionTransport(_DummyWebSocket())
    session = create_session(
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
            transport=transport,
        )
    )
    session.call_identity = CallIdentity(
        caller_number="+15551112222",
        called_number="+15559876543",
        direction="outbound",
    )

    await transport._handle_start(
        {
            "streamSid": "MZ1",
            "start": {
                "streamSid": "MZ1",
                "callSid": "CA1",
                "customParameters": {
                    "Direction": "outbound-api",
                    "From": "+15559876543",
                    "To": "+15551112222",
                    "crm_account_id": "ACC-42",
                },
            },
        }
    )

    assert transport.call_identity is not None
    assert transport.call_identity.direction == "outbound"
    assert transport.call_identity.caller_number == "+15551112222"
    assert transport.call_identity.called_number == "+15559876543"
    assert session.call_identity is not transport.call_identity
    assert session.call_identity is not None
    assert session.call_identity.direction == "outbound"
    assert session.call_identity.caller_number == "+15551112222"
    assert session.call_identity.called_number == "+15559876543"
    assert session.call_identity.call_sid == "CA1"
    assert session.call_identity.custom_fields == {"crm_account_id": "ACC-42"}


@pytest.mark.parametrize(
    ("tts_config", "expected_rate", "expected_output"),
    [
        (OpenAITTSConfig(api_key="test-key"), 16000, PCM16_MONO_16K),
        (DeepgramTTSConfig(api_key="test-key"), 16000, PCM16_MONO_16K),
        (CartesiaTTSConfig(api_key="test-key"), 16000, PCM16_MONO_16K),
        (ElevenLabsTTSConfig(api_key="test-key"), 16000, "pcm_16000"),
    ],
)
def test_easycat_config_auto_aligns_default_tts_configs_to_transport(
    tts_config,
    expected_rate,
    expected_output,
):
    config = EasyConfig(
        stt=OpenAIRealtimeSTTConfig(api_key="stt-key"),
        tts=tts_config,
        transport=LocalTransportConfig(audio_format=PCM16_MONO_16K),
    )

    if isinstance(config.tts, OpenAITTSConfig):
        assert config.tts.output_format == expected_output
    elif isinstance(config.tts, DeepgramTTSConfig):
        assert config.tts.sample_rate == expected_rate
        assert config.tts.output_format == expected_output
    elif isinstance(config.tts, CartesiaTTSConfig):
        assert config.tts.sample_rate == expected_rate
        assert config.tts.output_format == expected_output
    else:
        assert isinstance(config.tts, ElevenLabsTTSConfig)
        assert config.tts.output_format == expected_output
        assert config.tts.audio_format == PCM16_MONO_16K


def test_easycat_config_auto_aligns_string_tts_shortcuts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-elevenlabs-key")

    config = EasyConfig(
        stt=OpenAIRealtimeSTTConfig(api_key="stt-key"),
        tts="elevenlabs",
        transport=LocalTransportConfig(audio_format=PCM16_MONO_16K),
    )

    assert isinstance(config.tts, ElevenLabsTTSConfig)
    assert config.tts.output_format == "pcm_16000"
    assert config.tts.audio_format == PCM16_MONO_16K


def test_easycat_config_preserves_explicit_tts_playback_when_auto_align_disabled():
    config = EasyConfig(
        stt=OpenAIRealtimeSTTConfig(api_key="stt-key"),
        tts=ElevenLabsTTSConfig(api_key="tts-key"),
        transport=LocalTransportConfig(audio_format=PCM16_MONO_16K),
        auto_align_tts_output_to_transport=False,
    )

    assert isinstance(config.tts, ElevenLabsTTSConfig)
    assert config.tts.output_format == "pcm_24000"
    assert config.tts.audio_format == PCM16_MONO_24K


def test_easycat_config_echo_cancellation_defaults_for_local_and_websocket():
    local = EasyConfig(openai_api_key="test-key", transport=LocalTransportConfig())
    websocket = EasyConfig(openai_api_key="test-key", transport=WebSocketTransportConfig())

    assert local.echo_cancellation == EchoCancellationConfig(enabled=True)
    assert websocket.echo_cancellation == EchoCancellationConfig(enabled=True)


def test_easycat_config_echo_cancellation_defaults_off_for_other_transports():
    twilio = EasyConfig(openai_api_key="test-key", transport=TwilioTransportConfig())
    webrtc = EasyConfig(openai_api_key="test-key", transport=WebRTCTransportConfig())

    assert twilio.echo_cancellation == EchoCancellationConfig(enabled=False)
    assert webrtc.echo_cancellation == EchoCancellationConfig(enabled=False)


def test_easycat_config_echo_cancellation_respects_explicit_override():
    config = EasyConfig(
        openai_api_key="test-key",
        transport=LocalTransportConfig(),
        echo_cancellation=EchoCancellationConfig(enabled=False),
    )

    assert config.echo_cancellation == EchoCancellationConfig(enabled=False)


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
    from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

    raw = pydantic_ai_mod.Agent("openai:gpt-4o-mini")
    config = EasyConfig(openai_api_key="test-key", agent=raw)
    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise

    assert isinstance(session.agent, AgentRunner)
    assert isinstance(session.agent._agent, PydanticAIBridge)


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


@pytest.mark.asyncio
async def test_telephony_helpers_are_managed_by_session_lifecycle():
    config = EasyConfig(
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
    config = EasyConfig(
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

    config = EasyConfig(
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

    config = EasyConfig(
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

    config = EasyConfig(
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

    config = EasyConfig(
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
    EasyConfig(openai_api_key="test-key", debug="full")
    assert logging.getLogger("easycat").level == logging.DEBUG


def test_debug_bool_true_rejected():
    with pytest.raises(ValueError, match="Invalid debug=True"):
        EasyConfig(openai_api_key="test-key", debug=True)


def test_debug_bool_false_rejected():
    with pytest.raises(ValueError, match="Invalid debug=False"):
        EasyConfig(openai_api_key="test-key", debug=False)
