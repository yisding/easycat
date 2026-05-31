"""Tests for session integration with outbound calling features.

Verifies that create_session() wires outbound helpers correctly and that
the helpers respond to events within the session lifecycle.
"""

from __future__ import annotations

import asyncio

import pytest

from easycat.config import OutboundCallConfig, TelephonyConfig
from easycat.events import (
    CallAnswered,
    CallEnded,
    CallRinging,
    CallScreening,
    EventBus,
    STTFinal,
    STTPartial,
    VoicemailDetected,
)
from easycat.telephony.call_state import (
    CallStateChanged,
    ClassificationGate,
    OutboundCallState,
    OutboundCallStateMachine,
)
from easycat.telephony.ivr import IVRAction, IVRActionType, IVRNavigator
from easycat.telephony.screening import CallScreeningDetector
from easycat.telephony.voicemail import VoicemailPolicyHandler

# ── Helper factory tests ──────────────────────────────────────────


class TestOutboundSessionCreation:
    def test_create_session_with_outbound_config(self) -> None:
        """create_telephony_helpers with outbound config creates all helpers."""
        from easycat.config import _create_telephony_helpers

        async def _dummy_agent(ctx: dict) -> dict:
            return {"action": "wait"}

        bus = EventBus()
        config = TelephonyConfig(
            enable_outbound_call_manager=True,
            outbound=OutboundCallConfig(
                from_number="+15551234567",
                ivr_agent_callback=_dummy_agent,
            ),
        )
        helpers = _create_telephony_helpers(bus, config).helpers
        types = [type(h).__name__ for h in helpers]
        assert "OutboundCallStateMachine" in types
        assert "CallScreeningDetector" in types
        assert "IVRNavigator" in types
        assert "VoicemailPolicyHandler" in types

    def test_create_session_without_ivr_agent_skips_navigator(self) -> None:
        """Without ivr_agent_callback, IVRNavigator is not created."""
        from easycat.config import _create_telephony_helpers

        bus = EventBus()
        config = TelephonyConfig(
            enable_outbound_call_manager=True,
            outbound=OutboundCallConfig(from_number="+15551234567"),
        )
        helpers = _create_telephony_helpers(bus, config).helpers
        types = [type(h).__name__ for h in helpers]
        assert "IVRNavigator" not in types

    def test_create_session_without_outbound(self) -> None:
        """Without outbound config, no outbound helpers created."""
        from easycat.config import _create_telephony_helpers

        bus = EventBus()
        config = TelephonyConfig(enable_dtmf_aggregator=True)
        helpers = _create_telephony_helpers(bus, config).helpers
        types = [type(h).__name__ for h in helpers]
        assert "OutboundCallStateMachine" not in types
        assert "DTMFAggregator" in types

    def test_outbound_helpers_started_on_session_start(self) -> None:
        """start() on all helpers doesn't error."""
        from easycat.config import _create_telephony_helpers

        bus = EventBus()
        config = TelephonyConfig(
            enable_outbound_call_manager=True,
            outbound=OutboundCallConfig(from_number="+15551234567"),
        )
        helpers = _create_telephony_helpers(bus, config).helpers
        for h in helpers:
            h.start()
        for h in helpers:
            assert h._started is True
        for h in helpers:
            h.stop()

    def test_outbound_helpers_stopped_on_session_stop(self) -> None:
        """stop() on all helpers cleans up."""
        from easycat.config import _create_telephony_helpers

        bus = EventBus()
        config = TelephonyConfig(
            enable_outbound_call_manager=True,
            outbound=OutboundCallConfig(from_number="+15551234567"),
        )
        helpers = _create_telephony_helpers(bus, config).helpers
        for h in helpers:
            h.start()
        for h in helpers:
            h.stop()
        for h in helpers:
            assert h._started is False

    def test_outbound_manager_accessible(self) -> None:
        """OutboundCallStateMachine is accessible from helpers list."""
        from easycat.config import _create_telephony_helpers

        bus = EventBus()
        config = TelephonyConfig(
            enable_outbound_call_manager=True,
            outbound=OutboundCallConfig(from_number="+15551234567"),
        )
        helpers = _create_telephony_helpers(bus, config).helpers
        sm_list = [h for h in helpers if isinstance(h, OutboundCallStateMachine)]
        assert len(sm_list) == 1


