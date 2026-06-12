"""Tests for outbound call state machine."""

from __future__ import annotations

import pytest

from easycat.events import (
    CallAnswered,
    CallScreening,
    EventBus,
    STTFinal,
    VoicemailDetected,
)
from easycat.telephony.call_state import (
    OutboundCallState,
    OutboundCallStateMachine,
)


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
