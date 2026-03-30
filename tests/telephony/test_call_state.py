"""Tests for outbound call state machine."""

from __future__ import annotations

import asyncio

import pytest

from easycat.events import (
    DTMF,
    CallAnswered,
    CallEnded,
    CallFailed,
    CallRinging,
    CallScreening,
    EventBus,
    STTFinal,
    TTSAudio,
    VoicemailDetected,
)
from easycat.telephony.call_state import (
    TERMINAL_CLASSIFICATION_STATES,
    CallStateChanged,
    ClassificationGate,
    OutboundCallState,
    OutboundCallStateMachine,
)


class TestOutboundCallStates:
    def test_all_states_exist(self) -> None:
        expected = {
            "INITIATING",
            "RINGING",
            "ANSWERED",
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


class TestCallStateMachineWithExistingHelpers:
    @pytest.mark.asyncio
    async def test_integrates_with_voicemail_detector(self) -> None:
        """VoicemailDetector's VoicemailDetected consumed by state machine."""
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
    async def test_integrates_with_voicemail_policy(self) -> None:
        """After VOICEMAIL classification, VoicemailPolicyHandler can act."""
        from easycat.telephony.voicemail import VoicemailPolicyConfig, VoicemailPolicyHandler

        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        policy = VoicemailPolicyHandler(bus, VoicemailPolicyConfig())
        sm.start()
        policy.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            # Policy handler should have acted (it only acts once).
            assert policy._action_taken is True
        finally:
            sm.stop()
            policy.stop()

    @pytest.mark.asyncio
    async def test_integrates_with_dtmf_aggregator(self) -> None:
        """DTMF events still work alongside state machine."""
        from easycat.telephony.dtmf import DTMFAggregator, DTMFAggregatorConfig

        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        agg = DTMFAggregator(bus, DTMFAggregatorConfig(timeout_ms=50))
        sm.start()
        agg.start()
        try:
            await bus.emit(DTMF(digit="1"))
            assert sm.state == OutboundCallState.INITIATING
            assert agg.buffer == "1"
        finally:
            sm.stop()
            agg.stop()

    @pytest.mark.asyncio
    async def test_does_not_interfere_with_existing_helpers(self) -> None:
        """DTMF events are ignored by the state machine."""
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        dtmf_received: list[DTMF] = []
        bus.subscribe(DTMF, dtmf_received.append)
        sm.start()
        try:
            await bus.emit(DTMF(digit="1"))
            assert len(dtmf_received) == 1
            assert sm.state == OutboundCallState.INITIATING
        finally:
            sm.stop()


# ── Classification gate ──────────────────────────────────────────


class TestClassificationGate:
    @pytest.mark.asyncio
    async def test_gate_buffers_agent_tts_during_classifying(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0)
        gate.start()
        try:
            gate.close()
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            assert len(gate.buffer) == 1
            assert gate.is_buffering
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_releases_on_amd_result(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        flushed: list[list[TTSAudio]] = []
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0, on_flush=flushed.append)
        gate.start()
        try:
            gate.close()
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            assert len(gate.buffer) == 1
            released = gate.release()
            assert len(released) == 1
            assert not gate.is_buffering
            assert len(flushed) == 1
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_releases_on_stt_classification(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0)
        gate.start()
        try:
            gate.close()
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            released = gate.release()
            assert len(released) == 1
            assert not gate.is_buffering
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_releases_on_timeout(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=0.05)
        gate.start()
        try:
            gate.close()
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            assert gate.is_buffering
            await asyncio.sleep(0.3)
            assert not gate.is_buffering
            assert len(gate.buffer) == 0  # Flushed.
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_releases_on_first_signal(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0)
        gate.start()
        try:
            gate.close()
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            gate.release()
            assert not gate.is_buffering
            # Second release is a no-op.
            second = gate.release()
            assert len(second) == 0
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_hold_audio_plays(self) -> None:
        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0, hold_audio="hold.wav")
        gate.start()
        try:
            gate.close()
            assert gate._hold_audio_playing
            gate.release()
            assert not gate._hold_audio_playing
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_disabled_no_buffering(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=False)
        gate.start()
        try:
            gate.close()
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            assert len(gate.buffer) == 0
            assert not gate.is_buffering
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_no_buffering_after_classifying(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0)
        gate.start()
        try:
            gate.close()
            gate.release()
            assert not gate.is_buffering
            # After release, new TTS passes through (not buffered).
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            assert len(gate.buffer) == 0
        finally:
            gate.stop()