# ── Pipeline flow tests ──────────────────────────────────────────


class TestOutboundSessionPipeline:
    @pytest.mark.asyncio
    async def test_outbound_stt_events_reach_screening_detector(self) -> None:
        """STTPartial events reach CallScreeningDetector through shared bus."""
        from easycat.config import _create_telephony_helpers

        bus = EventBus()
        config = TelephonyConfig(
            enable_outbound_call_manager=True,
            outbound=OutboundCallConfig(
                from_number="+1555",
                enable_screening_detection=True,
            ),
        )
        helpers = _create_telephony_helpers(bus, config).helpers
        for h in helpers:
            h.start()

        screening_events: list[CallScreening] = []
        bus.subscribe(CallScreening, screening_events.append)

        try:
            # Simulate call answered.
            await bus.emit(CallAnswered(call_sid="CA1"))
            # STTPartial reaches the screening detector.
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert len(screening_events) == 1
        finally:
            for h in helpers:
                h.stop()

    @pytest.mark.asyncio
    async def test_outbound_stt_events_reach_ivr_navigator(self) -> None:
        """STTFinal events reach IVRNavigator when activated."""
        from easycat.config import _create_telephony_helpers

        received_prompts: list[str] = []

        async def _mock_agent(ctx: dict) -> dict:
            received_prompts.append(ctx["prompt"])
            return {"action": "wait"}

        bus = EventBus()
        config = TelephonyConfig(
            enable_outbound_call_manager=True,
            outbound=OutboundCallConfig(
                from_number="+1555",
                ivr_agent_callback=_mock_agent,
            ),
        )
        helpers = _create_telephony_helpers(bus, config).helpers
        for h in helpers:
            h.start()

        # Find the IVR navigator and activate it.
        nav = next(h for h in helpers if isinstance(h, IVRNavigator))
        nav.activate()

        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            assert nav._active is True
            assert received_prompts == ["Press 1 for sales"]
        finally:
            for h in helpers:
                h.stop()

    @pytest.mark.asyncio
    async def test_classification_gate_intercepts_tts(self) -> None:
        """Classification gate buffers TTS during CLASSIFYING state."""
        from easycat.audio_format import AudioChunk, AudioFormat
        from easycat.events import TTSAudio

        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            classification_gate=True,
            classification_gate_timeout_s=5.0,
        )
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
            assert sm.gate.is_buffering

            # TTS audio should be buffered.
            chunk = AudioChunk(
                data=b"\x00" * 100,
                format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
            )
            await bus.emit(TTSAudio(chunk=chunk))
            assert len(sm.gate.buffer) == 1

            # Classify as human — gate releases.
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
            assert not sm.gate.is_buffering
        finally:
            sm.stop()


# ── State reaction tests ──────────────────────────────────────────


