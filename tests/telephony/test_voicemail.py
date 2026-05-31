"""Tests for voicemail detection and policy handling (Tasks 6.5, 6.6, 6.7)."""

from __future__ import annotations

import math
import struct
import time

from easycat.events import (
    CallAnswered,
    CallInitiated,
    CallStateChanged,
    EventBus,
    VADStartSpeaking,
    VADStopSpeaking,
    VoicemailDetected,
)
from easycat.telephony.call_state import OutboundCallState
from easycat.telephony.voicemail import (
    BeepDetectorConfig,
    VoicemailDetector,
    VoicemailDetectorConfig,
    VoicemailPolicy,
    VoicemailPolicyConfig,
    VoicemailPolicyHandler,
    emit_twilio_amd,
    parse_twilio_amd_webhook,
)

# ── Task 6.5: Twilio AMD result consumer ─────────────────────────


class TestParseTwilioAmdWebhook:
    """Tests for parse_twilio_amd_webhook."""

    def test_human_result(self) -> None:
        event = parse_twilio_amd_webhook({"AnsweredBy": "human"})
        assert event is not None
        assert event.result == "human"

    def test_machine_start(self) -> None:
        event = parse_twilio_amd_webhook({"AnsweredBy": "machine_start"})
        assert event is not None
        assert event.result == "machine"

    def test_machine_end_beep(self) -> None:
        event = parse_twilio_amd_webhook({"AnsweredBy": "machine_end_beep"})
        assert event is not None
        assert event.result == "machine"

    def test_machine_end_silence(self) -> None:
        event = parse_twilio_amd_webhook({"AnsweredBy": "machine_end_silence"})
        assert event is not None
        assert event.result == "machine"

    def test_machine_end_other(self) -> None:
        event = parse_twilio_amd_webhook({"AnsweredBy": "machine_end_other"})
        assert event is not None
        assert event.result == "machine"

    def test_fax_maps_to_machine(self) -> None:
        event = parse_twilio_amd_webhook({"AnsweredBy": "fax"})
        assert event is not None
        assert event.result == "machine"

    def test_unknown_result(self) -> None:
        event = parse_twilio_amd_webhook({"AnsweredBy": "unknown"})
        assert event is not None
        assert event.result == "unknown"

    def test_unrecognized_value_maps_to_unknown(self) -> None:
        event = parse_twilio_amd_webhook({"AnsweredBy": "something_new"})
        assert event is not None
        assert event.result == "unknown"

    def test_missing_answered_by(self) -> None:
        event = parse_twilio_amd_webhook({"CallSid": "CA123"})
        assert event is None

    def test_empty_answered_by(self) -> None:
        event = parse_twilio_amd_webhook({"AnsweredBy": ""})
        assert event is None

    def test_non_string_answered_by(self) -> None:
        event = parse_twilio_amd_webhook({"AnsweredBy": 42})
        assert event is None

    def test_case_insensitive(self) -> None:
        event = parse_twilio_amd_webhook({"AnsweredBy": "HUMAN"})
        assert event is not None
        assert event.result == "human"

    def test_typical_twilio_amd_payload(self) -> None:
        """Simulate a realistic Twilio AMD callback."""
        payload = {
            "AccountSid": "AC123",
            "ApiVersion": "2010-04-01",
            "CallSid": "CA456",
            "CallStatus": "in-progress",
            "Called": "+15551234567",
            "Caller": "+15559876543",
            "AnsweredBy": "machine_start",
            "MachineDetectionDuration": "3500",
        }
        event = parse_twilio_amd_webhook(payload)
        assert event is not None
        assert event.result == "machine"
        assert event.call_sid == "CA456"


class TestEmitTwilioAmd:
    """Tests for emit_twilio_amd convenience function."""

    async def test_emits_valid_amd(self) -> None:
        bus = EventBus()
        received: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: received.append(e))

        result = await emit_twilio_amd({"AnsweredBy": "machine_start"}, bus)
        assert result is not None
        assert result.result == "machine"
        assert len(received) == 1

    async def test_skips_non_amd(self) -> None:
        bus = EventBus()
        received: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: received.append(e))

        result = await emit_twilio_amd({"CallSid": "CA123"}, bus)
        assert result is None
        assert len(received) == 0


