"""Tests for outbound call state machine."""

from __future__ import annotations

import asyncio

import pytest

from easycat.events import (
    CallAnswered,
    CallEnded,
    CallFailed,
    CallInitiated,
    CallRinging,
    CallScreening,
    EventBus,
    ScreeningTimedOut,
    STTFinal,
    VoicemailDetected,
)
from easycat.telephony.call_state import (
    TERMINAL_CLASSIFICATION_STATES,
    CallStateChanged,
    OutboundCallState,
    OutboundCallStateMachine,
)
from easycat.telephony.voicemail import STTAMDFusionClassifier


class TestOutboundCallStates:
    def test_all_states_exist(self) -> None:
        expected = {
            "INITIATING",
            "RINGING",
            "CLASSIFYING",
            "HUMAN",
            "SCREENING",
            "VOICEMAIL",
            "IVR",
            "UNKNOWN",
            "ENDED",
        }
        actual = {s.name for s in OutboundCallState}
        assert expected == actual

    def test_state_is_terminal(self) -> None:
        for state in (
            OutboundCallState.HUMAN,
            OutboundCallState.VOICEMAIL,
            OutboundCallState.IVR,
            OutboundCallState.UNKNOWN,
            OutboundCallState.ENDED,
        ):
            assert state in TERMINAL_CLASSIFICATION_STATES


