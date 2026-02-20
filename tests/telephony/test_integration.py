"""End-to-end telephony scenario tests (Task 6.8).

Tests full DTMF and voicemail flows using mocked Twilio messages.
"""

from __future__ import annotations

import asyncio
import math
import struct
import time

from easycat.events import (
    DTMF,
    DTMFAggregated,
    EventBus,
    VADStartSpeaking,
    VADStopSpeaking,
    VoicemailDetected,
)
from easycat.telephony.dtmf import (
    DTMFAggregator,
    DTMFAggregatorConfig,
    emit_twilio_dtmf,
)
from easycat.telephony.twiml import emit_gather_digits
from easycat.telephony.voicemail import (
    BeepDetectorConfig,
    VoicemailDetector,
    VoicemailDetectorConfig,
    VoicemailPolicy,
    VoicemailPolicyConfig,
    VoicemailPolicyHandler,
    emit_twilio_amd,
)


def _generate_tone(frequency: float, duration_s: float, sample_rate: int = 16000) -> bytes:
    num_samples = int(sample_rate * duration_s)
    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        value = int(16000 * math.sin(2 * math.pi * frequency * t))
        samples.append(max(-32768, min(32767, value)))
    return struct.pack(f"<{len(samples)}h", *samples)


# ── Scenario 1: Full DTMF flow via Twilio Media Streams ──────────


class TestDTMFEndToEnd:
    """Twilio sends digit messages -> parsed -> aggregated -> event emitted."""

    async def test_twilio_dtmf_to_aggregated(self) -> None:
        bus = EventBus()
        dtmf_events: list[DTMF] = []
        aggregated_events: list[DTMFAggregated] = []

        bus.subscribe(DTMF, lambda e: dtmf_events.append(e))
        bus.subscribe(DTMFAggregated, lambda e: aggregated_events.append(e))

        # Set up aggregator with # terminator
        config = DTMFAggregatorConfig(
            terminators=frozenset({"#"}),
            timeout_ms=5000,
        )
        agg = DTMFAggregator(bus, config)
        agg.start()

        try:
            # Simulate Twilio WebSocket messages for an account number entry
            sid = "MZ123"
            twilio_messages = [
                {"event": "dtmf", "streamSid": sid, "dtmf": {"digit": d}} for d in "1928#"
            ]

            # Also intersperse non-DTMF messages
            other_messages = [
                {"event": "media", "streamSid": "MZ123", "media": {"payload": "abc123"}},
                {"event": "start", "streamSid": "MZ123", "start": {"accountSid": "AC123"}},
            ]

            # Process all messages (DTMF and non-DTMF)
            all_messages = [
                other_messages[0],
                twilio_messages[0],
                twilio_messages[1],
                other_messages[1],
                twilio_messages[2],
                twilio_messages[3],
                twilio_messages[4],
            ]

            for msg in all_messages:
                await emit_twilio_dtmf(msg, bus)

            # Verify all 5 DTMF digits were parsed
            assert len(dtmf_events) == 5
            assert [e.digit for e in dtmf_events] == ["1", "9", "2", "8", "#"]

            # Verify aggregation triggered by # terminator
            assert len(aggregated_events) == 1
            assert aggregated_events[0].sequence == "1928#"
        finally:
            agg.stop()

    async def test_twilio_dtmf_with_timeout_aggregation(self) -> None:
        bus = EventBus()
        aggregated_events: list[DTMFAggregated] = []
        bus.subscribe(DTMFAggregated, lambda e: aggregated_events.append(e))

        config = DTMFAggregatorConfig(timeout_ms=100)
        agg = DTMFAggregator(bus, config)
        agg.start()

        try:
            messages = [
                {"event": "dtmf", "dtmf": {"digit": "4"}},
                {"event": "dtmf", "dtmf": {"digit": "2"}},
            ]
            for msg in messages:
                await emit_twilio_dtmf(msg, bus)

            # Wait for timeout
            await asyncio.sleep(0.2)

            assert len(aggregated_events) == 1
            assert aggregated_events[0].sequence == "42"
        finally:
            agg.stop()

    async def test_twilio_dtmf_max_length(self) -> None:
        bus = EventBus()
        aggregated_events: list[DTMFAggregated] = []
        bus.subscribe(DTMFAggregated, lambda e: aggregated_events.append(e))

        config = DTMFAggregatorConfig(max_length=3, timeout_ms=5000)
        agg = DTMFAggregator(bus, config)
        agg.start()

        try:
            for digit in "123456":
                msg = {"event": "dtmf", "dtmf": {"digit": digit}}
                await emit_twilio_dtmf(msg, bus)

            # Should have two aggregated events (123 and 456)
            assert len(aggregated_events) == 2
            assert aggregated_events[0].sequence == "123"
            assert aggregated_events[1].sequence == "456"
        finally:
            agg.stop()