class TestOutboundSessionStateReactions:
    @pytest.mark.asyncio
    async def test_human_state_enables_normal_pipeline(self) -> None:
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
    async def test_voicemail_state_triggers_policy(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        handler = VoicemailPolicyHandler(bus)
        sm.start()
        handler.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            assert handler._action_taken is True
        finally:
            handler.stop()
            sm.stop()

    @pytest.mark.asyncio
    async def test_ivr_state_activates_navigator(self) -> None:
        """IVR state should be manually activated by session layer."""
        bus = EventBus()
        nav = IVRNavigator(bus)
        nav.start()
        try:
            assert not nav._active
            nav.activate()
            assert nav._active
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_screening_state_triggers_response(self) -> None:
        from easycat.telephony.screening import ScreeningResponse

        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        det = CallScreeningDetector(
            bus,
            call_sid="CA1",
            screening_response="Sarah from Acme",
            track_filter=None,
        )
        responses: list[ScreeningResponse] = []
        bus.subscribe(ScreeningResponse, responses.append)
        sm.start()
        det.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert sm.state == OutboundCallState.SCREENING
            assert len(responses) == 1
            assert responses[0].mode == "static"
        finally:
            det.stop()
            sm.stop()

    @pytest.mark.asyncio
    async def test_ended_state_cleans_up_session(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        sm.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(CallEnded(call_sid="CA1"))
            assert sm.state == OutboundCallState.ENDED
        finally:
            sm.stop()


# ── Full call flow tests ──────────────────────────────────────────


class TestOutboundCallFlow:
    @pytest.mark.asyncio
    async def test_place_call_and_converse(self) -> None:
        """Simulate: answered → classified as human → conversation works."""
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        changes: list[CallStateChanged] = []
        bus.subscribe(CallStateChanged, changes.append)
        sm.start()
        try:
            await bus.emit(CallRinging(call_sid="CA1"))
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="human"))
            assert sm.state == OutboundCallState.HUMAN
            states = [c.new for c in changes]
            assert OutboundCallState.RINGING in states
            assert OutboundCallState.CLASSIFYING in states
            assert OutboundCallState.HUMAN in states
        finally:
            sm.stop()

    @pytest.mark.asyncio
    async def test_place_call_to_voicemail_with_message(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(bus, classification_timeout_s=60)
        from easycat.telephony.voicemail import VoicemailPolicy, VoicemailPolicyConfig

        handler = VoicemailPolicyHandler(
            bus,
            VoicemailPolicyConfig(
                policy=VoicemailPolicy.LEAVE_MESSAGE,
                message_text="Returning your call",
            ),
        )
        sm.start()
        handler.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            assert handler.last_action is not None
            assert handler.last_action["type"] == "leave_message"
            assert handler.last_action["message_text"] == "Returning your call"
        finally:
            handler.stop()
            sm.stop()

    @pytest.mark.asyncio
    async def test_place_call_through_ivr(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        async def mock_agent(ctx: dict) -> dict:
            return {"action": "dtmf", "digits": "2"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 2 for billing"))
            assert len(actions) >= 1
            assert actions[0].type == IVRActionType.DTMF
            assert actions[0].digits == "2"
        finally:
            nav.stop()


# ── Classification gate module tests ──────────────────────────────


class TestClassificationGateModule:
    @pytest.mark.asyncio
    async def test_gate_buffers_tts_audio_frames(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat
        from easycat.events import TTSAudio

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0)
        gate.start()
        try:
            gate.close()
            chunk = AudioChunk(
                data=b"\x00" * 100,
                format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
            )
            await bus.emit(TTSAudio(chunk=chunk))
            assert len(gate.buffer) == 1
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_release_flushes_buffer(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat
        from easycat.events import TTSAudio

        bus = EventBus()
        flushed: list[list] = []
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0, on_flush=flushed.append)
        gate.start()
        try:
            gate.close()
            chunk = AudioChunk(
                data=b"\x00" * 100,
                format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
            )
            await bus.emit(TTSAudio(chunk=chunk))
            released = gate.release()
            assert len(released) == 1
            assert len(flushed) == 1
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_transparent_when_open(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat
        from easycat.events import TTSAudio

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0)
        gate.start()
        try:
            # Gate not closed — audio passes through.
            chunk = AudioChunk(
                data=b"\x00" * 100,
                format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
            )
            await bus.emit(TTSAudio(chunk=chunk))
            assert len(gate.buffer) == 0
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_hold_audio_plays_during_buffer(self) -> None:
        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0, hold_audio="hold.wav")
        gate.start()
        try:
            gate.close()
            assert gate._hold_audio_playing
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_auto_releases_on_timeout(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat
        from easycat.events import TTSAudio

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=0.05)
        gate.start()
        try:
            gate.close()
            chunk = AudioChunk(
                data=b"\x00" * 100,
                format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
            )
            await bus.emit(TTSAudio(chunk=chunk))
            await asyncio.sleep(0.3)
            assert not gate.is_buffering
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_only_active_during_classifying(self) -> None:
        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            classification_gate=True,
        )
        sm.start()
        try:
            # Gate not closed before call is answered.
            assert not sm.gate.is_buffering
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
            assert sm.gate.is_buffering
            # Classify — gate opens.
            await bus.emit(VoicemailDetected(result="human"))
            assert not sm.gate.is_buffering
        finally:
            sm.stop()
