from __future__ import annotations

import logging

import pytest

from easycat import PCM16_MONO_16K, PCM16_MONO_24K, EasyConfig, create_session
from easycat.audio_format import AudioChunk
from easycat.config import TelephonyConfig
from easycat.echo_cancellation import EchoCancellationConfig
from easycat.events import DTMFAggregated
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.session._types import CallIdentity
from easycat.smart_turn import SmartTurnConfig
from easycat.stt.deepgram_provider import DeepgramSTTConfig
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


class _CapabilityTransportConfig:
    default_echo_cancellation_enabled = True


class _IdentitySinkTransport:
    def __init__(self) -> None:
        self.identity_sink = None

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self):
        return
        yield

    async def send_audio(self, chunk: AudioChunk) -> bool:
        return True

    async def clear_audio(self) -> None:
        pass

    def bind_identity_sink(self, sink) -> None:
        self.identity_sink = sink

    def version_info(self) -> dict[str, str]:
        return {"provider": "identity-sink"}


def test_easycat_config_requires_stt_tts(monkeypatch: pytest.MonkeyPatch):
    # No key resolved and no stt/tts configured now routes through the
    # error catalog as EASYCAT_E203 (an EasyCatError, not a ValueError).
    from easycat.errors import EasyCatError

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(EasyCatError) as excinfo:
        EasyConfig()
    assert excinfo.value.code == "EASYCAT_E203"


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


def test_easycat_config_uses_transport_echo_preference_capability():
    config = EasyConfig(openai_api_key="test-key", transport=_CapabilityTransportConfig())

    assert config.echo_cancellation is not None
    assert config.echo_cancellation.enabled is True


def test_create_session_binds_custom_identity_sink_capability():
    transport = _IdentitySinkTransport()
    session = create_session(
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
            transport=transport,
        )
    )
    identity = CallIdentity(caller_number="+15551234567")

    assert transport.identity_sink is not None
    transport.identity_sink(identity)

    assert session.call_identity is identity


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
    assert session._caller_id.private_identity is not None
    assert session._caller_id.private_identity.caller_number == "+15551234567"


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
    for helper in session.telephony.helpers:
        helper.start()

    await emit_twilio_dtmf({"event": "dtmf", "dtmf": {"digit": "1"}}, bus)
    await emit_twilio_dtmf({"event": "dtmf", "dtmf": {"digit": "#"}}, bus)
    assert aggregated

    for helper in session.telephony.helpers:
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
        "easycat.config._factory.create_vad",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("create_vad should not be called")
        ),
    )

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
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

    monkeypatch.setattr("easycat.config._factory.create_vad", _create_vad)
    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
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

    monkeypatch.setattr("easycat.config._factory.create_vad", _create_vad)
    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
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