# ── Scenario 2: DTMF flow via Gather webhook ─────────────────────


class TestGatherEndToEnd:
    """Gather webhook -> parsed -> aggregated -> event emitted."""

    async def test_gather_digits_to_aggregated(self) -> None:
        bus = EventBus()
        dtmf_events: list[DTMF] = []
        aggregated_events: list[DTMFAggregated] = []

        bus.subscribe(DTMF, lambda e: dtmf_events.append(e))
        bus.subscribe(DTMFAggregated, lambda e: aggregated_events.append(e))

        config = DTMFAggregatorConfig(terminators=frozenset({"#"}), timeout_ms=5000)
        agg = DTMFAggregator(bus, config)
        agg.start()

        try:
            # Simulate a Gather webhook that already includes the terminator
            payload = {
                "AccountSid": "AC123",
                "CallSid": "CA456",
                "Digits": "5678#",
            }
            await emit_gather_digits(payload, bus)

            assert len(dtmf_events) == 5
            assert [e.digit for e in dtmf_events] == ["5", "6", "7", "8", "#"]
            assert len(aggregated_events) == 1
            assert aggregated_events[0].sequence == "5678#"
        finally:
            agg.stop()


# ── Scenario 3: Voicemail detection via Twilio AMD ───────────────


class TestVoicemailAmdEndToEnd:
    """Outbound call -> AMD detects machine -> policy executes."""

    async def test_amd_machine_with_hangup_policy(self) -> None:
        bus = EventBus()
        detected: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: detected.append(e))

        # Set up policy handler
        policy_config = VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        policy_handler = VoicemailPolicyHandler(bus, policy_config)
        policy_handler.start()

        try:
            # Simulate Twilio AMD callback
            amd_payload = {
                "AccountSid": "AC123",
                "CallSid": "CA456",
                "AnsweredBy": "machine_start",
            }
            await emit_twilio_amd(amd_payload, bus)

            assert len(detected) == 1
            assert detected[0].result == "machine"
            assert policy_handler.last_action is not None
            assert policy_handler.last_action["type"] == "hang_up"
            assert "<Hangup/>" in policy_handler.last_action["twiml"]
        finally:
            policy_handler.stop()

    async def test_amd_human_no_action(self) -> None:
        bus = EventBus()
        policy_config = VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        policy_handler = VoicemailPolicyHandler(bus, policy_config)
        policy_handler.start()

        try:
            amd_payload = {"AnsweredBy": "human"}
            await emit_twilio_amd(amd_payload, bus)

            # Human detection should not trigger any policy action
            assert policy_handler.last_action is None
        finally:
            policy_handler.stop()

    async def test_amd_machine_with_leave_message_policy(self) -> None:
        bus = EventBus()
        policy_config = VoicemailPolicyConfig(
            policy=VoicemailPolicy.LEAVE_MESSAGE,
            message_text="Hi, this is a test message.",
            wait_for_beep=True,
        )
        policy_handler = VoicemailPolicyHandler(bus, policy_config)
        policy_handler.start()

        try:
            await emit_twilio_amd({"AnsweredBy": "machine_end_beep"}, bus)

            assert policy_handler.last_action is not None
            assert policy_handler.last_action["type"] == "leave_message"
            assert policy_handler.last_action["message_text"] == "Hi, this is a test message."
        finally:
            policy_handler.stop()

    async def test_amd_machine_with_transfer_policy(self) -> None:
        bus = EventBus()
        policy_config = VoicemailPolicyConfig(
            policy=VoicemailPolicy.TRANSFER,
            transfer_number="+15559876543",
        )
        policy_handler = VoicemailPolicyHandler(bus, policy_config)
        policy_handler.start()

        try:
            await emit_twilio_amd({"AnsweredBy": "machine_start"}, bus)

            assert policy_handler.last_action is not None
            assert policy_handler.last_action["type"] == "transfer"
            assert "+15559876543" in policy_handler.last_action["twiml"]
        finally:
            policy_handler.stop()


# ── Scenario 4: Heuristic voicemail with policy ──────────────────