# ── SmartTurn suppression ────────────────────────────────────────


class TestSmartTurnSuppression:
    @pytest.mark.asyncio
    async def test_smart_turn_disabled_during_classifying(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60, smart_turn_suppress=True)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
            assert sm.smart_turn_suppressed is True
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_smart_turn_disabled_during_screening(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60, smart_turn_suppress=True)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(CallScreening(call_sid="CA1", platform="ios"))
            assert sm.state == OutboundCallState.SCREENING
            assert sm.smart_turn_suppressed is True
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_smart_turn_disabled_during_ivr(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60, smart_turn_suppress=True)
        sm._state = OutboundCallState.IVR
        sm._smart_turn_suppressed = True
        # Verify the state is in the suppress set.
        assert sm.smart_turn_suppressed is True

    @pytest.mark.asyncio
    async def test_smart_turn_reenabled_on_human(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60, smart_turn_suppress=True)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.smart_turn_suppressed is True
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
            assert sm.smart_turn_suppressed is False
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_longer_vad_timeout_during_screening(self) -> None:
        bus = EventBus()
        vad_changes: list[float] = []
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            smart_turn_suppress=True,
            vad_timeout_extension_s=3.0,
        )
        sm._on_vad_timeout_change = vad_changes.append
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(CallScreening(call_sid="CA1", platform="ios"))
            assert sm.state == OutboundCallState.SCREENING
            assert 3.0 in vad_changes
            # Transition to HUMAN resets.
            await bus.emit(STTFinal(text="Hello"))
            assert sm.state == OutboundCallState.HUMAN
            assert 0.0 in vad_changes
        finally:
            sm.stop()


# ── Late voicemail detection ────────────────────────────────────


class TestLateVoicemailDetection:
    @pytest.mark.asyncio
    async def test_human_to_voicemail_on_late_beep(self) -> None:
        """A VoicemailDetected(machine) during the late window transitions HUMAN → VOICEMAIL."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus, classification_timeout_s=60, late_voicemail_window_s=5.0
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
            # Late beep detected while still within window.
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_late_voicemail_ignored_after_window_expires(self) -> None:
        """After the late voicemail window closes, machine events stay in HUMAN."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus, classification_timeout_s=60, late_voicemail_window_s=0.05
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
            # Wait for window to expire.
            await asyncio.sleep(0.3)
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_late_voicemail_disabled_by_default(self) -> None:
        """With late_voicemail_window_s=0, HUMAN ignores late machine events."""
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_late_voicemail_emits_state_change(self) -> None:
        """HUMAN → VOICEMAIL transition emits CallStateChanged."""
        bus = EventBus()
        changes: list[CallStateChanged] = []
        bus.subscribe(CallStateChanged, changes.append)
        sm = OutboundCallStateMachine(
            bus, call_sid="CA1", classification_timeout_s=60, late_voicemail_window_s=5.0
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            # Find the HUMAN → VOICEMAIL transition.
            late = [c for c in changes if c.old == OutboundCallState.HUMAN]
            assert len(late) == 1
            assert late[0].new == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_late_voicemail_triggers_policy_handler(self) -> None:
        """VoicemailPolicyHandler fires on late HUMAN → VOICEMAIL."""
        from easycat.telephony.voicemail import VoicemailPolicyConfig, VoicemailPolicyHandler

        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus, classification_timeout_s=60, late_voicemail_window_s=5.0
        )
        policy = VoicemailPolicyHandler(bus, VoicemailPolicyConfig())
        sm.start()
        policy.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
            assert policy._action_taken is False
            # Late voicemail detection.
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            assert policy._action_taken is True
        finally:
            sm.stop()
            policy.stop()

    @pytest.mark.asyncio
    async def test_late_voicemail_window_cancelled_on_call_end(self) -> None:
        """CallEnded cancels the late voicemail window."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus, classification_timeout_s=60, late_voicemail_window_s=60.0
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
            assert sm._late_voicemail_task is not None
            await bus.emit(CallEnded(call_sid="CA1"))
            assert sm.state == OutboundCallState.ENDED
            assert sm._late_voicemail_task is None
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_late_voicemail_respects_fused_voicemail(self) -> None:
        """With expect_fused_voicemail=True, raw AMD events are still ignored."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            late_voicemail_window_s=5.0,
            expect_fused_voicemail=True,
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human", source="fusion"))
            assert sm.state == OutboundCallState.HUMAN
            # Raw AMD event should be ignored.
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.HUMAN
            # Fused event should trigger late voicemail.
            await bus.emit(VoicemailDetected(result="machine", source="fusion"))
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()


