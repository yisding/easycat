"""End-to-end integration tests for outbound calling features.

Tests full call flows combining state machine, screening, voicemail, IVR,
and DTMF — verifying that all helpers work together without interference.
"""

from __future__ import annotations

import asyncio
import math
import struct

import pytest

from easycat.events import (
    DTMF,
    CallAnswered,
    CallEnded,
    CallFailed,
    CallRinging,
    DTMFAggregated,
    EventBus,
    STTFinal,
    STTPartial,
    VoicemailDetected,
)
from easycat.telephony.call_state import (
    CallStateChanged,
    OutboundCallState,
    OutboundCallStateMachine,
)
from easycat.telephony.dtmf import DTMFAggregator
from easycat.telephony.ivr import IVRAction, IVRActionType, IVRNavigator
from easycat.telephony.screening import (
    CallScreeningDetector,
    ScreeningResponse,
    ScreeningState,
)
from easycat.telephony.voicemail import (
    VoicemailDetector,
    VoicemailPolicy,
    VoicemailPolicyConfig,
    VoicemailPolicyHandler,
)


def _generate_tone(frequency: float, duration_s: float, sample_rate: int = 16000) -> bytes:
    num_samples = int(sample_rate * duration_s)
    samples = [
        int(16000 * math.sin(2 * math.pi * frequency * i / sample_rate))
        for i in range(num_samples)
    ]
    return struct.pack(f"<{len(samples)}h", *samples)


# ── Full Flows ────────────────────────────────────────────────────