# ── Task 6.6: Heuristic voicemail detection ──────────────────────


def _generate_tone(frequency: float, duration_s: float, sample_rate: int = 16000) -> bytes:
    """Generate a PCM16 sine wave tone."""
    num_samples = int(sample_rate * duration_s)
    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        value = int(16000 * math.sin(2 * math.pi * frequency * t))
        samples.append(max(-32768, min(32767, value)))
    return struct.pack(f"<{len(samples)}h", *samples)


def _generate_silence(duration_s: float, sample_rate: int = 16000) -> bytes:
    """Generate silent PCM16 audio."""
    num_samples = int(sample_rate * duration_s)
    return b"\x00\x00" * num_samples


class TestVoicemailDetectorMonologue:
    """Tests for monologue-based voicemail detection."""

    async def test_long_monologue_detected(self) -> None:
        bus = EventBus()
        detected: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: detected.append(e))

        detector = VoicemailDetector(bus, VoicemailDetectorConfig(monologue_threshold_s=5.0))
        detector.start()

        try:
            now = time.monotonic()
            # Simulate 6 seconds of continuous speech
            await bus.emit(VADStartSpeaking(timestamp=now))
            await bus.emit(VADStopSpeaking(timestamp=now + 6.0))

            assert len(detected) == 1
            assert detected[0].result == "machine"
        finally:
            detector.stop()

    async def test_short_speech_not_detected(self) -> None:
        bus = EventBus()
        detected: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: detected.append(e))

        detector = VoicemailDetector(bus, VoicemailDetectorConfig(monologue_threshold_s=8.0))
        detector.start()

        try:
            now = time.monotonic()
            # Simulate 3 seconds of speech (well under threshold)
            await bus.emit(VADStartSpeaking(timestamp=now))
            await bus.emit(VADStopSpeaking(timestamp=now + 3.0))

            assert len(detected) == 0
        finally:
            detector.stop()

    async def test_emits_only_once(self) -> None:
        bus = EventBus()
        detected: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: detected.append(e))

        detector = VoicemailDetector(bus, VoicemailDetectorConfig(monologue_threshold_s=2.0))
        detector.start()

        try:
            now = time.monotonic()
            # Two long monologues
            await bus.emit(VADStartSpeaking(timestamp=now))
            await bus.emit(VADStopSpeaking(timestamp=now + 5.0))
            await bus.emit(VADStartSpeaking(timestamp=now + 6.0))
            await bus.emit(VADStopSpeaking(timestamp=now + 12.0))

            # Should only emit once
            assert len(detected) == 1
        finally:
            detector.stop()

    async def test_reset_allows_re_detection(self) -> None:
        bus = EventBus()
        detected: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: detected.append(e))

        detector = VoicemailDetector(bus, VoicemailDetectorConfig(monologue_threshold_s=2.0))
        detector.start()

        try:
            now = time.monotonic()
            await bus.emit(VADStartSpeaking(timestamp=now))
            await bus.emit(VADStopSpeaking(timestamp=now + 5.0))
            assert len(detected) == 1

            detector.reset()

            await bus.emit(VADStartSpeaking(timestamp=now + 10.0))
            await bus.emit(VADStopSpeaking(timestamp=now + 15.0))
            assert len(detected) == 2
        finally:
            detector.stop()

    async def test_resets_and_stamps_call_sid_across_sequential_calls(self) -> None:
        """CallInitiated re-arms the detector per call and stamps call_sid.

        Mirrors the per-call-reset convention of the peer detectors so a
        session placing multiple sequential calls keeps detecting monologues
        (rather than latching after the first), and so the emitted event
        carries the active call_sid for the state machine's stale-event guard.
        """
        bus = EventBus()
        detected: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: detected.append(e))

        detector = VoicemailDetector(bus, VoicemailDetectorConfig(monologue_threshold_s=2.0))
        detector.start()

        try:
            now = time.monotonic()

            # First call.
            await bus.emit(CallInitiated(call_sid="CA1", to="+15551112222", from_="+15559998888"))
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VADStartSpeaking(timestamp=now))
            await bus.emit(VADStopSpeaking(timestamp=now + 5.0))
            assert len(detected) == 1
            assert detected[0].call_sid == "CA1"

            # Second sequential call — CallInitiated re-arms the detector.
            await bus.emit(CallInitiated(call_sid="CA2", to="+15553334444", from_="+15559998888"))
            await bus.emit(CallAnswered(call_sid="CA2"))
            await bus.emit(VADStartSpeaking(timestamp=now + 10.0))
            await bus.emit(VADStopSpeaking(timestamp=now + 15.0))
            assert len(detected) == 2
            assert detected[1].call_sid == "CA2"
        finally:
            detector.stop()


