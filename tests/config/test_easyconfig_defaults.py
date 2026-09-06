from __future__ import annotations

import logging
import os

import pytest

from easycat import (
    PCM16_MONO_8K,
    PCM16_MONO_16K,
    PCM16_MONO_24K,
    EasyConfig,
)
from easycat.config import EasyConfigError, TelephonyConfig
from easycat.echo_cancellation import EchoCancellationConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.transports.local import LocalTransportConfig
from easycat.transports.twilio_media import TwilioConnectionTransport, TwilioTransportConfig
from easycat.transports.webrtc import WebRTCTransportConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.transports.webtransport import WebTransportTransportConfig
from easycat.tts.cartesia_tts import CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig, TurnMode
from easycat.vad import VADConfig
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


def test_openai_defaults_match_the_planner_default_provider_names():
    """DX1-1, decision E: pins the default-provider duplication before DX1-2
    introduces the shared constants — ``easy.py``'s ``__post_init__`` default
    classes must name the same providers ``build_provider_plan`` assumes for
    an unset stt/tts (``provider_plan.py:564,567``).
    """
    from easycat.planning import build_provider_plan
    from easycat.project.schema import VoiceProfile
    from easycat.stt.factory import _CATALOG as stt_catalog
    from easycat.tts.factory import _CATALOG as tts_catalog

    config = EasyConfig(openai_api_key="test-key")
    assert type(config.stt) is stt_catalog.providers["openai-realtime"][1]
    assert type(config.tts) is tts_catalog.providers["openai"][1]

    plan = build_provider_plan(
        VoiceProfile(name="default", transport="local"), environ={"OPENAI_API_KEY": "test-key"}
    )
    assert plan.selected["stt"].config_type == type(config.stt).__name__
    assert plan.selected["tts"].config_type == type(config.tts).__name__


def test_easycat_config_defaults_debug_to_light():
    # The default is the in-memory ``"light"`` journal so per-frame capture
    # stays off the disk and off the live audio loop. ``"full"`` is the
    # opt-in durable/deep-debugging mode.
    config = EasyConfig(openai_api_key="test-key")
    assert config.debug == "light"


def test_easycat_config_defaults_journal_capacity():
    config = EasyConfig(openai_api_key="test-key")
    assert config.journal_capacity == 10_000


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
def test_easycat_config_validates_journal_capacity(capacity):
    with pytest.raises(ValueError, match="journal_capacity must be a positive integer"):
        EasyConfig(
            openai_api_key="test-key",
            journal_capacity=capacity,  # type: ignore[arg-type]
        )


def test_easycat_config_defaults_journal_redaction_to_secrets():
    config = EasyConfig(openai_api_key="test-key")
    assert config.journal_redaction == "secrets"


def test_easycat_config_validates_journal_redaction():
    with pytest.raises(ValueError, match="Invalid journal_redaction"):
        EasyConfig(
            openai_api_key="test-key",
            journal_redaction="everything",  # type: ignore[arg-type]
        )


def test_easycat_config_rejects_invalid_caller_id_exposure():
    with pytest.raises(EasyConfigError, match="caller_id_exposure"):
        EasyConfig(
            openai_api_key="test-key",
            caller_id_exposure="offf",  # type: ignore[arg-type]
        )


def test_easycat_validation_errors_share_one_config_exception():
    with pytest.raises(EasyConfigError, match="journal_capacity"):
        EasyConfig(openai_api_key="test-key", journal_capacity=0)


def test_easycat_config_defaults_event_dispatch_diagnostics():
    config = EasyConfig(openai_api_key="test-key")

    assert config.slow_handler_threshold_s == 0.005
    assert config.handler_error_policy == "continue"


def test_easycat_config_rejects_invalid_event_dispatch_settings():
    with pytest.raises(ValueError, match="slow_handler_threshold_s must be non-negative"):
        EasyConfig(openai_api_key="test-key", slow_handler_threshold_s=-0.001)
    with pytest.raises(ValueError, match="Invalid handler_error_policy"):
        EasyConfig(openai_api_key="test-key", handler_error_policy="strict")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "threshold",
    [float("nan"), float("inf"), float("-inf"), True],
)
def test_easycat_config_rejects_non_finite_event_dispatch_thresholds(threshold):
    with pytest.raises(ValueError, match="slow_handler_threshold_s"):
        EasyConfig(openai_api_key="test-key", slow_handler_threshold_s=threshold)


def test_debugger_autolaunch_defaults_off_even_with_debug_full():
    # ``debug="full"`` keeps a durable journal but must NOT arm debugger
    # auto-launch on its own — that is strictly opt-in.
    config = EasyConfig(openai_api_key="test-key", debug="full")
    assert config.debug == "full"
    assert config.debugger_autolaunch is False


def test_debugger_autolaunch_opt_in():
    config = EasyConfig(
        openai_api_key="test-key",
        debugger_autolaunch=True,
    )
    assert config.debugger_autolaunch is True


def test_capture_aec_reference_defaults_off_even_with_debug_full():
    # ``debug="full"`` keeps a durable journal but must NOT journal per-frame
    # AEC reference rows on its own — that is strictly opt-in.
    config = EasyConfig(openai_api_key="test-key", debug="full")
    assert config.debug == "full"
    assert config.capture_aec_reference is False


def test_capture_aec_reference_opt_in():
    config = EasyConfig(
        openai_api_key="test-key",
        capture_aec_reference=True,
    )
    assert config.capture_aec_reference is True


