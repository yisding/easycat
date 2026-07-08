from __future__ import annotations

import os

import pytest

from easycat import (
    PCM16_MONO_8K,
    PCM16_MONO_16K,
    PCM16_MONO_24K,
    AudioProcessingConfig,
    EasyConfig,
    ObservabilityConfig,
    SessionPolicyConfig,
)
from easycat.echo_cancellation import EchoCancellationConfig
from easycat.smart_turn import SmartTurnConfig
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.transports.local import LocalTransportConfig
from easycat.transports.twilio_media import TwilioConnectionTransport, TwilioTransportConfig
from easycat.transports.webrtc import WebRTCTransportConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.tts.cartesia_tts import CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTSConfig
from tests.config._helpers import (
    _CapabilityTransportConfig,
    _DummyWebSocket,
)


def test_easycat_config_openai_defaults():
    config = EasyConfig(openai_api_key="test-key")
    # Default STT is the streaming Realtime provider (sub-second
    # stop-to-final); the batch OpenAISTTConfig is still usable via
    # explicit override but is no longer the auto-wired default.
    assert isinstance(config.stt, OpenAIRealtimeSTTConfig)
    assert isinstance(config.tts, OpenAITTSConfig)


def test_easycat_config_defaults_debug_to_full():
    # Durable journaling is on by default: the single source of the default
    # lives on ObservabilityConfig, and EasyConfig inherits it through the
    # observability alias proxy.
    config = EasyConfig(openai_api_key="test-key")
    assert config.debug == "full"
    assert config.observability.debug == "full"
    assert ObservabilityConfig().debug == "full"


def test_debugger_autolaunch_defaults_off_even_with_debug_full():
    # ``debug="full"`` keeps a durable journal but must NOT arm debugger
    # auto-launch on its own — that is strictly opt-in.
    assert ObservabilityConfig().debugger_autolaunch is False
    config = EasyConfig(openai_api_key="test-key")
    assert config.observability.debug == "full"
    assert config.observability.debugger_autolaunch is False
    # Reachable through the observability alias proxy.
    assert config.debugger_autolaunch is False


def test_debugger_autolaunch_opt_in_via_observability_knob():
    config = EasyConfig(
        openai_api_key="test-key",
        observability=ObservabilityConfig(debugger_autolaunch=True),
    )
    assert config.observability.debugger_autolaunch is True
    assert config.debugger_autolaunch is True


def test_capture_aec_reference_defaults_off_even_with_debug_full():
    # ``debug="full"`` keeps a durable journal but must NOT journal per-frame
    # AEC reference rows on its own — that is strictly opt-in.
    assert ObservabilityConfig().capture_aec_reference is False
    config = EasyConfig(openai_api_key="test-key")
    assert config.observability.debug == "full"
    assert config.observability.capture_aec_reference is False
    # Reachable through the observability alias proxy.
    assert config.capture_aec_reference is False


def test_capture_aec_reference_opt_in_via_observability_knob():
    config = EasyConfig(
        openai_api_key="test-key",
        observability=ObservabilityConfig(capture_aec_reference=True),
    )
    assert config.observability.capture_aec_reference is True
    assert config.capture_aec_reference is True


def test_easycat_config_programmatic_openai_key_parses_string_shortcuts_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    config = EasyConfig(openai_api_key="programmatic-key", stt="openai-realtime", tts="openai")

    assert isinstance(config.stt, OpenAIRealtimeSTTConfig)
    assert isinstance(config.tts, OpenAITTSConfig)
    assert config.stt.api_key == "programmatic-key"
    assert config.tts.api_key == "programmatic-key"
    assert os.getenv("OPENAI_API_KEY") is None


def test_easycat_config_programmatic_openai_key_does_not_overwrite_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")

    config = EasyConfig(openai_api_key="programmatic-key", stt="openai-realtime", tts="openai")

    assert isinstance(config.stt, OpenAIRealtimeSTTConfig)
    assert isinstance(config.tts, OpenAITTSConfig)
    assert config.stt.api_key == "programmatic-key"
    assert config.tts.api_key == "programmatic-key"
    assert os.getenv("OPENAI_API_KEY") == "env-key"


def test_easycat_config_auto_aligns_default_openai_tts_to_twilio_transport_instance():
    transport = TwilioConnectionTransport(_DummyWebSocket())

    config = EasyConfig(openai_api_key="test-key", transport=transport)

    assert isinstance(config.tts, OpenAITTSConfig)
    assert transport.audio_format == PCM16_MONO_16K
    assert transport.preferred_tts_output_format == PCM16_MONO_8K
    assert config.tts.output_format == PCM16_MONO_8K


def test_easycat_config_auto_aligns_default_openai_tts_to_twilio_transport_config():
    transport = TwilioTransportConfig()

    config = EasyConfig(openai_api_key="test-key", transport=transport)

    assert isinstance(config.tts, OpenAITTSConfig)
    assert transport.audio_format == PCM16_MONO_16K
    assert transport.preferred_tts_output_format == PCM16_MONO_8K
    assert config.tts.output_format == PCM16_MONO_8K


def test_easycat_config_uses_transport_echo_preference_capability():
    config = EasyConfig(openai_api_key="test-key", transport=_CapabilityTransportConfig())

    assert config.echo_cancellation is not None
    assert config.echo_cancellation.enabled is True


