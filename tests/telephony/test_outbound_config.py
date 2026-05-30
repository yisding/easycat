"""Tests for outbound call configuration."""

from __future__ import annotations

import pytest

from easycat.config import (
    OutboundCallConfig,
    TelephonyConfig,
    VoicemailDetectionConfig,
    _create_telephony_helpers,
)
from easycat.events import CallFailed, EventBus
from easycat.telephony.call_state import OutboundCallStateMachine
from easycat.telephony.compliance import DNCList
from easycat.telephony.number_health import CallDispositionTracker
from easycat.telephony.screening import CallScreeningDetector


class TestVoicemailDetectionConfig:
    def test_defaults_map_to_twilio(self) -> None:
        cfg = VoicemailDetectionConfig()
        assert cfg.mode == "detect_end_of_greeting"
        assert cfg.async_mode is True
        assert cfg.detection_timeout_s == 30
        assert cfg.speech_threshold_ms == 2400
        assert cfg.speech_end_threshold_ms == 1200
        assert cfg.silence_timeout_ms == 5000
        params = cfg.to_twilio_params()
        assert params["amd_mode"] == "DetectMessageEnd"
        assert params["async_amd"] is True
        assert params["amd_timeout"] == 30
        assert params["speech_threshold"] == 2400
        assert params["speech_end_threshold"] == 1200
        assert params["silence_timeout"] == 5000

    def test_detect_mode_maps_to_enable(self) -> None:
        cfg = VoicemailDetectionConfig(mode="detect")
        assert cfg.to_twilio_params()["amd_mode"] == "Enable"

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_detection_timeout_rejected(self, bad: int) -> None:
        # detection_timeout_s flows into asyncio.sleep with no runtime guard,
        # so non-positive values must fail fast at construction.
        with pytest.raises(ValueError, match="detection_timeout_s must be positive"):
            VoicemailDetectionConfig(detection_timeout_s=bad)

    @pytest.mark.parametrize(
        "field_name",
        ["speech_threshold_ms", "speech_end_threshold_ms", "silence_timeout_ms"],
    )
    def test_negative_threshold_rejected(self, field_name: str) -> None:
        with pytest.raises(ValueError, match=f"{field_name} must be non-negative"):
            VoicemailDetectionConfig(**{field_name: -1})

    def test_zero_thresholds_allowed(self) -> None:
        cfg = VoicemailDetectionConfig(
            speech_threshold_ms=0,
            speech_end_threshold_ms=0,
            silence_timeout_ms=0,
        )
        assert cfg.speech_threshold_ms == 0