class TestHeuristicVoicemailEndToEnd:
    """Heuristic detection -> policy executes."""

    async def test_monologue_triggers_hangup(self) -> None:
        bus = EventBus()
        detected: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: detected.append(e))

        # Set up detector and policy
        detector_config = VoicemailDetectorConfig(monologue_threshold_s=5.0)
        detector = VoicemailDetector(bus, detector_config)
        detector.start()

        policy_config = VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        policy_handler = VoicemailPolicyHandler(bus, policy_config)
        policy_handler.start()

        try:
            now = time.monotonic()
            # Simulate a long voicemail greeting
            await bus.emit(VADStartSpeaking(timestamp=now))
            await bus.emit(VADStopSpeaking(timestamp=now + 10.0))

            assert len(detected) == 1
            assert detected[0].result == "machine"
            assert policy_handler.last_action is not None
            assert policy_handler.last_action["type"] == "hang_up"
        finally:
            detector.stop()
            policy_handler.stop()

    async def test_beep_triggers_leave_message(self) -> None:
        bus = EventBus()
        detected: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, lambda e: detected.append(e))

        detector_config = VoicemailDetectorConfig(
            beep=BeepDetectorConfig(
                min_frequency_hz=800,
                max_frequency_hz=1200,
                min_duration_ms=100,
                energy_threshold=0.01,
            )
        )
        detector = VoicemailDetector(bus, detector_config)
        detector.start()

        policy_config = VoicemailPolicyConfig(
            policy=VoicemailPolicy.LEAVE_MESSAGE,
            message_text="Please call us back at 555-1234.",
        )
        policy_handler = VoicemailPolicyHandler(bus, policy_config)
        policy_handler.start()

        try:
            # Stream beep audio chunks
            for _ in range(4):  # 200ms of 1000Hz tone
                chunk = _generate_tone(1000.0, 0.05, sample_rate=16000)
                result = await detector.process_audio(chunk)
                if result:
                    break

            assert len(detected) == 1
            assert policy_handler.last_action is not None
            assert policy_handler.last_action["type"] == "leave_message"
            assert policy_handler.last_action["message_text"] == "Please call us back at 555-1234."
        finally:
            detector.stop()
            policy_handler.stop()


# ── Scenario 5: Combined DTMF + voicemail ────────────────────────


class TestCombinedScenarios:
    """Call connects -> DTMF prompt -> user enters digits -> agent processes."""

    async def test_dtmf_and_voicemail_coexist(self) -> None:
        """Both DTMF aggregation and voicemail detection active simultaneously."""
        bus = EventBus()
        dtmf_events: list[DTMF] = []
        aggregated_events: list[DTMFAggregated] = []
        vm_events: list[VoicemailDetected] = []

        bus.subscribe(DTMF, lambda e: dtmf_events.append(e))
        bus.subscribe(DTMFAggregated, lambda e: aggregated_events.append(e))
        bus.subscribe(VoicemailDetected, lambda e: vm_events.append(e))

        # Set up DTMF aggregator
        agg_config = DTMFAggregatorConfig(terminators=frozenset({"#"}))
        agg = DTMFAggregator(bus, agg_config)
        agg.start()

        # Set up voicemail detector
        vm_config = VoicemailDetectorConfig(monologue_threshold_s=8.0)
        vm_detector = VoicemailDetector(bus, vm_config)
        vm_detector.start()

        try:
            # User presses DTMF digits (short human interaction — not voicemail)
            now = time.monotonic()
            await bus.emit(VADStartSpeaking(timestamp=now))
            await bus.emit(VADStopSpeaking(timestamp=now + 2.0))

            for digit in "42#":
                msg = {"event": "dtmf", "dtmf": {"digit": digit}}
                await emit_twilio_dtmf(msg, bus)

            assert len(dtmf_events) == 3
            assert len(aggregated_events) == 1
            assert aggregated_events[0].sequence == "42#"
            # Short speech should not trigger voicemail
            assert len(vm_events) == 0
        finally:
            agg.stop()
            vm_detector.stop()

    async def test_multiple_dtmf_sequences_in_call(self) -> None:
        """User enters multiple DTMF sequences during a call."""
        bus = EventBus()
        aggregated_events: list[DTMFAggregated] = []
        bus.subscribe(DTMFAggregated, lambda e: aggregated_events.append(e))

        config = DTMFAggregatorConfig(terminators=frozenset({"#"}), timeout_ms=5000)
        agg = DTMFAggregator(bus, config)
        agg.start()

        try:
            # First prompt: "Enter your account number"
            for digit in "123456#":
                await bus.emit(DTMF(digit=digit))

            # Second prompt: "Enter your PIN"
            for digit in "9876#":
                await bus.emit(DTMF(digit=digit))

            assert len(aggregated_events) == 2
            assert aggregated_events[0].sequence == "123456#"
            assert aggregated_events[1].sequence == "9876#"
        finally:
            agg.stop()
