"""Tests for outbound call state machine."""

from __future__ import annotations

import asyncio

import pytest

from easycat.events import (
    CallAnswered,
    CallEnded,
    EventBus,
    STTFinal,
    VoicemailDetected,
)
from easycat.telephony.call_state import (
    CallStateChanged,
    OutboundCallState,
    OutboundCallStateMachine,
)


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
            await bus.emit(STTFinal(text="Hello?", track="inbound"))
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
            await bus.emit(STTFinal(text="Please leave a message after the beep", track="inbound"))
            assert sm.state == OutboundCallState.VOICEMAIL
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_missing_track_stt_ignored_during_voicemail(self) -> None:
        """Untracked STTFinal events do not trigger VOICEMAIL → HUMAN."""
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

    @pytest.mark.parametrize("track", ["inbound", "inbound_track", "caller", "INBOUND"])
    @pytest.mark.asyncio
    async def test_whitelisted_inbound_tracks_trigger_pickup(self, track: str) -> None:
        """Every trusted inbound-track label (case-insensitive) reaches pickup.

        Regression: the early-return filter once hard-coded ``"inbound"`` so
        ``"inbound_track"``/``"caller"`` were dropped before reaching the
        voicemail-pickup block even though the trusted whitelist accepts them.
        """
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus, classification_timeout_s=60, voicemail_pickup_window_s=5.0
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            await bus.emit(STTFinal(text="Hello?", track=track))
            assert sm.state == OutboundCallState.HUMAN
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
            await bus.emit(STTFinal(text="Hello?", track="inbound"))
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
            # Gate fully opens after discard: the opener is dropped but later
            # TTS (e.g. a leave-message drop, or agent speech on pickup) must
            # reach the transport rather than buffer with no timeout to flush.
            assert not sm.gate.is_buffering
            await bus.emit(STTFinal(text="Hello?", track="inbound"))
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
            await bus.emit(STTFinal(text="Yeah?", track="inbound"))
            assert sm.state == OutboundCallState.HUMAN
        finally:
            sm.stop()
