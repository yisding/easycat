"""Tests for outbound call configuration."""

from __future__ import annotations

import asyncio

import pytest

from easycat.config import (
    OutboundCallConfig,
    TelephonyConfig,
    VoicemailDetectionConfig,
    _create_telephony_helpers,
)
from easycat.events import (
    CallAnswered,
    CallEnded,
    CallFailed,
    CallInitiated,
    EventBus,
    ScreeningResponse,
    VADStartSpeaking,
    VADStopSpeaking,
    VoicemailDetected,
)
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

    @pytest.mark.parametrize("value", ["false", 0, 1, None])
    def test_async_mode_requires_boolean(self, value: object) -> None:
        with pytest.raises(ValueError, match="async_mode must be a boolean"):
            VoicemailDetectionConfig(async_mode=value)  # type: ignore[arg-type]

    def test_validate_rejects_mutated_async_mode(self) -> None:
        cfg = VoicemailDetectionConfig()
        cfg.async_mode = "false"  # type: ignore[assignment]

        with pytest.raises(ValueError, match="async_mode must be a boolean"):
            cfg.validate()

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="voicemail_detection.mode"):
            VoicemailDetectionConfig(mode="detect_end")  # type: ignore[arg-type]

    def test_mutated_invalid_mode_rejected_before_twilio_mapping(self) -> None:
        cfg = VoicemailDetectionConfig()
        cfg.mode = "detect_end"  # type: ignore[assignment]

        with pytest.raises(ValueError, match="voicemail_detection.mode"):
            cfg.to_twilio_params()

    def test_defaults_map_to_telnyx_greeting_end(self) -> None:
        params = VoicemailDetectionConfig().to_telnyx_params()
        assert params == {"answering_machine_detection": "greeting_end"}

    def test_detect_mode_maps_to_telnyx_detect(self) -> None:
        params = VoicemailDetectionConfig(mode="detect").to_telnyx_params()
        assert params == {"answering_machine_detection": "detect"}

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("detect", "detect"),
            ("detect_end_of_greeting", "greeting_end"),
        ],
    )
    def test_telnyx_params_are_valid_amd_tokens(self, mode: str, expected: str) -> None:
        value = VoicemailDetectionConfig(mode=mode).to_telnyx_params()[
            "answering_machine_detection"
        ]
        assert value == expected
        assert value in {
            "premium",
            "detect",
            "detect_beep",
            "detect_words",
            "greeting_end",
            "disabled",
        }

    def test_mutated_invalid_mode_rejected_before_telnyx_mapping(self) -> None:
        cfg = VoicemailDetectionConfig()
        cfg.mode = "detect_end"  # type: ignore[assignment]

        with pytest.raises(ValueError, match="voicemail_detection.mode"):
            cfg.to_telnyx_params()

    @pytest.mark.parametrize("bad", [0, -1, 1.5, True, float("nan"), float("inf")])
    def test_invalid_detection_timeout_rejected(self, bad: object) -> None:
        # detection_timeout_s flows into asyncio.sleep with no runtime guard,
        # so it must be a positive, finite built-in number at construction.
        with pytest.raises(ValueError, match="detection_timeout_s must be positive"):
            VoicemailDetectionConfig(detection_timeout_s=bad)

    @pytest.mark.parametrize(
        ("field_name", "bad"),
        [
            (field_name, bad)
            for field_name in (
                "speech_threshold_ms",
                "speech_end_threshold_ms",
                "silence_timeout_ms",
            )
            for bad in (-1, 1.5, True, float("nan"), float("inf"))
        ],
    )
    def test_invalid_threshold_rejected(self, field_name: str, bad: object) -> None:
        with pytest.raises(ValueError, match=f"{field_name} must be non-negative"):
            VoicemailDetectionConfig(**{field_name: bad})

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
        assert cfg.voicemail_pickup_window_s == 0.0

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

    @pytest.mark.parametrize("bad", [0, -1, True, float("nan"), float("inf")])
    def test_invalid_classification_gate_timeout_rejected(self, bad: object) -> None:
        with pytest.raises(ValueError, match="classification_gate_timeout_s must be positive"):
            OutboundCallConfig(from_number="+1555", classification_gate_timeout_s=bad)

    @pytest.mark.parametrize("bad", [0, -1, True, float("nan"), float("inf")])
    def test_invalid_max_call_duration_rejected(self, bad: object) -> None:
        with pytest.raises(ValueError, match="max_call_duration_s must be positive"):
            OutboundCallConfig(from_number="+1555", max_call_duration_s=bad)

    @pytest.mark.parametrize("bad", [0, -1, 1.5, True, float("nan"), float("inf")])
    def test_invalid_max_screening_turns_rejected(self, bad: object) -> None:
        with pytest.raises(ValueError, match="max_screening_turns must be positive"):
            OutboundCallConfig(from_number="+1555", max_screening_turns=bad)

    @pytest.mark.parametrize("bad", [-1.0, True, float("nan"), float("inf")])
    def test_invalid_late_voicemail_window_rejected(self, bad: object) -> None:
        with pytest.raises(ValueError, match="late_voicemail_window_s must be non-negative"):
            OutboundCallConfig(from_number="+1555", late_voicemail_window_s=bad)

    @pytest.mark.parametrize("bad", [-1.0, True, float("nan"), float("inf")])
    def test_invalid_voicemail_pickup_window_rejected(self, bad: object) -> None:
        with pytest.raises(ValueError, match="voicemail_pickup_window_s must be non-negative"):
            OutboundCallConfig(from_number="+1555", voicemail_pickup_window_s=bad)

    def test_zero_windows_allowed(self) -> None:
        # Zero disables the window in the state machine; it is valid config.
        cfg = OutboundCallConfig(
            from_number="+1555",
            late_voicemail_window_s=0.0,
            voicemail_pickup_window_s=0.0,
        )
        assert cfg.late_voicemail_window_s == 0.0
        assert cfg.voicemail_pickup_window_s == 0.0

    @pytest.mark.parametrize(
        "field_name",
        [
            "enable_screening_detection",
            "screening_use_agent",
            "enable_realtime_transcription",
            "classification_gate",
            "enable_number_health",
            "enable_disposition_tracker",
            "enable_retry_strategy",
        ],
    )
    def test_boolean_policy_rejects_wrong_type(self, field_name: str) -> None:
        with pytest.raises(ValueError, match=rf"{field_name} must be a boolean"):
            OutboundCallConfig(**{field_name: "false"})  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("field_name", "bad", "message"),
        [
            ("classification_gate_timeout_s", 0.0, "classification_gate_timeout_s"),
            ("max_call_duration_s", 0.0, "max_call_duration_s"),
            ("max_screening_turns", 0, "max_screening_turns"),
            ("late_voicemail_window_s", -1.0, "late_voicemail_window_s"),
            ("voicemail_pickup_window_s", -1.0, "voicemail_pickup_window_s"),
        ],
    )
    def test_validate_rejects_post_construction_mutation(
        self,
        field_name: str,
        bad: object,
        message: str,
    ) -> None:
        cfg = OutboundCallConfig(from_number="+1555")
        setattr(cfg, field_name, bad)

        with pytest.raises(ValueError, match=message):
            cfg.validate()

    def test_validate_rechecks_nested_voicemail_detection(self) -> None:
        cfg = OutboundCallConfig(from_number="+1555")
        cfg.voicemail_detection.mode = "detect_end"  # type: ignore[assignment]

        with pytest.raises(ValueError, match="voicemail_detection.mode"):
            cfg.validate()

    def test_telnyx_provider_defaults(self) -> None:
        cfg = OutboundCallConfig(from_number="+1555")
        assert cfg.provider == "twilio"
        assert cfg.telnyx_api_key == ""
        assert cfg.telnyx_connection_id == ""
        assert cfg.telnyx_webhook_url == ""

    def test_telnyx_provider_fields_configurable(self) -> None:
        cfg = OutboundCallConfig(
            from_number="+1555",
            provider="telnyx",
            telnyx_api_key="key",
            telnyx_connection_id="conn-1",
            telnyx_webhook_url="https://example.test/telnyx",
        )
        assert cfg.provider == "telnyx"
        assert cfg.telnyx_api_key == "key"
        assert cfg.telnyx_connection_id == "conn-1"
        assert cfg.telnyx_webhook_url == "https://example.test/telnyx"

    def test_telnyx_api_key_hidden_from_repr(self) -> None:
        cfg = OutboundCallConfig(from_number="+1555", telnyx_api_key="secret-key")

        assert "secret-key" not in repr(cfg)

    @pytest.mark.parametrize("provider", ["plivo", "", None])
    def test_invalid_provider_rejected(self, provider: object) -> None:
        with pytest.raises(ValueError, match="Invalid outbound provider"):
            OutboundCallConfig(from_number="+1555", provider=provider)  # type: ignore[arg-type]

    def test_mutated_invalid_provider_rejected(self) -> None:
        cfg = OutboundCallConfig(from_number="+1555")
        cfg.provider = "sip"  # type: ignore[assignment]

        with pytest.raises(ValueError, match="Invalid outbound provider"):
            cfg.validate()

    def test_telnyx_provider_requires_connection_id(self) -> None:
        with pytest.raises(ValueError, match="telnyx_connection_id is required"):
            OutboundCallConfig(from_number="+1555", provider="telnyx")

    def test_mutated_telnyx_provider_requires_connection_id(self) -> None:
        cfg = OutboundCallConfig(from_number="+1555", twilio_account_sid="AC123")
        cfg.provider = "telnyx"

        with pytest.raises(ValueError, match="telnyx_connection_id is required"):
            cfg.validate()

    def test_twilio_provider_still_allows_blank_credentials(self) -> None:
        cfg = OutboundCallConfig(from_number="+1555")
        cfg.validate()


