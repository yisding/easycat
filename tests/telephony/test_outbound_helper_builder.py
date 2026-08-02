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
    CallAnswered,
    CallEnded,
    CallInitiated,
    CallStateChanged,
    EventBus,
    IVRAction,
    IVRActionType,
    STTFinal,
    STTPartial,
    VoicemailDetected,
)
from easycat.telephony.call_state import OutboundCallState
from easycat.telephony.ivr import IVRNavigator
from easycat.telephony.outbound import OutboundCallManager
from easycat.telephony.screening import CallScreeningDetector, ScreeningState
from easycat.telephony.voicemail import (
    PostScreeningVoicemailDetector,
    STTAMDFusionClassifier,
)


def _start_helpers(helpers: tuple[object, ...]) -> None:
    for helper in helpers:
        start = getattr(helper, "start", None)
        if start is not None:
            start()


def _stop_helpers(helpers: tuple[object, ...]) -> None:
    for helper in reversed(helpers):
        stop = getattr(helper, "stop", None)
        if stop is not None:
            stop()


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
async def test_active_call_fusion_ignores_duplicate_and_overlapping_initiations() -> None:
    bus = EventBus(handler_error_policy="raise")
    built = build_outbound_helpers(
        bus,
        OutboundCallConfig(from_number="+15550000002"),
        manager_cls=OutboundCallManager,
    )
    fusion = next(helper for helper in built.helpers if isinstance(helper, STTAMDFusionClassifier))
    fused: list[VoicemailDetected] = []

    def capture(event: VoicemailDetected) -> None:
        if event.source == "fusion":
            fused.append(event)

    bus.subscribe(VoicemailDetected, capture)
    _start_helpers(built.helpers)
    try:
        await bus.emit(CallInitiated(call_sid="CA-A", to="+15550000001", from_="+15550000002"))
        await bus.emit(CallAnswered(call_sid="CA-A"))
        await bus.emit(VoicemailDetected(result="machine", call_sid="CA-A"))

        await bus.emit(CallInitiated(call_sid="CA-A", to="+15550000001", from_="+15550000002"))
        await bus.emit(CallInitiated(call_sid="CA-B", to="+15550000003", from_="+15550000002"))

        assert built.state_machine.call_sid == "CA-A"
        assert built.state_machine.state == OutboundCallState.CLASSIFYING
        assert fusion.amd_result == "machine"

        await bus.emit(
            STTFinal(text="You've reached Alex, please leave a message", track="inbound")
        )

        assert built.state_machine.state == OutboundCallState.VOICEMAIL
        assert [(event.call_sid, event.result) for event in fused] == [("CA-A", "machine")]

        await bus.emit(CallEnded(call_sid="CA-A"))
        await bus.emit(CallInitiated(call_sid="CA-B", to="+15550000003", from_="+15550000002"))

        assert built.state_machine.call_sid == "CA-B"
        assert built.state_machine.state == OutboundCallState.INITIATING
        assert fusion._call_sid == "CA-B"
        assert fusion.amd_result is None
    finally:
        _stop_helpers(built.helpers)


@pytest.mark.asyncio
async def test_active_screening_survives_stale_initiation_and_keeps_call_sid() -> None:
    bus = EventBus(handler_error_policy="raise")
    built = build_outbound_helpers(
        bus,
        OutboundCallConfig(from_number="+15550000002", screening_response=""),
        manager_cls=OutboundCallManager,
    )
    screening = next(
        helper for helper in built.helpers if isinstance(helper, CallScreeningDetector)
    )
    post_screening = next(
        helper for helper in built.helpers if isinstance(helper, PostScreeningVoicemailDetector)
    )
    fused: list[VoicemailDetected] = []

    def capture(event: VoicemailDetected) -> None:
        if event.source == "fusion":
            fused.append(event)

    bus.subscribe(VoicemailDetected, capture)
    _start_helpers(built.helpers)
    try:
        await bus.emit(CallInitiated(call_sid="CA-A", to="+15550000001", from_="+15550000002"))
        await bus.emit(CallAnswered(call_sid="CA-A"))
        await bus.emit(
            STTPartial(
                text="Please record your name and reason for calling",
                track="inbound",
            )
        )

        assert built.state_machine.state == OutboundCallState.SCREENING
        assert screening.state == ScreeningState.SCREENING_DETECTED
        assert post_screening.active is True

        await bus.emit(CallInitiated(call_sid="CA-A", to="+15550000001", from_="+15550000002"))
        await bus.emit(CallInitiated(call_sid="CA-B", to="+15550000003", from_="+15550000002"))

        assert built.state_machine.call_sid == "CA-A"
        assert screening.state == ScreeningState.SCREENING_DETECTED
        assert post_screening.active is True

        await bus.emit(STTFinal(text="Please leave a message after the tone", track="inbound"))

        assert built.state_machine.state == OutboundCallState.VOICEMAIL
        assert [(event.call_sid, event.result) for event in fused] == [("CA-A", "machine")]
    finally:
        _stop_helpers(built.helpers)


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