class TestOutboundCallStateMachine:
    @pytest.mark.asyncio
    async def test_initial_state(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus)
        assert sm.state == OutboundCallState.INITIATING

    @pytest.mark.asyncio
    async def test_initiated_to_ringing(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus)
        sm.start()
        try:
            await bus.emit(CallRinging(call_sid="CA1"))
            assert sm.state == OutboundCallState.RINGING
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_ringing_to_answered(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallRinging(call_sid="CA1"))
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_duplicate_initiated_status_does_not_reset_active_call(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallInitiated(call_sid="CA1", to="+15551234567", from_="+15557654321"))
            await bus.emit(CallRinging(call_sid="CA1"))
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING

            await bus.emit(CallInitiated(call_sid="CA1", to="+15551234567", from_="+15557654321"))

            assert sm.state == OutboundCallState.CLASSIFYING
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_stale_failure_does_not_end_active_call(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallInitiated(call_sid="CA1", to="+15551234567", from_="+15557654321"))
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING

            await bus.emit(CallFailed(call_sid="CA2", reason="busy"))

            assert sm.state == OutboundCallState.CLASSIFYING
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_stale_voicemail_classification_is_ignored(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallInitiated(call_sid="CA1", to="+15551234567", from_="+15557654321"))
            await bus.emit(CallAnswered(call_sid="CA1"))

            await bus.emit(VoicemailDetected(result="machine", call_sid="CA2"))

            assert sm.state == OutboundCallState.CLASSIFYING
            await bus.emit(VoicemailDetected(result="machine", call_sid="CA1"))
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_stale_amd_does_not_fuse_into_active_call(self) -> None:
        bus = EventBus()
        classifier = STTAMDFusionClassifier(bus, stt_timeout_s=0.01)
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            expect_fused_voicemail=True,
        )
        classifier.start()
        sm.start()
        try:
            await bus.emit(CallInitiated(call_sid="CA1", to="+15551234567", from_="+15557654321"))
            await bus.emit(CallAnswered(call_sid="CA1"))

            await bus.emit(VoicemailDetected(result="machine", call_sid="CA2"))
            await asyncio.sleep(0.05)
            assert sm.state == OutboundCallState.CLASSIFYING

            await bus.emit(VoicemailDetected(result="machine", call_sid="CA1"))
            await asyncio.sleep(0.05)
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            classifier.stop()
            sm.stop()

    @pytest.mark.asyncio
    async def test_stale_screening_event_is_ignored(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallInitiated(call_sid="CA1", to="+15551234567", from_="+15557654321"))
            await bus.emit(CallAnswered(call_sid="CA1"))

            await bus.emit(CallScreening(call_sid="CA2", platform="ios"))

            assert sm.state == OutboundCallState.CLASSIFYING
            await bus.emit(CallScreening(call_sid="CA1", platform="ios"))
            assert sm.state == OutboundCallState.SCREENING
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_stale_screening_timeout_is_ignored(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallInitiated(call_sid="CA1", to="+15551234567", from_="+15557654321"))
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(CallScreening(call_sid="CA1", platform="ios"))

            await bus.emit(ScreeningTimedOut(call_sid="CA2"))

            assert sm.state == OutboundCallState.SCREENING
            await bus.emit(ScreeningTimedOut(call_sid="CA1"))
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_ringing_to_failed(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus)
        sm.start()
        try:
            await bus.emit(CallRinging(call_sid="CA1"))
            await bus.emit(CallFailed(call_sid="CA1", reason="busy"))
            assert sm.state == OutboundCallState.ENDED
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_initiating_direct_to_answered(self) -> None:
        """Some carriers skip ring-back signaling."""
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_classify_human_from_amd(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_classify_voicemail_from_amd(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_classify_screening(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(CallScreening(call_sid="CA1", platform="ios"))
            assert sm.state == OutboundCallState.SCREENING
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_screening_to_human(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(CallScreening(call_sid="CA1", platform="ios"))
            assert sm.state == OutboundCallState.SCREENING
            await bus.emit(STTFinal(text="Hello, how can I help you?"))
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_classifying_to_human_via_stt(self) -> None:
        """Conversational STTFinal during CLASSIFYING transitions to HUMAN."""
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
            await bus.emit(STTFinal(text="Hello?"))
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_classifying_stays_on_long_non_conversational_stt(self) -> None:
        """Long non-conversational text during CLASSIFYING does not transition."""
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
            await bus.emit(
                STTFinal(text="Please leave a message after the tone and we will get back to you")
            )
            assert sm.state == OutboundCallState.CLASSIFYING
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_screening_to_voicemail(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(CallScreening(call_sid="CA1", platform="ios"))
            assert sm.state == OutboundCallState.SCREENING
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_screening_to_declined(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(CallScreening(call_sid="CA1", platform="ios"))
            await bus.emit(CallEnded(call_sid="CA1"))
            assert sm.state == OutboundCallState.ENDED
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_classify_timeout_to_unknown(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=0.05)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
            await asyncio.sleep(0.3)
            assert sm.state == OutboundCallState.UNKNOWN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_unknown_fallback_lets_agent_handle(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=0.05)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await asyncio.sleep(0.3)
            assert sm.state == OutboundCallState.UNKNOWN
            # UNKNOWN is a terminal classification; normal pipeline runs.
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_call_ended_from_any_state(self) -> None:
        for pre_state in (
            OutboundCallState.INITIATING,
            OutboundCallState.RINGING,
            OutboundCallState.CLASSIFYING,
            OutboundCallState.SCREENING,
            OutboundCallState.HUMAN,
        ):
            bus = EventBus()
            sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
            sm._state = pre_state  # Force state for testing
            sm.start()
            try:
                await bus.emit(CallEnded(call_sid="CA1"))
                assert sm.state == OutboundCallState.ENDED
            finally:
                sm.stop()

    @pytest.mark.asyncio
    async def test_state_change_emits_event(self) -> None:
        bus = EventBus()
        changes: list[CallStateChanged] = []
        bus.subscribe(CallStateChanged, changes.append)
        sm = OutboundCallStateMachine(bus, call_sid="CA1", classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallRinging(call_sid="CA1"))
            assert len(changes) == 1
            assert changes[0].old == OutboundCallState.INITIATING
            assert changes[0].new == OutboundCallState.RINGING
            assert changes[0].call_sid == "CA1"
        finally:
            sm.stop()

    def test_start_stop_lifecycle(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus)
        sm.start()
        assert sm._started is True
        sm.stop()
        assert sm._started is False

    def test_idempotent_start_stop(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus)
        sm.start()
        sm.start()
        assert sm._started is True
        sm.stop()
        sm.stop()
        assert sm._started is False

    @pytest.mark.asyncio
    async def test_max_call_duration_enforced(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60, max_call_duration_s=0.05)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
            await asyncio.sleep(0.3)
            assert sm.state == OutboundCallState.ENDED
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_max_call_duration_timer_cancelled_on_call_end(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60, max_call_duration_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(CallEnded(call_sid="CA1"))
            # Timer should be cancelled; verify no error after sleep.
            assert sm._max_duration_task is None or sm._max_duration_task.cancelled()
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_sip_607_608_maps_to_ended(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus)
        sm.start()
        try:
            await bus.emit(CallFailed(call_sid="CA1", reason="blocked_unwanted", sip_code=607))
            assert sm.state == OutboundCallState.ENDED
        finally:
            sm.stop()


class TestCallStateMachineTimeBounds:
    @pytest.mark.asyncio
    async def test_classification_timeout_configurable(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=0.05)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await asyncio.sleep(0.3)
            assert sm.state == OutboundCallState.UNKNOWN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_short_timeout_fast_fallback(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=0.01)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await asyncio.sleep(0.1)
            assert sm.state == OutboundCallState.UNKNOWN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_timeout_cancels_on_classification(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=0.1)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
            await asyncio.sleep(0.15)
            # Should still be HUMAN, not UNKNOWN.
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()