class TestOutboundCallConfig:
    def test_defaults(self) -> None:
        cfg = OutboundCallConfig(from_number="+1555")
        assert cfg.from_number == "+1555"
        # Voicemail-detection defaults live on the nested config now.
        assert cfg.voicemail_detection.mode == "detect_end_of_greeting"
        assert cfg.voicemail_detection.async_mode is True
        assert cfg.voicemail_detection.detection_timeout_s == 30
        assert cfg.voicemail_detection.speech_threshold_ms == 2400
        assert cfg.voicemail_detection.speech_end_threshold_ms == 1200
        assert cfg.voicemail_detection.silence_timeout_ms == 5000
        assert cfg.enable_screening_detection is True
        assert cfg.screening_response == ""
        assert cfg.screening_use_agent is False
        assert cfg.enable_realtime_transcription is True
        assert cfg.classification_gate is True
        assert cfg.classification_gate_timeout_s == 5.0
        assert cfg.classification_gate_hold_audio == ""
        assert cfg.max_call_duration_s == 300
        assert cfg.callee_language == "en"
        assert cfg.max_screening_turns == 3
        assert cfg.voicemail_pickup_window_s == 60.0

    def test_all_fields_configurable(self) -> None:
        async def _dummy_agent(ctx: dict) -> dict:
            return {"action": "wait"}

        vm = VoicemailDetectionConfig(
            mode="detect",
            async_mode=False,
            detection_timeout_s=15,
            speech_threshold_ms=3000,
            speech_end_threshold_ms=2000,
            silence_timeout_ms=8000,
        )
        cfg = OutboundCallConfig(
            from_number="+1999",
            voicemail_detection=vm,
            enable_screening_detection=False,
            screening_response="Hi I'm Sarah",
            screening_use_agent=True,
            ivr_agent_callback=_dummy_agent,
            max_screening_turns=5,
            enable_realtime_transcription=False,
            classification_gate=False,
            classification_gate_timeout_s=3.0,
            classification_gate_hold_audio="One moment please",
            max_call_duration_s=600,
            voicemail_pickup_window_s=45.0,
            callee_language="es",
            twilio_account_sid="AC123",
            twilio_auth_token="token",
        )
        assert cfg.voicemail_detection is vm
        assert cfg.voicemail_detection.mode == "detect"
        assert cfg.voicemail_detection.detection_timeout_s == 15
        assert cfg.voicemail_detection.speech_threshold_ms == 3000
        assert cfg.voicemail_detection.speech_end_threshold_ms == 2000
        assert cfg.voicemail_detection.silence_timeout_ms == 8000
        assert cfg.enable_screening_detection is False
        assert cfg.screening_response == "Hi I'm Sarah"
        assert cfg.screening_use_agent is True
        assert cfg.max_screening_turns == 5
        assert cfg.enable_realtime_transcription is False
        assert cfg.classification_gate is False
        assert cfg.classification_gate_timeout_s == 3.0
        assert cfg.classification_gate_hold_audio == "One moment please"
        assert cfg.max_call_duration_s == 600
        assert cfg.voicemail_pickup_window_s == 45.0
        assert cfg.callee_language == "es"
        assert cfg.twilio_account_sid == "AC123"
        assert cfg.twilio_auth_token == "token"

    def test_screening_response_modes(self) -> None:
        cfg = OutboundCallConfig(
            from_number="+1555",
            screening_use_agent=False,
            screening_response="Hi I'm Sarah",
        )
        assert cfg.screening_use_agent is False
        assert cfg.screening_response == "Hi I'm Sarah"

    def test_classification_gate_defaults(self) -> None:
        cfg = OutboundCallConfig(from_number="+1555")
        assert cfg.classification_gate is True
        assert cfg.classification_gate_timeout_s == 5.0
        assert cfg.classification_gate_hold_audio == ""

    def test_max_screening_turns_default(self) -> None:
        cfg = OutboundCallConfig(from_number="+1555")
        assert cfg.max_screening_turns == 3

    def test_callee_language_configurable(self) -> None:
        cfg = OutboundCallConfig(from_number="+1555", callee_language="es")
        assert cfg.callee_language == "es"

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_classification_gate_timeout_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError, match="classification_gate_timeout_s must be positive"):
            OutboundCallConfig(from_number="+1555", classification_gate_timeout_s=bad)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_max_call_duration_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="max_call_duration_s must be positive"):
            OutboundCallConfig(from_number="+1555", max_call_duration_s=bad)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_max_screening_turns_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="max_screening_turns must be positive"):
            OutboundCallConfig(from_number="+1555", max_screening_turns=bad)

    def test_negative_late_voicemail_window_rejected(self) -> None:
        with pytest.raises(ValueError, match="late_voicemail_window_s must be non-negative"):
            OutboundCallConfig(from_number="+1555", late_voicemail_window_s=-1.0)

    def test_negative_voicemail_pickup_window_rejected(self) -> None:
        with pytest.raises(ValueError, match="voicemail_pickup_window_s must be non-negative"):
            OutboundCallConfig(from_number="+1555", voicemail_pickup_window_s=-1.0)

    def test_zero_windows_allowed(self) -> None:
        # Zero disables the window in the state machine; it is valid config.
        cfg = OutboundCallConfig(
            from_number="+1555",
            late_voicemail_window_s=0.0,
            voicemail_pickup_window_s=0.0,
        )
        assert cfg.late_voicemail_window_s == 0.0
        assert cfg.voicemail_pickup_window_s == 0.0