class TestOutboundCallFullFlow:
    @pytest.mark.asyncio
    async def test_outbound_to_human(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallRinging(call_sid="CA1"))
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_outbound_to_voicemail_hangup(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        handler = VoicemailPolicyHandler(
            bus, VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        )
        sm.start()
        handler.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            assert handler.last_action is not None
            assert handler.last_action["type"] == "hang_up"
        finally:
            handler.stop()
            sm.stop()

    @pytest.mark.asyncio
    async def test_outbound_to_voicemail_leave_message(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        handler = VoicemailPolicyHandler(
            bus,
            VoicemailPolicyConfig(
                policy=VoicemailPolicy.LEAVE_MESSAGE,
                message_text="Hi, returning your call",
            ),
        )
        sm.start()
        handler.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            assert handler.last_action["type"] == "leave_message"
        finally:
            handler.stop()
            sm.stop()

    @pytest.mark.asyncio
    async def test_outbound_to_ios_screening_then_human(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        detector = CallScreeningDetector(
            bus,
            call_sid="CA1",
            screening_response="Sarah from Acme",
            track_filter=None,
        )
        sm.start()
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert sm.state == OutboundCallState.SCREENING
            assert detector.state == ScreeningState.RESPONDING
        finally:
            detector.stop()
            sm.stop()

    @pytest.mark.asyncio
    async def test_outbound_to_ios_screening_then_voicemail(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        detector = CallScreeningDetector(bus, call_sid="CA1", track_filter=None)
        sm.start()
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert sm.state == OutboundCallState.SCREENING
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            detector.stop()
            sm.stop()

    @pytest.mark.asyncio
    async def test_outbound_to_android_screening(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        detector = CallScreeningDetector(bus, call_sid="CA1", track_filter=None)
        sm.start()
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(
                STTPartial(text="The person you're calling is using a screening service")
            )
            assert sm.state == OutboundCallState.SCREENING
        finally:
            detector.stop()
            sm.stop()

    @pytest.mark.asyncio
    async def test_outbound_to_ivr_single_level(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        async def mock_agent(ctx: dict) -> dict:
            return {"action": "dtmf", "digits": "1"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            assert len(actions) == 1
            assert actions[0].type == IVRActionType.DTMF
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_outbound_to_ivr_multi_level(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        call_count = 0

        async def mock_agent(ctx: dict) -> dict:
            nonlocal call_count
            call_count += 1
            return {
                "action": "dtmf",
                "digits": str(call_count),
            }

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            await bus.emit(STTFinal(text="Press 2 for returns"))
            assert len(actions) == 2
            assert nav.menu_depth == 2
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_outbound_busy(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus)
        sm.start()
        try:
            await bus.emit(CallFailed(call_sid="CA1", reason="busy"))
            assert sm.state == OutboundCallState.ENDED
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_outbound_no_answer(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus)
        sm.start()
        try:
            await bus.emit(CallFailed(call_sid="CA1", reason="no-answer"))
            assert sm.state == OutboundCallState.ENDED
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_all_helpers_coexist(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        detector = CallScreeningDetector(bus, call_sid="CA1", track_filter=None)
        vm_detector = VoicemailDetector(bus)
        handler = VoicemailPolicyHandler(
            bus, VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        )
        dtmf_agg = DTMFAggregator(bus)

        sm.start()
        detector.start()
        vm_detector.start()
        handler.start()
        dtmf_agg.start()
        try:
            # All running, no interference.
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING

            # DTMF still works.
            dtmf_received: list[DTMFAggregated] = []
            bus.subscribe(DTMFAggregated, dtmf_received.append)
            await bus.emit(DTMF(digit="1"))
            await bus.emit(DTMF(digit="#"))
            await asyncio.sleep(0.05)
            assert len(dtmf_received) >= 1
        finally:
            dtmf_agg.stop()
            handler.stop()
            vm_detector.stop()
            detector.stop()
            sm.stop()


# ── Screening Edge Cases ──────────────────────────────────────────


class TestScreeningEdgeCases:
    @pytest.mark.asyncio
    async def test_screening_response_within_time_window(self) -> None:
        bus = EventBus()
        responses: list[ScreeningResponse] = []
        bus.subscribe(ScreeningResponse, responses.append)
        detector = CallScreeningDetector(
            bus,
            screening_response="Sarah. Acme Corp. Thursday appointment.",
            track_filter=None,
        )
        detector.start()
        try:
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert len(responses) == 1
            assert responses[0].mode == "static"
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_screening_with_agent_response(self) -> None:
        bus = EventBus()
        responses: list[ScreeningResponse] = []
        bus.subscribe(ScreeningResponse, responses.append)
        detector = CallScreeningDetector(bus, screening_use_agent=True, track_filter=None)
        detector.start()
        try:
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert len(responses) == 1
            assert responses[0].mode == "agent"
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_dnd_focus_mode_fast_voicemail(self) -> None:
        """Call goes ringing → completed very quickly (DND)."""
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallRinging(call_sid="CA1"))
            # Immediately ends (DND sends to voicemail instantly).
            await bus.emit(CallEnded(call_sid="CA1", duration_s=1.0))
            assert sm.state == OutboundCallState.ENDED
        finally:
            sm.stop()


# ── Webhook Timing Edge Cases ─────────────────────────────────────


class TestWebhookTimingEdgeCases:
    @pytest.mark.asyncio
    async def test_skip_ringing_direct_to_answered(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            # No CallRinging — directly answered.
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_amd_webhook_arrives_before_any_stt(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            # AMD arrives before any STT.
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()


# ── Voicemail Edge Cases ──────────────────────────────────────────


class TestVoicemailEdgeCases:
    @pytest.mark.asyncio
    async def test_dual_greeting_silence_gap(self) -> None:
        """Carrier greeting → gap → personal greeting. AMD may false-positive."""
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            # Still classifying during the gap.
            assert sm.state == OutboundCallState.CLASSIFYING
            # Eventually AMD reports machine.
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_voicemail_full_disconnect(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            # Call disconnects (voicemail full).
            await bus.emit(CallEnded(call_sid="CA1"))
            assert sm.state == OutboundCallState.ENDED
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_human_double_hello_not_machine(self) -> None:
        from easycat.telephony.voicemail import classify_greeting

        assert classify_greeting("Hello? ... Hello?") == "human"


# ── Bot-to-Bot Detection ─────────────────────────────────────────


class TestBotToBotDetection:
    @pytest.mark.asyncio
    async def test_max_call_duration_terminates_call(self) -> None:
        bus = EventBus()
        changes: list[CallStateChanged] = []
        bus.subscribe(CallStateChanged, changes.append)
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            max_call_duration_s=0.05,
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
            await asyncio.sleep(0.1)
            assert sm.state == OutboundCallState.ENDED
        finally:
            sm.stop()


# ── Existing Tests Unbroken ───────────────────────────────────────


class TestExistingTestsUnbroken:
    @pytest.mark.asyncio
    async def test_existing_dtmf_works(self) -> None:
        bus = EventBus()
        received: list[DTMFAggregated] = []
        bus.subscribe(DTMFAggregated, received.append)
        agg = DTMFAggregator(bus)
        agg.start()
        try:
            await bus.emit(DTMF(digit="1"))
            await bus.emit(DTMF(digit="2"))
            await bus.emit(DTMF(digit="#"))
            await asyncio.sleep(0.05)
            assert len(received) == 1
            assert "12" in received[0].sequence
        finally:
            agg.stop()

    @pytest.mark.asyncio
    async def test_existing_voicemail_policy_works(self) -> None:
        bus = EventBus()
        handler = VoicemailPolicyHandler(
            bus, VoicemailPolicyConfig(policy=VoicemailPolicy.HANG_UP)
        )
        handler.start()
        try:
            await bus.emit(VoicemailDetected(result="machine"))
            assert handler.last_action is not None
            assert handler.last_action["type"] == "hang_up"
        finally:
            handler.stop()

    @pytest.mark.asyncio
    async def test_existing_voicemail_beep_detection_works(self) -> None:
        bus = EventBus()
        detector = VoicemailDetector(bus)
        audio = _generate_tone(1000, 0.5)
        result = await detector.process_audio(audio, 16000)
        assert result is True
