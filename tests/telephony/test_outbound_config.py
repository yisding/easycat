"""Tests for outbound call configuration."""

from __future__ import annotations

from easycat.config import OutboundCallConfig, TelephonyConfig


class TestOutboundCallConfig:
    def test_defaults(self) -> None:
        cfg = OutboundCallConfig(from_number="+1555")
        assert cfg.from_number == "+1555"
        assert cfg.amd_mode == "DetectMessageEnd"
        assert cfg.async_amd is True
        assert cfg.amd_timeout == 30
        assert cfg.speech_threshold == 2400
        assert cfg.speech_end_threshold == 1200
        assert cfg.silence_timeout == 5000
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
        cfg = OutboundCallConfig(
            from_number="+1999",
            amd_mode="Enable",
            async_amd=False,
            amd_timeout=15,
            speech_threshold=3000,
            speech_end_threshold=2000,
            silence_timeout=8000,
            enable_screening_detection=False,
            screening_response="Hi I'm Sarah",
            screening_use_agent=True,
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
        assert cfg.amd_mode == "Enable"
        assert cfg.async_amd is False
        assert cfg.amd_timeout == 15
        assert cfg.speech_threshold == 3000
        assert cfg.speech_end_threshold == 2000
        assert cfg.silence_timeout == 8000
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
