"""Tests for outbound call state machine."""

from __future__ import annotations

import asyncio

import pytest

from easycat.audio_format import AudioChunk, AudioFormat
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
    TTSAudio,
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
    async def test_fused_classifying_does_not_promote_short_stt_to_human(self) -> None:
        """Fusion-enabled classification waits for AMD/fusion instead of short STT."""
        bus = EventBus()
        flushed: list[list[TTSAudio]] = []
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            classification_gate=True,
            expect_fused_voicemail=True,
        )
        sm.set_gate_flush_callback(flushed.append)
        sm.start()
        fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
            assert sm.gate.is_buffering
            await bus.emit(TTSAudio(chunk=AudioChunk(data=b"\x00" * 100, format=fmt)))
            assert len(sm.gate.buffer) == 1

            await bus.emit(STTFinal(text="leave your message"))

            assert sm.state == OutboundCallState.CLASSIFYING
            assert sm.gate.is_buffering
            assert len(sm.gate.buffer) == 1
            assert flushed == []
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

    @pytest.mark.asyncio
    async def test_state_observer_failure_preserves_committed_answer_invariants(self) -> None:
        bus = EventBus(handler_error_policy="raise")
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            max_call_duration_s=60,
            classification_gate=True,
            smart_turn_suppress=True,
        )
        sm.start()

        async def fail_state_observer(_event: CallStateChanged) -> None:
            raise RuntimeError("observer failed")

        bus.subscribe(CallStateChanged, fail_state_observer)
        try:
            with pytest.raises(RuntimeError, match="observer failed"):
                await bus.emit(CallAnswered(call_sid="CA1"))

            assert sm.state == OutboundCallState.CLASSIFYING
            assert sm.smart_turn_suppressed is True
            assert sm.gate.is_buffering is True
            assert sm._timers.active("call_classification_timeout")
            assert sm._timers.active("call_max_duration")

            with pytest.raises(RuntimeError, match="observer failed"):
                await sm.transition(OutboundCallState.VOICEMAIL)

            assert sm.state == OutboundCallState.VOICEMAIL
            assert sm.gate.is_buffering is False
        finally:
            bus.unsubscribe(CallStateChanged, fail_state_observer)
            sm.stop()

    @pytest.mark.asyncio
    async def test_reentrant_state_observer_does_not_apply_stale_gate_transition(self) -> None:
        bus = EventBus(handler_error_policy="raise")
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            classification_gate=True,
        )
        flushed: list[list[TTSAudio]] = []

        async def flush(events: list[TTSAudio]) -> None:
            flushed.append(events)

        async def redirect_human_to_voicemail(event: CallStateChanged) -> None:
            if event.new == OutboundCallState.HUMAN:
                await sm.transition(OutboundCallState.VOICEMAIL)

        sm.set_gate_flush_callback(flush)
        sm.start()
        bus.subscribe(CallStateChanged, redirect_human_to_voicemail)
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            opener = TTSAudio(
                chunk=AudioChunk(
                    data=b"opener",
                    format=AudioFormat(sample_rate=16_000, channels=1, sample_width=2),
                )
            )
            await bus.emit(opener)

            await sm.transition(OutboundCallState.HUMAN)

            assert sm.state == OutboundCallState.VOICEMAIL
            assert not sm.gate.is_buffering
            assert all(opener not in batch for batch in flushed)
        finally:
            bus.unsubscribe(CallStateChanged, redirect_human_to_voicemail)
            sm.stop()

    @pytest.mark.asyncio
    async def test_child_task_state_observer_must_transition_directly(self) -> None:
        bus = EventBus(handler_error_policy="raise")
        sm = OutboundCallStateMachine(bus)

        async def redirect_in_child(event: CallStateChanged) -> None:
            if event.new == OutboundCallState.HUMAN:
                await asyncio.gather(sm.transition(OutboundCallState.VOICEMAIL))

        bus.subscribe(CallStateChanged, redirect_in_child)
        try:
            with pytest.raises(RuntimeError, match="must await transition.*directly"):
                await asyncio.wait_for(sm.transition(OutboundCallState.HUMAN), timeout=1)
            assert sm.state == OutboundCallState.HUMAN
        finally:
            bus.unsubscribe(CallStateChanged, redirect_in_child)

    @pytest.mark.asyncio
    async def test_fire_and_forget_observer_transition_cannot_race_settlement(self) -> None:
        bus = EventBus(handler_error_policy="raise")
        sm = OutboundCallStateMachine(bus)
        children: list[asyncio.Task[None]] = []

        async def spawn_redirect(event: CallStateChanged) -> None:
            if event.new == OutboundCallState.HUMAN:
                children.append(asyncio.create_task(sm.transition(OutboundCallState.VOICEMAIL)))
                await asyncio.sleep(0)

        bus.subscribe(CallStateChanged, spawn_redirect)
        try:
            await sm.transition(OutboundCallState.HUMAN)

            assert sm.state == OutboundCallState.HUMAN
            assert len(children) == 1
            with pytest.raises(RuntimeError, match="must await transition.*directly"):
                await children[0]
        finally:
            bus.unsubscribe(CallStateChanged, spawn_redirect)

    @pytest.mark.asyncio
    async def test_observer_spawned_task_may_transition_after_settlement(self) -> None:
        """A stale inherited transition context must not reject later transitions."""
        bus = EventBus(handler_error_policy="raise")
        sm = OutboundCallStateMachine(bus)
        release = asyncio.Event()
        children: list[asyncio.Task[None]] = []

        async def follow_up() -> None:
            await release.wait()
            await sm.transition(OutboundCallState.VOICEMAIL)

        async def spawn_follow_up(event: CallStateChanged) -> None:
            if event.new == OutboundCallState.HUMAN:
                # The child copies the active transition context but first
                # runs only after the owning transition has settled.
                children.append(asyncio.create_task(follow_up()))

        bus.subscribe(CallStateChanged, spawn_follow_up)
        try:
            await sm.transition(OutboundCallState.HUMAN)
            assert sm.state == OutboundCallState.HUMAN
            assert len(children) == 1

            release.set()
            await asyncio.wait_for(children[0], timeout=1)
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            release.set()
            bus.unsubscribe(CallStateChanged, spawn_follow_up)

    @pytest.mark.asyncio
    async def test_unrelated_concurrent_transition_waits_for_active_observer(self) -> None:
        bus = EventBus(handler_error_policy="raise")
        sm = OutboundCallStateMachine(bus)
        observer_started = asyncio.Event()
        release_observer = asyncio.Event()

        async def block_human_observer(event: CallStateChanged) -> None:
            if event.new == OutboundCallState.HUMAN:
                observer_started.set()
                await release_observer.wait()

        bus.subscribe(CallStateChanged, block_human_observer)
        human_task = asyncio.create_task(sm.transition(OutboundCallState.HUMAN))
        try:
            await asyncio.wait_for(observer_started.wait(), timeout=1)
            voicemail_task = asyncio.create_task(sm.transition(OutboundCallState.VOICEMAIL))
            await asyncio.sleep(0)

            assert not voicemail_task.done()
            assert sm.state == OutboundCallState.HUMAN

            release_observer.set()
            await asyncio.wait_for(asyncio.gather(human_task, voicemail_task), timeout=1)
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            release_observer.set()
            await asyncio.gather(human_task, return_exceptions=True)
            bus.unsubscribe(CallStateChanged, block_human_observer)

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
    async def test_max_duration_callback_failure_still_ends_and_emits(self) -> None:
        bus = EventBus()
        ended: list[CallEnded] = []
        ended_event = asyncio.Event()

        async def capture(event: CallEnded) -> None:
            ended.append(event)
            ended_event.set()

        async def failing_hangup(_call_sid: str) -> None:
            raise RuntimeError("Twilio unavailable")

        bus.subscribe(CallEnded, capture)
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            max_call_duration_s=0.01,
        )
        sm.set_max_duration_hangup(failing_hangup)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await asyncio.wait_for(ended_event.wait(), timeout=0.2)

            assert sm.state == OutboundCallState.ENDED
            assert len(ended) == 1
            assert ended[0].call_sid == "CA1"
            assert ended[0].disposition == "max_duration"
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_max_duration_hangup_precedes_terminal_transition_handlers(self) -> None:
        bus = EventBus()
        hangup_started = asyncio.Event()
        terminal_handler_started = asyncio.Event()
        release_terminal_handler = asyncio.Event()

        async def hangup(_call_sid: str) -> None:
            hangup_started.set()

        async def slow_terminal_handler(event: CallStateChanged) -> None:
            if event.new == OutboundCallState.ENDED:
                terminal_handler_started.set()
                await release_terminal_handler.wait()

        bus.subscribe(CallStateChanged, slow_terminal_handler)
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            max_call_duration_s=0.01,
        )
        sm.set_max_duration_hangup(hangup)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await asyncio.wait_for(terminal_handler_started.wait(), timeout=0.2)
            assert hangup_started.is_set()
        finally:
            release_terminal_handler.set()
            sm.stop()

    @pytest.mark.asyncio
    async def test_max_call_duration_timer_cancelled_on_call_end(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60, max_call_duration_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(CallEnded(call_sid="CA1"))
            assert not sm._timers.active("call_max_duration")
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_every_owned_timer(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            max_call_duration_s=60,
            late_voicemail_window_s=60,
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))

            assert not sm._timers.empty

            sm.stop()

            assert sm._timers.empty
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