def test_capture_audio_defaults_on_and_accepts_predicate():
    consent = False
    config = EasyConfig(
        openai_api_key="test-key",
        capture_audio=lambda: consent,
    )

    assert callable(config.capture_audio)
    assert config.capture_audio() is False


def test_capture_audio_rejects_invalid_policy():
    with pytest.raises(ValueError, match="capture_audio"):
        EasyConfig(openai_api_key="test-key", capture_audio="yes")  # type: ignore[arg-type]

    async def async_policy() -> bool:
        return True

    with pytest.raises(ValueError, match="synchronous"):
        EasyConfig(openai_api_key="test-key", capture_audio=async_policy)


def test_on_agent_failure_accepts_text_and_callable():
    static = EasyConfig(openai_api_key="test-key", on_agent_failure="Please try again.")
    dynamic = EasyConfig(
        openai_api_key="test-key",
        on_agent_failure=lambda error: type(error).__name__,
    )

    assert static.on_agent_failure == "Please try again."
    assert callable(dynamic.on_agent_failure)


@pytest.mark.parametrize("value", [" ", 42])
def test_on_agent_failure_rejects_invalid_policy(value):
    with pytest.raises(ValueError, match="on_agent_failure"):
        EasyConfig(openai_api_key="test-key", on_agent_failure=value)  # type: ignore[arg-type]


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


def test_browser_preset_preserves_echo_cancellation_override(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    config = EasyConfig.browser(enable_echo_cancellation=False)

    assert config.enable_echo_cancellation is False
    assert config.echo_cancellation is not None
    assert config.echo_cancellation.enabled is False


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
    elif isinstance(config.tts, (DeepgramTTSConfig, CartesiaTTSConfig)):
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
    elif isinstance(config.tts, (DeepgramTTSConfig, CartesiaTTSConfig)):
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


def test_easycat_config_echo_cancellation_defaults_on_for_local_only():
    local = EasyConfig(openai_api_key="test-key", transport=LocalTransportConfig())

    assert local.echo_cancellation == EchoCancellationConfig(enabled=True)


def test_easycat_config_echo_cancellation_defaults_off_without_playback_timed_reference():
    websocket = EasyConfig(openai_api_key="test-key", transport=WebSocketTransportConfig())
    webtransport = EasyConfig(
        openai_api_key="test-key",
        transport=WebTransportTransportConfig(),
    )
    twilio = EasyConfig(openai_api_key="test-key", transport=TwilioTransportConfig())
    webrtc = EasyConfig(openai_api_key="test-key", transport=WebRTCTransportConfig())

    assert websocket.echo_cancellation == EchoCancellationConfig(enabled=False)
    assert webtransport.echo_cancellation == EchoCancellationConfig(enabled=False)
    assert twilio.echo_cancellation == EchoCancellationConfig(enabled=False)
    assert webrtc.echo_cancellation == EchoCancellationConfig(enabled=False)


@pytest.mark.parametrize(
    "transport",
    [WebSocketTransportConfig(), WebTransportTransportConfig()],
)
def test_remote_browser_transports_preserve_explicit_server_aec_opt_in(transport):
    config = EasyConfig(
        openai_api_key="test-key",
        transport=transport,
        enable_echo_cancellation=True,
    )

    assert config.echo_cancellation == EchoCancellationConfig(enabled=True)


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


def test_easycat_default_preroll_covers_vad_confirmation_and_onset_margin() -> None:
    config = EasyConfig(openai_api_key="test-key")

    assert config.turn_taking.pre_roll_ms >= config.vad.min_speech_duration_ms + 150


def test_easycat_warns_when_vad_confirmation_exceeds_preroll_margin(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="easycat.config")

    EasyConfig(
        openai_api_key="test-key",
        debug="off",
        vad=VADConfig(min_speech_duration_ms=400),
        turn_taking=TurnManagerConfig(pre_roll_ms=450),
    )

    assert "pre_roll_ms=450 is shorter than vad.min_speech_duration_ms=400" in caplog.text
    assert "Increase pre_roll_ms to at least 550" in caplog.text


def test_easycat_does_not_warn_about_vad_preroll_in_push_to_talk_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="easycat.config")

    EasyConfig(
        openai_api_key="test-key",
        debug="off",
        vad=VADConfig(min_speech_duration_ms=400),
        turn_taking=TurnManagerConfig(pre_roll_ms=0, mode=TurnMode.PUSH_TO_TALK),
    )

    assert "pre_roll_ms" not in caplog.text


def test_easycat_does_not_warn_when_native_stt_disables_vad(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="easycat.config")

    EasyConfig(
        stt=ElevenLabsSTTConfig(api_key="test-key"),
        tts=OpenAITTSConfig(api_key="test-key"),
        debug="off",
        vad=VADConfig(min_speech_duration_ms=400),
        turn_taking=TurnManagerConfig(pre_roll_ms=0),
    )

    assert "pre_roll_ms" not in caplog.text


def test_easycat_warns_when_voicemail_enables_vad_with_native_stt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="easycat.config")

    EasyConfig(
        stt=ElevenLabsSTTConfig(api_key="test-key"),
        tts=OpenAITTSConfig(api_key="test-key"),
        debug="off",
        vad=VADConfig(min_speech_duration_ms=400),
        turn_taking=TurnManagerConfig(pre_roll_ms=0),
        telephony=TelephonyConfig(enable_voicemail_detector=True),
    )

    assert "pre_roll_ms=0 is shorter than vad.min_speech_duration_ms=400" in caplog.text


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