class TestVoicemailDetectorBeep:
    """Tests for beep-based voicemail detection."""

    async def test_beep_detected(self) -> None:
        bus = EventBus()
        detected: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: detected.append(e))

        config = VoicemailDetectorConfig(
            beep=BeepDetectorConfig(
                min_frequency_hz=800,
                max_frequency_hz=1200,
                min_duration_ms=100,
                energy_threshold=0.01,
                sample_rate=16000,
            )
        )
        detector = VoicemailDetector(bus, config)
        detector.start()

        try:
            # Generate a 1000Hz tone in chunks to simulate streaming
            # Each chunk is 50ms, we need >100ms total
            for _ in range(4):  # 200ms total
                chunk = _generate_tone(1000.0, 0.05, sample_rate=16000)
                result = await detector.process_audio(chunk)
                if result:
                    break

            assert len(detected) == 1
            assert detected[0].result == "machine"
        finally:
            detector.stop()

    async def test_silence_not_detected_as_beep(self) -> None:
        bus = EventBus()
        detected: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: detected.append(e))

        config = VoicemailDetectorConfig(
            beep=BeepDetectorConfig(
                min_duration_ms=100,
                energy_threshold=0.01,
            )
        )
        detector = VoicemailDetector(bus, config)
        detector.start()

        try:
            silence = _generate_silence(0.5)
            await detector.process_audio(silence)
            assert len(detected) == 0
        finally:
            detector.stop()

    async def test_wrong_frequency_not_detected(self) -> None:
        bus = EventBus()
        detected: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: detected.append(e))

        config = VoicemailDetectorConfig(
            beep=BeepDetectorConfig(
                min_frequency_hz=800,
                max_frequency_hz=1200,
                min_duration_ms=100,
                energy_threshold=0.01,
            )
        )
        detector = VoicemailDetector(bus, config)
        detector.start()

        try:
            # 200Hz is well outside the 800-1200Hz range
            for _ in range(4):
                chunk = _generate_tone(200.0, 0.05, sample_rate=16000)
                await detector.process_audio(chunk)

            assert len(detected) == 0
        finally:
            detector.stop()

    async def test_empty_audio(self) -> None:
        bus = EventBus()
        detector = VoicemailDetector(bus)
        detector.start()

        try:
            result = await detector.process_audio(b"")
            assert result is False
        finally:
            detector.stop()


# ── Task 6.7: Voicemail policy handler ───────────────────────────