def test_easyconfig_session_policy_keeps_legacy_top_level_aliases():
    config = EasyConfig(
        stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
        tts=OpenAITTSConfig(api_key="test-key"),
        greeting="Hello",
        caller_id_exposure="system_message",
    )

    assert config.session_policy == SessionPolicyConfig(
        greeting="Hello",
        caller_id_exposure="system_message",
    )
    assert config.greeting == "Hello"
    assert config.caller_id_exposure == "system_message"

    config.caller_id_exposure = "tools_only"

    assert config.session_policy.caller_id_exposure == "tools_only"


def test_easyconfig_audio_processing_keeps_legacy_top_level_aliases():
    echo_cancellation = EchoCancellationConfig(enabled=False)

    config = EasyConfig(
        openai_api_key="test-key",
        echo_cancellation=echo_cancellation,
        enable_noise_reduction=True,
        smart_turn=True,
    )

    assert config.audio_processing.echo_cancellation is echo_cancellation
    assert config.echo_cancellation is echo_cancellation
    assert config.audio_processing.enable_noise_reduction is True
    assert config.enable_noise_reduction is True
    assert isinstance(config.audio_processing.smart_turn, SmartTurnConfig)
    assert config.smart_turn.enabled is True

    config.enable_noise_reduction = False
    config.echo_cancellation = EchoCancellationConfig(enabled=True)

    assert config.audio_processing.enable_noise_reduction is False
    assert config.audio_processing.echo_cancellation is config.echo_cancellation
    assert config.echo_cancellation.enabled is True


def test_browser_preset_preserves_grouped_echo_cancellation_override(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    config = EasyConfig.browser(
        audio_processing=AudioProcessingConfig(enable_echo_cancellation=False)
    )

    assert config.enable_echo_cancellation is False
    assert config.echo_cancellation is not None
    assert config.echo_cancellation.enabled is False


def test_easyconfig_observability_keeps_legacy_top_level_aliases():
    config = EasyConfig(
        openai_api_key="test-key",
        observability=ObservabilityConfig(debug="light", journal_backend="libsql"),
        debug="off",
        journal_retention="delete",
    )

    assert config.observability == ObservabilityConfig(
        debug="off",
        journal_backend="libsql",
        journal_retention="delete",
    )
    assert config.debug == "off"
    assert config.journal_backend == "libsql"
    assert config.journal_retention == "delete"

    config.debug = "light"
    config.journal_retention = "archive"

    assert config.observability.debug == "light"
    assert config.observability.journal_retention == "archive"


def test_easyconfig_observability_carries_advanced_runtime_knobs():
    config = EasyConfig(
        openai_api_key="test-key",
        observability=ObservabilityConfig(
            warmup=False,
        ),
    )

    assert config.warmup is False


def test_easyconfig_observability_advanced_knobs_keep_top_level_aliases():
    config = EasyConfig(
        openai_api_key="test-key",
        observability=ObservabilityConfig(warmup=True),
        warmup=False,
    )

    assert config.observability.warmup is False


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


@pytest.mark.parametrize(
    ("tts_config", "expected_rate", "expected_output"),
    [
        (OpenAITTSConfig(api_key="test-key"), None, PCM16_MONO_8K),
        (DeepgramTTSConfig(api_key="test-key"), 8000, PCM16_MONO_8K),
        (CartesiaTTSConfig(api_key="test-key"), 8000, PCM16_MONO_8K),
        (ElevenLabsTTSConfig(api_key="test-key"), None, "pcm_16000"),
    ],
)
def test_easycat_config_auto_aligns_default_tts_configs_to_twilio_tts_preference(
    tts_config,
    expected_rate,
    expected_output,
):
    config = EasyConfig(
        stt=OpenAIRealtimeSTTConfig(api_key="stt-key"),
        tts=tts_config,
        transport=TwilioTransportConfig(),
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
        assert config.tts.audio_format == PCM16_MONO_8K


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


def test_easycat_config_enable_echo_cancellation_folds_into_supplied_config():
    config = EasyConfig.browser(
        openai_api_key="test-key",
        enable_echo_cancellation=True,
        echo_cancellation=EchoCancellationConfig(fallback_policy="error"),
    )

    assert config.echo_cancellation.enabled is True
    # ``fallback_policy`` must survive the fold.
    assert config.echo_cancellation.fallback_policy == "error"


def test_easycat_config_enable_echo_cancellation_flag_wins_over_config_enabled():
    config = EasyConfig.browser(
        openai_api_key="test-key",
        enable_echo_cancellation=False,
        echo_cancellation=EchoCancellationConfig(enabled=True),
    )

    assert config.echo_cancellation.enabled is False


def test_easycat_config_smart_turn_defaults_on_for_local_transport():
    config = EasyConfig(openai_api_key="test-key", transport=LocalTransportConfig())

    assert config.smart_turn.enabled is True


def test_easycat_config_mic_preset_defaults_smart_turn_on(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    config = EasyConfig.mic()

    assert config.smart_turn.enabled is True


def test_easycat_config_smart_turn_defaults_off_for_non_local_transports():
    websocket = EasyConfig(openai_api_key="test-key", transport=WebSocketTransportConfig())
    twilio = EasyConfig(openai_api_key="test-key", transport=TwilioTransportConfig())
    webrtc = EasyConfig(openai_api_key="test-key", transport=WebRTCTransportConfig())

    assert websocket.smart_turn.enabled is False
    assert twilio.smart_turn.enabled is False
    assert webrtc.smart_turn.enabled is False


def test_easycat_config_smart_turn_explicit_false_overrides_local_default():
    config = EasyConfig(
        openai_api_key="test-key",
        transport=LocalTransportConfig(),
        smart_turn=False,
    )

    assert config.smart_turn.enabled is False