class TestTelephonyConfigExtension:
    def test_enable_outbound_flag(self) -> None:
        cfg = TelephonyConfig(enable_outbound_call_manager=True)
        assert cfg.enable_outbound_call_manager is True

    def test_outbound_config_nested(self) -> None:
        outbound = OutboundCallConfig(from_number="+15559876543")
        cfg = TelephonyConfig(outbound=outbound)
        assert cfg.outbound is outbound
        assert cfg.outbound.from_number == "+15559876543"

    def test_backwards_compatible(self) -> None:
        cfg = TelephonyConfig(enable_dtmf_aggregator=True)
        assert cfg.enable_dtmf_aggregator is True
        assert cfg.enable_voicemail_detector is False
        assert cfg.enable_outbound_call_manager is False
        assert cfg.outbound is None

    def test_outbound_helpers_start_disposition_tracker_before_state_machine(self) -> None:
        bus = EventBus()
        helpers = _create_telephony_helpers(
            bus,
            TelephonyConfig(
                enable_outbound_call_manager=True,
                outbound=OutboundCallConfig(from_number="+15559876543"),
            ),
        )
        tracker_index = next(
            i for i, helper in enumerate(helpers) if isinstance(helper, CallDispositionTracker)
        )
        sm_index = next(
            i for i, helper in enumerate(helpers) if isinstance(helper, OutboundCallStateMachine)
        )
        assert tracker_index < sm_index

    def test_outbound_helpers_wire_inbound_track_filter_on_screening(self) -> None:
        """Screening detector defaults to the inbound track filter.

        Defense-in-depth so the bot's own speech (transcription_track="both")
        cannot trigger a false screening match.  The filter accepts track-less
        events, so it does not break screening in the common pipeline.
        """
        bus = EventBus()
        helpers = _create_telephony_helpers(
            bus,
            TelephonyConfig(
                enable_outbound_call_manager=True,
                outbound=OutboundCallConfig(from_number="+15559876543"),
            ),
        )
        screening = next(helper for helper in helpers if isinstance(helper, CallScreeningDetector))
        assert screening._track_filter == "inbound"

    @pytest.mark.asyncio
    async def test_outbound_helpers_record_specific_failed_disposition(self) -> None:
        bus = EventBus()
        helpers = _create_telephony_helpers(
            bus,
            TelephonyConfig(
                enable_outbound_call_manager=True,
                outbound=OutboundCallConfig(from_number="+15559876543"),
            ),
        )
        tracker = next(helper for helper in helpers if isinstance(helper, CallDispositionTracker))

        for helper in helpers:
            helper.start()
        try:
            await bus.emit(CallFailed(call_sid="CA1", reason="busy"))
            assert tracker._dispositions
            assert tracker._dispositions[0][1] == "busy"
        finally:
            for helper in helpers:
                helper.stop()

    def test_shared_dnc_list_is_wired_to_outbound_manager(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Manager:
            def __init__(self, *_args, **_kwargs) -> None:
                self.dnc_list = None

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

        monkeypatch.setattr("easycat.config.OutboundCallManager", _Manager)

        dnc = DNCList()
        helpers = _create_telephony_helpers(
            EventBus(),
            TelephonyConfig(
                enable_outbound_call_manager=True,
                outbound=OutboundCallConfig(
                    from_number="+15559876543",
                    twilio_account_sid="AC123",
                    twilio_auth_token="secret",
                ),
            ),
            dnc_list=dnc,
        )

        manager = next(helper for helper in helpers if isinstance(helper, _Manager))
        assert manager.dnc_list is dnc