def _stub_audio_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub VAD / noise reduction so create_session never needs a backend."""

    class _VAD:
        async def process(self, chunk):
            if False:
                yield chunk

        def configure(self, **kwargs):
            pass

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    monkeypatch.setattr("easycat.config._factory.create_vad", lambda *_a, **_k: _VAD())
    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_a, **_k: _NoiseReducer()
    )


def test_create_session_derives_endpoint_threshold_from_smart_turn(
    monkeypatch: pytest.MonkeyPatch,
):
    """When endpoint_threshold is left None, it is derived from smart_turn.threshold."""
    _stub_audio_backends(monkeypatch)
    config = EasyConfig(
        openai_api_key="test-key",
        smart_turn=SmartTurnConfig(enabled=True, threshold=0.7),
        agent=_DummyAgent(),
    )

    session = create_session(config)

    assert session._turn_manager._config.endpoint_threshold == 0.7
    # The source config must not be mutated.
    assert config.turn_taking.endpoint_threshold is None


def test_create_session_endpoint_threshold_overrides_smart_turn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """An explicit manager endpoint_threshold wins and warns when it diverges."""
    _stub_audio_backends(monkeypatch)
    config = EasyConfig(
        openai_api_key="test-key",
        turn_taking=TurnManagerConfig(endpoint_threshold=0.8),
        smart_turn=SmartTurnConfig(enabled=True, threshold=0.5),
        agent=_DummyAgent(),
    )

    with caplog.at_level(logging.WARNING, logger="easycat.config"):
        session = create_session(config)

    assert session._turn_manager._config.endpoint_threshold == 0.8
    assert any("endpoint_threshold" in rec.message for rec in caplog.records)


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

    monkeypatch.setattr("easycat.config._factory.create_vad", _create_vad)
    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
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


@pytest.fixture
def _restore_easycat_logger():
    """Snapshot/restore easycat logger state so debug-mode tests stay isolated."""
    logger = logging.getLogger("easycat")
    handlers = logger.handlers[:]
    level = logger.level
    propagate = logger.propagate
    try:
        yield logger
    finally:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def test_debug_mode_defaults_easycat_logger_to_info(_restore_easycat_logger):
    EasyConfig(openai_api_key="test-key", debug="full")
    # H4: EASYCAT_LOG_LEVEL has a single meaning — INFO by default, DEBUG only
    # when the env var explicitly requests it (mirrors run()).
    assert _restore_easycat_logger.level == logging.INFO


def test_debug_mode_honors_env_debug_level(
    monkeypatch: pytest.MonkeyPatch, _restore_easycat_logger
):
    monkeypatch.setenv("EASYCAT_LOG_LEVEL", "debug")
    EasyConfig(openai_api_key="test-key", debug="full")
    assert _restore_easycat_logger.level == logging.DEBUG


def test_debug_bool_true_rejected():
    with pytest.raises(ValueError, match="Invalid debug=True"):
        EasyConfig(openai_api_key="test-key", debug=True)


def test_debug_bool_false_rejected():
    with pytest.raises(ValueError, match="Invalid debug=False"):
        EasyConfig(openai_api_key="test-key", debug=False)


# ── provider display name in missing-API-key errors ───────────────────


def test_missing_api_key_error_uses_catalog_name_for_deepgram():
    with pytest.raises(ValueError, match=r"deepgram STT requires an API key"):
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
        )


def test_missing_api_key_error_uses_catalog_name_for_openai_tts():
    with pytest.raises(ValueError, match=r"openai TTS requires an API key"):
        EasyConfig(
            stt=OpenAIRealtimeSTTConfig(api_key="stt-key"),
            tts=OpenAITTSConfig(api_key=""),
        )


# ── missing-key routes through EASYCAT_E203 ────────────────────────────


def test_missing_openai_key_with_no_stt_tts_raises_e203(monkeypatch: pytest.MonkeyPatch):
    from easycat.errors import EasyCatError

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(EasyCatError) as excinfo:
        EasyConfig()
    assert excinfo.value.code == "EASYCAT_E203"
    assert excinfo.value.context == {"var": "OPENAI_API_KEY"}


def test_easyconfig_is_keyword_only(monkeypatch: pytest.MonkeyPatch):
    """Positional construction must fail loudly, never silently mis-bind.

    Regression guard for the ``_AgentSessionConfig`` base extraction: a base
    dataclass injects its fields before the subclass's in the generated
    ``__init__``, so without ``kw_only=True`` a positional
    ``EasyConfig("sk-...")`` would bind the key to ``agent`` instead of
    ``openai_api_key``. ``kw_only`` turns that into a ``TypeError``.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(TypeError):
        EasyConfig("sk-test")  # type: ignore[misc]  # positional is rejected
    # The keyword form still works and resolves the key correctly.
    cfg = EasyConfig(openai_api_key="sk-test")
    assert cfg.openai_api_key == "sk-test"
    assert cfg.agent is None


# ── agent shape fail-fast at construction ──────────────────────────────


def _stub_audio_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub VAD/noise-reduction so create_session reaches the agent check.

    The agent shape check sits after VAD creation in ``create_session``;
    stub the audio backends so the check is reached even when no real VAD
    backend is installed in the test environment.
    """

    class _VAD:
        async def process(self, chunk):
            if False:
                yield chunk

        def configure(self, **kwargs):
            pass

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    monkeypatch.setattr("easycat.config._factory.create_vad", lambda *_a, **_k: _VAD())
    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_a, **_k: _NoiseReducer()
    )


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


# ── text session config object form ───────────────────────────────────


def test_create_text_session_accepts_config_object():
    from easycat.config import TextSessionConfig, create_text_session

    config = TextSessionConfig(agent=_DummyAgent(), debug="off")
    session = create_text_session(config)
    assert session is not None


def test_create_text_session_kwargs_still_supported():
    from easycat.config import create_text_session

    session = create_text_session(agent=_DummyAgent(), debug="off")
    assert session is not None


def test_text_session_config_validates_debug():
    from easycat.config import TextSessionConfig

    with pytest.raises(ValueError, match="Invalid debug"):
        TextSessionConfig(agent=_DummyAgent(), debug="loud")  # type: ignore[arg-type]


def test_create_text_session_rejects_config_plus_loose_kwargs():
    from easycat.config import TextSessionConfig, create_text_session

    config = TextSessionConfig(agent=_DummyAgent())
    with pytest.raises(ValueError, match="not both"):
        create_text_session(config, agent=_DummyAgent())


def test_create_text_session_config_with_default_kwargs_ok():
    from easycat.config import TextSessionConfig, create_text_session

    # Passing config alongside only default-valued kwargs is allowed.
    config = TextSessionConfig(agent=_DummyAgent(), debug="off")
    session = create_text_session(config, debug="off")
    assert session is not None