class TestVoicemailPolicyHandler:
    """Tests for VoicemailPolicyHandler."""

    async def test_hang_up_policy(self) -> None:
        bus = EventBus()
        config = VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        handler = VoicemailPolicyHandler(bus, config)
        handler.start()

        try:
            await bus.emit(VoicemailDetected(result="machine"))

            assert handler.last_action is not None
            assert handler.last_action["type"] == "hang_up"
            assert "<Hangup/>" in handler.last_action["twiml"]
        finally:
            handler.stop()

    async def test_leave_message_policy(self) -> None:
        bus = EventBus()
        config = VoicemailPolicyConfig(
            policy=VoicemailPolicy.LEAVE_MESSAGE,
            message_text="Hi, please call us back.",
            wait_for_beep=True,
        )
        handler = VoicemailPolicyHandler(bus, config)
        handler.start()

        try:
            await bus.emit(VoicemailDetected(result="machine"))

            assert handler.last_action is not None
            assert handler.last_action["type"] == "leave_message"
            assert handler.last_action["message_text"] == "Hi, please call us back."
            assert handler.last_action["wait_for_beep"] is True
        finally:
            handler.stop()

    async def test_transfer_policy(self) -> None:
        bus = EventBus()
        config = VoicemailPolicyConfig(
            policy=VoicemailPolicy.TRANSFER,
            transfer_number="+15551234567",
        )
        handler = VoicemailPolicyHandler(bus, config)
        handler.start()

        try:
            await bus.emit(VoicemailDetected(result="machine"))

            assert handler.last_action is not None
            assert handler.last_action["type"] == "transfer"
            assert handler.last_action["transfer_number"] == "+15551234567"
            assert "<Dial>" in handler.last_action["twiml"]
        finally:
            handler.stop()

    async def test_human_detection_ignored(self) -> None:
        bus = EventBus()
        config = VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        handler = VoicemailPolicyHandler(bus, config)
        handler.start()

        try:
            await bus.emit(VoicemailDetected(result="human"))
            assert handler.last_action is None
        finally:
            handler.stop()

    async def test_unknown_detection_ignored(self) -> None:
        bus = EventBus()
        config = VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        handler = VoicemailPolicyHandler(bus, config)
        handler.start()

        try:
            await bus.emit(VoicemailDetected(result="unknown"))
            assert handler.last_action is None
        finally:
            handler.stop()

    async def test_only_acts_once(self) -> None:
        bus = EventBus()
        config = VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        handler = VoicemailPolicyHandler(bus, config)
        handler.start()

        try:
            await bus.emit(VoicemailDetected(result="machine"))
            first_action = handler.last_action

            await bus.emit(VoicemailDetected(result="machine"))
            # Action should not have changed (still the same object)
            assert handler.last_action is first_action
        finally:
            handler.stop()

    async def test_rearms_on_voicemail_pickup_then_late_voicemail(self) -> None:
        # Regression: a voicemail pickup (VOICEMAIL -> HUMAN) must re-arm the
        # handler so a subsequent late-voicemail re-entry is handled again
        # instead of being dropped as a duplicate machine detection.
        bus = EventBus()
        config = VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        handler = VoicemailPolicyHandler(bus, config)
        handler.start()

        try:
            await bus.emit(VoicemailDetected(result="machine"))
            first_action = handler.last_action
            assert first_action is not None
            assert handler._action_taken is True

            # Human picks up during the greeting — state machine reports the
            # authoritative VOICEMAIL -> HUMAN transition.
            await bus.emit(
                CallStateChanged(
                    old=OutboundCallState.VOICEMAIL,
                    new=OutboundCallState.HUMAN,
                )
            )
            assert handler._action_taken is False

            # Late voicemail re-entry: a new machine detection is handled again.
            await bus.emit(VoicemailDetected(result="machine"))
            assert handler.last_action is not None
            assert handler.last_action is not first_action
        finally:
            handler.stop()

    async def test_non_pickup_state_change_does_not_rearm(self) -> None:
        # A transition that does not leave VOICEMAIL must not re-arm.
        bus = EventBus()
        config = VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        handler = VoicemailPolicyHandler(bus, config)
        handler.start()

        try:
            await bus.emit(VoicemailDetected(result="machine"))
            first_action = handler.last_action
            assert first_action is not None

            # Unrelated transition (not leaving VOICEMAIL) keeps the handler armed-off.
            await bus.emit(
                CallStateChanged(
                    old=OutboundCallState.CLASSIFYING,
                    new=OutboundCallState.VOICEMAIL,
                )
            )
            assert handler._action_taken is True

            await bus.emit(VoicemailDetected(result="machine"))
            assert handler.last_action is first_action
        finally:
            handler.stop()

    async def test_stop_resets_state(self) -> None:
        bus = EventBus()
        config = VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        handler = VoicemailPolicyHandler(bus, config)
        handler.start()

        await bus.emit(VoicemailDetected(result="machine"))
        assert handler.last_action is not None

        handler.stop()
        assert handler.last_action is None
