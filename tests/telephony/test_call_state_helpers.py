"""Tests for outbound call state machine."""

from __future__ import annotations

import pytest

from easycat.events import (
    DTMF,
    CallAnswered,
    EventBus,
    VoicemailDetected,
)
from easycat.telephony.call_state import (
    OutboundCallState,
    OutboundCallStateMachine,
)


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