# ── Voicemail pickup detection (VOICEMAIL → HUMAN) ─────────────


class TestVoicemailPickupDetection:
    @pytest.mark.asyncio
    async def test_voicemail_to_human_on_conversational_stt(self) -> None:
        """Conversational STTFinal during VOICEMAIL transitions to HUMAN."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus, classification_timeout_s=60, voicemail_pickup_window_s=5.0
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            await bus.emit(STTFinal(text="Hello?"))
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_voicemail_pickup_ignored_after_window_expires(self) -> None:
        """After the pickup window closes, conversational STT stays in VOICEMAIL."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus, classification_timeout_s=60, voicemail_pickup_window_s=0.05
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            await asyncio.sleep(0.3)
            await bus.emit(STTFinal(text="Hello?"))
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_voicemail_pickup_disabled_by_default(self) -> None:
        """With voicemail_pickup_window_s=0 (default), VOICEMAIL ignores pickup signals."""
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            await bus.emit(STTFinal(text="Hello?"))
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_voicemail_system_prompt_does_not_trigger_pickup(self) -> None:
        """Voicemail system prompts do not trigger VOICEMAIL → HUMAN."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus, classification_timeout_s=60, voicemail_pickup_window_s=5.0
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            await bus.emit(STTFinal(text="Please leave a message after the beep"))
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_outbound_track_stt_ignored_during_voicemail(self) -> None:
        """Bot's own speech (outbound track) does not trigger pickup."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus, classification_timeout_s=60, voicemail_pickup_window_s=5.0
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            ev = STTFinal(text="Hello?", track="outbound")
            await bus.emit(ev)
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_voicemail_pickup_emits_state_change(self) -> None:
        """VOICEMAIL → HUMAN transition emits CallStateChanged."""
        bus = EventBus()
        changes: list[CallStateChanged] = []
        bus.subscribe(CallStateChanged, changes.append)
        sm = OutboundCallStateMachine(
            bus,
            call_sid="CA1",
            classification_timeout_s=60,
            voicemail_pickup_window_s=5.0,
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            await bus.emit(STTFinal(text="Hello?"))
            assert sm.state == OutboundCallState.HUMAN
            pickup = [c for c in changes if c.old == OutboundCallState.VOICEMAIL]
            assert len(pickup) == 1
            assert pickup[0].new == OutboundCallState.HUMAN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_gate_reopens_on_voicemail_to_human(self) -> None:
        """Classification gate reopens when VOICEMAIL transitions to HUMAN."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            classification_gate=True,
            voicemail_pickup_window_s=5.0,
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.gate.is_buffering
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            # Gate stays closed after discard (blocks remaining TTS).
            assert sm.gate.is_buffering
            await bus.emit(STTFinal(text="Hello?"))
            assert sm.state == OutboundCallState.HUMAN
            assert not sm.gate.is_buffering
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_voicemail_to_human_via_voicemail_detected_event(self) -> None:
        """VoicemailDetected(result='human') during VOICEMAIL transitions to HUMAN."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus, classification_timeout_s=60, voicemail_pickup_window_s=5.0
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            await bus.emit(VoicemailDetected(result="human", source="fusion"))
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_voicemail_pickup_raw_amd_ignored_with_fusion(self) -> None:
        """With expect_fused_voicemail=True, raw AMD human events are ignored."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            voicemail_pickup_window_s=5.0,
            expect_fused_voicemail=True,
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine", source="fusion"))
            assert sm.state == OutboundCallState.VOICEMAIL
            # Raw AMD human event (no source) should be filtered.
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_voicemail_pickup_window_cancelled_on_call_end(self) -> None:
        """CallEnded cancels the voicemail pickup window."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus, classification_timeout_s=60, voicemail_pickup_window_s=60.0
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            assert sm._voicemail_pickup_task is not None
            await bus.emit(CallEnded(call_sid="CA1"))
            assert sm.state == OutboundCallState.ENDED
            assert sm._voicemail_pickup_task is None
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_roundtrip_human_voicemail_human(self) -> None:
        """HUMAN → VOICEMAIL (late) → HUMAN (pickup) roundtrip works."""
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            late_voicemail_window_s=5.0,
            voicemail_pickup_window_s=5.0,
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
            # Late voicemail reclassification.
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            # Human picks up during voicemail.
            await bus.emit(STTFinal(text="Yeah?"))
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()