class TestTelephonyConfigExtension:
    @pytest.mark.parametrize(
        "field_name",
        [
            "enable_dtmf_aggregator",
            "enable_voicemail_detector",
            "enable_outbound_call_manager",
        ],
    )
    def test_enable_flags_require_booleans(self, field_name: str) -> None:
        with pytest.raises(ValueError, match=rf"{field_name} must be a boolean"):
            TelephonyConfig(**{field_name: "false"})  # type: ignore[arg-type]

    def test_outbound_requires_outbound_config(self) -> None:
        with pytest.raises(ValueError, match="telephony.outbound must be an OutboundCallConfig"):
            TelephonyConfig(outbound=object())  # type: ignore[arg-type]

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
        result = _create_telephony_helpers(
            bus,
            TelephonyConfig(
                enable_outbound_call_manager=True,
                outbound=OutboundCallConfig(from_number="+15559876543"),
            ),
        )
        helpers = result.helpers
        tracker_index = next(
            i for i, helper in enumerate(helpers) if isinstance(helper, CallDispositionTracker)
        )
        sm_index = next(
            i for i, helper in enumerate(helpers) if isinstance(helper, OutboundCallStateMachine)
        )
        assert tracker_index < sm_index
        # The typed result surfaces the state machine by name, no isinstance scan.
        assert result.state_machine is helpers[sm_index]

    def test_outbound_helpers_wire_inbound_track_filter_on_screening(self) -> None:
        """Screening detector defaults to the inbound track filter.

        Defense-in-depth so the bot's own speech (transcription_track="both")
        cannot trigger a false screening match.  The filter accepts track-less
        events, so it does not break screening in the common pipeline.
        """
        bus = EventBus()
        result = _create_telephony_helpers(
            bus,
            TelephonyConfig(
                enable_outbound_call_manager=True,
                outbound=OutboundCallConfig(from_number="+15559876543"),
            ),
        )
        screening = result.screening_detector
        assert isinstance(screening, CallScreeningDetector)
        assert screening._track_filter == "inbound"

    @pytest.mark.asyncio
    async def test_vad_voicemail_detector_uses_outbound_call_boundary(self) -> None:
        bus = EventBus(handler_error_policy="raise")
        result = _create_telephony_helpers(
            bus,
            TelephonyConfig(
                enable_voicemail_detector=True,
                enable_outbound_call_manager=True,
                outbound=OutboundCallConfig(from_number="+15559876543"),
            ),
        )
        detector = result.voicemail_detector
        assert detector is not None
        detected: list[VoicemailDetected] = []

        def capture(event: VoicemailDetected) -> None:
            if event.source == "detector":
                detected.append(event)

        bus.subscribe(VoicemailDetected, capture)
        for helper in result.helpers:
            helper.start()
        try:
            await bus.emit(CallInitiated(call_sid="CA-A", to="+15550000001", from_="+15550000002"))
            await bus.emit(CallAnswered(call_sid="CA-A"))
            await bus.emit(VADStartSpeaking(timestamp=10.0))

            await bus.emit(CallInitiated(call_sid="CA-A", to="+15550000001", from_="+15550000002"))
            await bus.emit(CallInitiated(call_sid="CA-B", to="+15550000003", from_="+15550000002"))
            await bus.emit(VADStopSpeaking(timestamp=20.0))

            assert [(event.call_sid, event.result) for event in detected] == [("CA-A", "machine")]

            await bus.emit(CallEnded(call_sid="CA-A"))
            await bus.emit(CallInitiated(call_sid="CA-B", to="+15550000003", from_="+15550000002"))

            assert detector._call_sid == "CA-B"
            assert detector.has_emitted is False
        finally:
            for helper in reversed(result.helpers):
                helper.stop()

    @pytest.mark.asyncio
    async def test_outbound_helpers_record_specific_failed_disposition(self) -> None:
        bus = EventBus()
        helpers = _create_telephony_helpers(
            bus,
            TelephonyConfig(
                enable_outbound_call_manager=True,
                outbound=OutboundCallConfig(from_number="+15559876543"),
            ),
        ).helpers
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

            async def hangup_owned_call(self, _call_sid: str) -> None:
                pass

        monkeypatch.setattr("easycat.config._factory.OutboundCallManager", _Manager)

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
        ).helpers

        manager = next(helper for helper in helpers if isinstance(helper, _Manager))
        assert manager.dnc_list is dnc

    @pytest.mark.asyncio
    async def test_max_duration_hangup_is_wired_to_outbound_manager(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hangups: list[str] = []
        hung_up = asyncio.Event()

        class _Manager:
            def __init__(self, *_args, **_kwargs) -> None:
                self.dnc_list = None

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            async def hangup_owned_call(self, call_sid: str) -> None:
                hangups.append(call_sid)
                hung_up.set()

        monkeypatch.setattr("easycat.config._factory.OutboundCallManager", _Manager)

        bus = EventBus()
        result = _create_telephony_helpers(
            bus,
            TelephonyConfig(
                enable_outbound_call_manager=True,
                outbound=OutboundCallConfig(
                    from_number="+15559876543",
                    twilio_account_sid="AC123",
                    twilio_auth_token="secret",
                    max_call_duration_s=0.01,
                ),
            ),
        )

        result.state_machine.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await asyncio.wait_for(hung_up.wait(), timeout=0.2)
            assert hangups == ["CA1"]
        finally:
            result.state_machine.stop()

    def test_outbound_manager_warns_on_blank_twilio_creds(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Blank Twilio creds surface a loud warning, not a silent skip.

        Regression: manager-enabled + blank creds used to skip the manager
        with no log, so the app failed later with a NoneType AttributeError.
        """
        import logging

        bus = EventBus()
        with caplog.at_level(logging.WARNING, logger="easycat.config"):
            result = _create_telephony_helpers(
                bus,
                TelephonyConfig(
                    enable_outbound_call_manager=True,
                    outbound=OutboundCallConfig(from_number="+15559876543"),
                ),
            )
        # Graceful degradation: manager skipped, but loudly.
        assert not any(type(h).__name__ == "OutboundCallManager" for h in result.helpers)
        assert any(
            "twilio" in rec.message.lower() and rec.levelno == logging.WARNING
            for rec in caplog.records
        )


class TestOutboundPipelineWiring:
    @pytest.mark.asyncio
    async def test_stop_cancels_hold_audio_and_unsubscribes_screening(self) -> None:
        from easycat.config._telephony_wiring import _OutboundPipelineWiring

        class FakeSession:
            def __init__(self) -> None:
                self.hold_started = asyncio.Event()
                self.hold_cancelled = asyncio.Event()

            async def synthesize_bypass(self, _text: str) -> None:
                self.hold_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.hold_cancelled.set()

        bus = EventBus()
        session = FakeSession()
        wiring = _OutboundPipelineWiring(session, bus)  # type: ignore[arg-type]
        wiring.start()

        assert wiring._on_screening_response in bus.subscribers(ScreeningResponse)
        wiring.play_hold_audio("please hold")
        await session.hold_started.wait()
        hold_task = wiring._hold_audio_task
        assert hold_task is not None
        assert wiring._hold_audio_tasks.tasks() == (hold_task,)

        wiring.stop()

        with pytest.raises(asyncio.CancelledError):
            await hold_task
        assert session.hold_cancelled.is_set()
        assert wiring._hold_audio_tasks.empty
        assert wiring._on_screening_response not in bus.subscribers(ScreeningResponse)
        assert wiring._hold_audio_task is None
        wiring.play_hold_audio("late hold")
        await asyncio.sleep(0)
        assert wiring._hold_audio_task is None

    @pytest.mark.asyncio
    async def test_new_hold_audio_replaces_owned_synthesis(self) -> None:
        from easycat.config._telephony_wiring import _OutboundPipelineWiring

        class FakeSession:
            def __init__(self) -> None:
                self.started = {text: asyncio.Event() for text in ("first", "second")}
                self.cancelled = {text: asyncio.Event() for text in ("first", "second")}

            async def synthesize_bypass(self, text: str) -> None:
                self.started[text].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled[text].set()

        session = FakeSession()
        wiring = _OutboundPipelineWiring(session, EventBus())  # type: ignore[arg-type]
        wiring.start()
        try:
            wiring.play_hold_audio("first")
            await session.started["first"].wait()
            first = wiring._hold_audio_task
            assert first is not None

            wiring.play_hold_audio("second")
            await session.started["second"].wait()
            second = wiring._hold_audio_task
            assert second is not None

            await asyncio.sleep(0)
            assert first.cancelled()
            assert session.cancelled["first"].is_set()
            assert wiring._hold_audio_tasks.tasks() == (second,)
        finally:
            wiring.stop()

    @pytest.mark.asyncio
    async def test_flush_gated_audio_propagates_caller_cancellation(self) -> None:
        """A discarded classification timeout must not replay opener audio.

        ``flush_gated_audio`` cancels and awaits the hold-audio task before
        replaying gated opener audio.  If the timeout task itself is cancelled
        while waiting for hold-audio cleanup, that cancellation must propagate
        instead of being mistaken for the child task's expected cancellation.
        """
        import asyncio

        from easycat.audio_format import AudioChunk, AudioFormat
        from easycat.config._telephony_wiring import _OutboundPipelineWiring
        from easycat.events import TTSAudio

        class FakeSession:
            def __init__(self) -> None:
                self.hold_started = asyncio.Event()
                self.hold_cleanup_entered = asyncio.Event()
                self.hold_cleanup_continue = asyncio.Event()
                self.replayed: list[list[TTSAudio]] = []

            async def synthesize_bypass(self, _text: str) -> None:
                self.hold_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.hold_cleanup_entered.set()
                    await self.hold_cleanup_continue.wait()

            async def replay_gated_audio(self, events: list[TTSAudio]) -> None:
                self.replayed.append(events)

        session = FakeSession()
        wiring = _OutboundPipelineWiring(session, EventBus())  # type: ignore[arg-type]
        wiring.start()
        wiring.play_hold_audio("please hold")
        await session.hold_started.wait()

        event = TTSAudio(
            chunk=AudioChunk(
                data=b"\x00" * 100,
                format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
            )
        )
        flush_task = asyncio.create_task(wiring.flush_gated_audio([event]))
        await session.hold_cleanup_entered.wait()

        flush_task.cancel()
        session.hold_cleanup_continue.set()
        with pytest.raises(asyncio.CancelledError):
            await flush_task

        assert session.replayed == []
        wiring.stop()
