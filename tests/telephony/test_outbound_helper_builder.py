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
    STTFinal,
)
from easycat.telephony.call_state import OutboundCallState
from easycat.telephony.ivr import IVRNavigator
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
    state_machine = Mock(state=OutboundCallState.IVR, call_sid="CA123")
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
    coordinator.start()

    await bus.emit(CallInitiated(call_sid="CA123", to="+15550000001", from_="+15550000002"))
    await bus.emit(CallInitiated(call_sid="CA123", to="+15550000001", from_="+15550000002"))
    await bus.emit(CallInitiated(call_sid="CA-stale", to="+15550000001", from_="+15550000002"))
    await bus.emit(CallStateChanged(old=OutboundCallState.CLASSIFYING, new=OutboundCallState.IVR))
    await bus.emit(IVRAction(type=IVRActionType.HUMAN_DETECTED))
    await bus.emit(IVRAction(type=IVRActionType.HANGUP))
    await bus.emit(IVRAction(type=IVRActionType.SPEAK, text="one moment"))
    await bus.emit(CallStateChanged(old=OutboundCallState.IVR, new=OutboundCallState.HUMAN))

    assert delivery.call_sid == "CA123"
    navigator.reset_for_call.assert_called_once_with()
    navigator.activate.assert_called_once_with()
    navigator.deactivate.assert_called_once_with()
    assert state_machine.transition.await_args_list == [
        call(OutboundCallState.HUMAN),
        call(OutboundCallState.ENDED),
    ]
    delivery.send_speech.assert_awaited_once_with("one moment")

    coordinator.stop()
    state_machine.call_sid = "CA456"
    await bus.emit(CallInitiated(call_sid="CA456", to="+15550000003", from_="+15550000002"))
    navigator.reset_for_call.assert_called_once_with()


@pytest.mark.asyncio
async def test_ivr_call_boundary_resets_navigation_without_dtmf_delivery() -> None:
    bus = EventBus(handler_error_policy="raise")
    contexts: list[dict[str, object]] = []

    async def agent(context: dict[str, object]) -> dict[str, str]:
        contexts.append(context)
        return {"action": "dtmf", "digits": "1"}

    built = build_outbound_helpers(
        bus,
        OutboundCallConfig(
            from_number="+15550000002",
            ivr_agent_callback=agent,
            ivr_dtmf_delivery=None,
        ),
        manager_cls=OutboundCallManager,
    )
    navigator = next(helper for helper in built.helpers if isinstance(helper, IVRNavigator))
    coordinator = next(
        helper for helper in built.helpers if isinstance(helper, _IVRCallbackCoordinator)
    )
    built.state_machine.start()
    navigator.start()
    coordinator.start()
    try:
        await bus.emit(CallInitiated(call_sid="CA1", to="+15550000001", from_="+15550000002"))
        navigator.activate()
        await bus.emit(STTFinal(text="Press 1 for sales"))
        navigator.notify_silence(11.0)

        assert navigator.menu_depth == 1
        assert len(navigator.history) == 1
        assert navigator.in_hold is True

        # Repeated activation within one call must retain its traversal state.
        navigator.activate()
        assert navigator.menu_depth == 1
        assert len(navigator.history) == 1
        assert navigator.in_hold is True

        await built.state_machine.transition(OutboundCallState.ENDED)
        await bus.emit(CallInitiated(call_sid="CA2", to="+15550000003", from_="+15550000002"))

        assert navigator.menu_depth == 0
        assert navigator.history == []
        assert navigator.in_hold is False

        navigator.activate()
        await bus.emit(STTFinal(text="Press 1 for support"))
        assert contexts[1]["menu_depth"] == 0
        assert contexts[1]["history"] == []
    finally:
        coordinator.stop()
        navigator.stop()
        built.state_machine.stop()
