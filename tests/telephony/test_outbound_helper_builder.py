"""Boundary tests for outbound helper graph construction."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, call

import pytest

from easycat.config import OutboundCallConfig
from easycat.config._outbound_helpers import (
    _IVRCallbackCoordinator,
    build_outbound_helpers,
)
from easycat.events import (
    CallInitiated,
    CallStateChanged,
    EventBus,
    IVRAction,
    IVRActionType,
)
from easycat.telephony.call_state import OutboundCallState
from easycat.telephony.outbound import OutboundCallManager


def test_builder_preserves_default_helper_order_and_shared_patterns() -> None:
    """Ordering-sensitive listeners and classifiers remain deterministic."""
    built = build_outbound_helpers(
        EventBus(),
        OutboundCallConfig(from_number="+15551234567", callee_language="es"),
        manager_cls=OutboundCallManager,
    )

    assert tuple(type(helper).__name__ for helper in built.helpers) == (
        "STTAMDFusionClassifier",
        "PostScreeningVoicemailDetector",
        "CallDispositionTracker",
        "OutboundCallStateMachine",
        "CallScreeningDetector",
        "VoicemailPolicyHandler",
        "NumberHealthMonitor",
    )
    assert built.state_machine is built.helpers[3]
    assert built.screening_detector is built.helpers[4]
    assert built.state_machine._screening_patterns is built.screening_detector._patterns


def test_builder_omits_disabled_optional_helpers() -> None:
    built = build_outbound_helpers(
        EventBus(),
        OutboundCallConfig(
            from_number="+15551234567",
            enable_screening_detection=False,
            enable_disposition_tracker=False,
            enable_number_health=False,
        ),
        manager_cls=OutboundCallManager,
    )

    assert tuple(type(helper).__name__ for helper in built.helpers) == (
        "STTAMDFusionClassifier",
        "PostScreeningVoicemailDetector",
        "OutboundCallStateMachine",
        "VoicemailPolicyHandler",
    )
    assert built.screening_detector is None


@pytest.mark.asyncio
async def test_ivr_callback_coordinator_owns_event_transitions() -> None:
    bus = EventBus(handler_error_policy="raise")
    state_machine = Mock(state=OutboundCallState.IVR)
    state_machine.transition = AsyncMock()
    navigator = Mock()
    delivery = Mock(call_sid="")
    delivery.send_speech = AsyncMock()
    coordinator = _IVRCallbackCoordinator(
        bus,
        state_machine,  # type: ignore[arg-type]
        navigator,  # type: ignore[arg-type]
        delivery,  # type: ignore[arg-type]
    )
    coordinator.connect()

    await bus.emit(CallInitiated(call_sid="CA123", to="+15550000001", from_="+15550000002"))
    await bus.emit(CallStateChanged(old=OutboundCallState.CLASSIFYING, new=OutboundCallState.IVR))
    await bus.emit(IVRAction(type=IVRActionType.HUMAN_DETECTED))
    await bus.emit(IVRAction(type=IVRActionType.HANGUP))
    await bus.emit(IVRAction(type=IVRActionType.SPEAK, text="one moment"))
    await bus.emit(CallStateChanged(old=OutboundCallState.IVR, new=OutboundCallState.HUMAN))

    assert delivery.call_sid == "CA123"
    navigator.activate.assert_called_once_with()
    navigator.deactivate.assert_called_once_with()
    assert state_machine.transition.await_args_list == [
        call(OutboundCallState.HUMAN),
        call(OutboundCallState.ENDED),
    ]
    delivery.send_speech.assert_awaited_once_with("one moment")
