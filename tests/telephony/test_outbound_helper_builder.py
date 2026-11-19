"""Boundary tests for outbound helper graph construction."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from easycat._dtmf_tones import DTMFToneConfig
from easycat.config import OutboundCallConfig, _outbound_helpers
from easycat.config._outbound_helpers import (
    _IVRCallbackCoordinator,
    build_outbound_helpers,
    resolve_ivr_dtmf_delivery,
)
from easycat.config.easy import VoicemailDetectionConfig
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
from easycat.telephony.ivr import DTMFDelivery, InBandDTMFDelivery, IVRNavigator
from easycat.telephony.outbound import OutboundCallManager, TelnyxOutboundClient
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


def _build_telnyx_manager(voicemail_detection: VoicemailDetectionConfig) -> OutboundCallManager:
    built = build_outbound_helpers(
        EventBus(),
        OutboundCallConfig(
            from_number="+15550001111",
            provider="telnyx",
            telnyx_api_key="KEY_x",
            telnyx_connection_id="conn-1",
            voicemail_detection=voicemail_detection,
        ),
        manager_cls=OutboundCallManager,
    )
    return next(helper for helper in built.helpers if isinstance(helper, OutboundCallManager))


def _telnyx_dial_payload(voicemail_detection: VoicemailDetectionConfig) -> dict[str, Any]:
    manager = _build_telnyx_manager(voicemail_detection)
    assert isinstance(manager._client, TelnyxOutboundClient)
    dials: list[dict[str, Any]] = []

    async def fake_dial(self: TelnyxOutboundClient, payload: dict[str, Any]) -> dict[str, Any]:
        dials.append(payload)
        return {"data": {"call_control_id": "CC-1"}}

    with patch.object(TelnyxOutboundClient, "dial", fake_dial):
        manager._client.calls.create(**manager._build_create_kwargs("+15550002222"))
    assert len(dials) == 1
    return dials[0]


@pytest.mark.parametrize("mode", ["detect", "detect_end_of_greeting"])
def test_telnyx_manager_honors_configured_voicemail_detection_mode(mode: str) -> None:
    """Telnyx outbound dials carry the configured AMD mode.

    The Telnyx branch of the builder used to drop ``voicemail_detection``,
    so the manager kept its ``DetectMessageEnd`` default and every Telnyx
    dial asked for ``greeting_end`` even when ``mode="detect"`` was set.
    """
    voicemail_detection = VoicemailDetectionConfig(mode=mode, detection_timeout_s=10)

    payload = _telnyx_dial_payload(voicemail_detection)

    assert (
        payload["answering_machine_detection"]
        == voicemail_detection.to_telnyx_params()["answering_machine_detection"]
    )


def test_telnyx_manager_receives_voicemail_detection_tuning() -> None:
    """Timeout and threshold tuning reach the Telnyx-backed manager too."""
    manager = _build_telnyx_manager(
        VoicemailDetectionConfig(mode="detect", detection_timeout_s=10, speech_threshold_ms=1800)
    )

    assert manager._amd_mode == "Enable"
    assert manager._amd_timeout == 10
    assert manager._speech_threshold == 1800


def test_telnyx_manager_default_voicemail_detection_waits_for_greeting_end() -> None:
    """The default policy keeps the greeting-end posture on Telnyx."""
    payload = _telnyx_dial_payload(VoicemailDetectionConfig())

    assert payload["answering_machine_detection"] == "greeting_end"


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


# ── IVR DTMF delivery resolution (ivr_dtmf_mode) ──────────────────────────


async def _agent_presses_one(_context: dict[str, object]) -> dict[str, str]:
    return {"action": "dtmf", "digits": "1"}


@pytest.mark.asyncio
async def test_ivr_callback_coordinator_names_a_non_awaitable_send_speech() -> None:
    bus = EventBus(handler_error_policy="raise")
    state_machine = Mock(state=OutboundCallState.IVR, call_sid="CA123")
    delivery = Mock(call_sid="")
    delivery.send_speech = Mock(return_value=True)
    coordinator = _IVRCallbackCoordinator(
        bus,
        state_machine,  # type: ignore[arg-type]
        Mock(),  # type: ignore[arg-type]
        delivery,  # type: ignore[arg-type]
    )
    coordinator.start()
    try:
        with pytest.raises(TypeError, match=r"send_speech\(\) must be async"):
            await bus.emit(IVRAction(type=IVRActionType.SPEAK, text="one moment"))
    finally:
        coordinator.stop()


class _CustomSender:
    async def send_dtmf_with_retry(self, digits: str, *, should_continue: Any = None) -> bool:
        return True

    async def send_speech(self, text: str) -> bool:
        return True


def _ivr_config(**overrides: Any) -> OutboundCallConfig:
    values: dict[str, Any] = {
        "from_number": "+15550000002",
        "ivr_agent_callback": _agent_presses_one,
    }
    values.update(overrides)
    return OutboundCallConfig(**values)


def _build(config: OutboundCallConfig) -> tuple[Any, IVRNavigator, _IVRCallbackCoordinator]:
    built = build_outbound_helpers(EventBus(), config, manager_cls=OutboundCallManager)
    navigator = next(helper for helper in built.helpers if isinstance(helper, IVRNavigator))
    coordinator = next(
        helper for helper in built.helpers if isinstance(helper, _IVRCallbackCoordinator)
    )
    return built, navigator, coordinator


def test_auto_mode_without_a_delivery_stays_emit_only() -> None:
    built, navigator, coordinator = _build(_ivr_config())

    assert built.ivr_delivery is None
    assert navigator._dtmf_delivery is None
    assert coordinator._delivery is None


def test_no_ivr_callback_builds_no_delivery() -> None:
    built = build_outbound_helpers(
        EventBus(),
        OutboundCallConfig(from_number="+15550000002", ivr_dtmf_mode="in_band"),
        manager_cls=OutboundCallManager,
    )

    assert built.ivr_delivery is None


def test_auto_mode_keeps_a_custom_sender() -> None:
    sender = _CustomSender()
    built, navigator, coordinator = _build(_ivr_config(ivr_dtmf_delivery=sender))

    assert built.ivr_delivery is sender
    assert navigator._dtmf_delivery is sender
    assert coordinator._delivery is sender


def test_auto_mode_swaps_stock_twilio_delivery_for_per_session_in_band(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_outbound_helpers, "_logged_notices", set())
    tones = DTMFToneConfig(tone_ms=120)
    shared = DTMFDelivery(twilio_client=Mock(), call_sid="")
    config = _ivr_config(ivr_dtmf_delivery=shared, ivr_dtmf_tones=tones)

    with caplog.at_level(logging.INFO, logger="easycat.config"):
        first, first_nav, first_coordinator = _build(config)
        second, _nav, _coordinator = _build(config)

    assert isinstance(first.ivr_delivery, InBandDTMFDelivery)
    assert isinstance(second.ivr_delivery, InBandDTMFDelivery)
    assert first.ivr_delivery is not second.ivr_delivery
    assert first.ivr_delivery._tone_config is tones
    assert first_nav._dtmf_delivery is first.ivr_delivery
    assert first_coordinator._delivery is first.ivr_delivery
    notices = [m for m in caplog.messages if "ivr_dtmf_mode='rest'" in m]
    assert len(notices) == 1


@pytest.mark.parametrize("delivery", [None, "stock"])
def test_in_band_mode_builds_a_fresh_in_band_delivery(delivery: str | None) -> None:
    configured = DTMFDelivery() if delivery == "stock" else None
    built, navigator, _coordinator = _build(
        _ivr_config(ivr_dtmf_mode="in_band", ivr_dtmf_delivery=configured)
    )

    assert isinstance(built.ivr_delivery, InBandDTMFDelivery)
    assert built.ivr_delivery is not configured
    assert navigator._dtmf_delivery is built.ivr_delivery


def test_rest_mode_keeps_the_users_delivery() -> None:
    shared = DTMFDelivery()
    built, navigator, coordinator = _build(
        _ivr_config(ivr_dtmf_mode="rest", ivr_dtmf_delivery=shared)
    )

    assert built.ivr_delivery is shared
    assert navigator._dtmf_delivery is shared
    assert coordinator._delivery is shared


def test_telnyx_auto_mode_leaves_delivery_unchanged() -> None:
    shared = DTMFDelivery()
    built, navigator, _coordinator = _build(
        _ivr_config(
            provider="telnyx",
            telnyx_api_key="key",
            telnyx_connection_id="conn",
            ivr_dtmf_delivery=shared,
        )
    )

    assert built.ivr_delivery is shared
    assert navigator._dtmf_delivery is shared


@pytest.mark.asyncio
async def test_in_band_mode_never_mutates_the_users_call_sid() -> None:
    bus = EventBus(handler_error_policy="raise")
    shared = DTMFDelivery(call_sid="")
    built = build_outbound_helpers(
        bus, _ivr_config(ivr_dtmf_delivery=shared), manager_cls=OutboundCallManager
    )
    _start_helpers(built.helpers)
    try:
        await bus.emit(CallInitiated(call_sid="CA1", to="+15550000001", from_="+15550000002"))
    finally:
        _stop_helpers(built.helpers)

    assert isinstance(built.ivr_delivery, InBandDTMFDelivery)
    assert shared.call_sid == ""


@pytest.mark.asyncio
async def test_rest_mode_still_receives_the_call_sid() -> None:
    bus = EventBus(handler_error_policy="raise")
    shared = DTMFDelivery(call_sid="")
    built = build_outbound_helpers(
        bus,
        _ivr_config(ivr_dtmf_mode="rest", ivr_dtmf_delivery=shared),
        manager_cls=OutboundCallManager,
    )
    _start_helpers(built.helpers)
    try:
        await bus.emit(CallInitiated(call_sid="CA1", to="+15550000001", from_="+15550000002"))
    finally:
        _stop_helpers(built.helpers)

    assert shared.call_sid == "CA1"


@pytest.mark.asyncio
async def test_custom_sender_without_call_sid_is_not_mutated() -> None:
    bus = EventBus(handler_error_policy="raise")
    sender = _CustomSender()
    built = build_outbound_helpers(
        bus, _ivr_config(ivr_dtmf_delivery=sender), manager_cls=OutboundCallManager
    )
    _start_helpers(built.helpers)
    try:
        await bus.emit(CallInitiated(call_sid="CA1", to="+15550000001", from_="+15550000002"))
    finally:
        _stop_helpers(built.helpers)

    assert not hasattr(sender, "call_sid")


class _OverridingDTMFDelivery(DTMFDelivery):
    """A REST delivery subclass with its own retry policy."""

    async def send_dtmf_with_retry(
        self, digits: str, *, should_continue: Any = None
    ) -> bool:  # pragma: no cover - identity is what the tests check
        return True


def test_auto_mode_keeps_a_dtmf_delivery_subclass() -> None:
    sender = _OverridingDTMFDelivery()
    built, navigator, coordinator = _build(_ivr_config(ivr_dtmf_delivery=sender))

    assert built.ivr_delivery is sender
    assert navigator._dtmf_delivery is sender
    assert coordinator._delivery is sender


def test_in_band_mode_still_replaces_a_dtmf_delivery_subclass() -> None:
    sender = _OverridingDTMFDelivery()
    built, _navigator, _coordinator = _build(
        _ivr_config(ivr_dtmf_mode="in_band", ivr_dtmf_delivery=sender)
    )

    assert isinstance(built.ivr_delivery, InBandDTMFDelivery)


class _ReadOnlySidSender(_CustomSender):
    """A custom sender that tracks the call itself and exposes a read-only SID."""

    @property
    def call_sid(self) -> str:
        return "CA-own"


@pytest.mark.asyncio
async def test_custom_sender_with_read_only_call_sid_still_resets_the_navigator() -> None:
    bus = EventBus(handler_error_policy="raise")
    sender = _ReadOnlySidSender()
    built = build_outbound_helpers(
        bus, _ivr_config(ivr_dtmf_delivery=sender), manager_cls=OutboundCallManager
    )
    navigator = next(h for h in built.helpers if isinstance(h, IVRNavigator))
    _start_helpers(built.helpers)
    try:
        with patch.object(navigator, "reset_for_call", wraps=navigator.reset_for_call) as reset:
            await bus.emit(CallInitiated(call_sid="CA1", to="+15550000001", from_="+15550000002"))
    finally:
        _stop_helpers(built.helpers)

    assert built.ivr_delivery is sender
    assert sender.call_sid == "CA-own"
    reset.assert_called_once_with()


def test_resolution_matrix_is_exposed_for_reuse() -> None:
    assert resolve_ivr_dtmf_delivery(_ivr_config()) is None


class _AllowListDelivery(InBandDTMFDelivery):
    """An in-band sender whose override must survive the per-session copy."""

    async def send_dtmf_with_retry(self, digits: str, *, should_continue: Any = None) -> bool:
        if digits != "1":
            return False
        return await super().send_dtmf_with_retry(digits, should_continue=should_continue)


@pytest.mark.parametrize("mode", ["auto", "in_band"])
def test_in_band_template_subclass_is_copied_per_session_with_its_class(mode: str) -> None:
    template = _AllowListDelivery()
    config = _ivr_config(ivr_dtmf_delivery=template, ivr_dtmf_mode=mode)

    first = resolve_ivr_dtmf_delivery(config)
    second = resolve_ivr_dtmf_delivery(config)

    assert type(first) is _AllowListDelivery and type(second) is _AllowListDelivery
    assert first is not template and second is not template and first is not second
