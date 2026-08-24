"""Construction boundary for outbound telephony runtime helpers.

This module is imported only when outbound calling is enabled. Keeping the
runtime imports here preserves the cheap ``EasyConfig`` import path while
making helper ordering and callback ownership explicit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from easycat.events import CallInitiated, CallStateChanged, EventBus
from easycat.telephony.call_state import OutboundCallState, OutboundCallStateMachine
from easycat.telephony.compliance import DNCStore
from easycat.telephony.ivr import DTMFDelivery, IVRAction, IVRActionType, IVRNavigator
from easycat.telephony.number_health import CallDispositionTracker, NumberHealthMonitor
from easycat.telephony.outbound import OutboundCallManager
from easycat.telephony.retry import RetryStrategy
from easycat.telephony.screening import (
    CallScreeningDetector,
    ScreeningPatternSet,
    screening_patterns_for_languages,
)
from easycat.telephony.voicemail import (
    PostScreeningVoicemailDetector,
    STTAMDFusionClassifier,
    VoicemailPolicyHandler,
)

from .easy import OutboundCallConfig

logger = logging.getLogger("easycat.config")


@dataclass(frozen=True, slots=True)
class BuiltOutboundHelpers:
    """Outbound helpers plus the collaborators needed by session wiring."""

    helpers: tuple[Any, ...]
    state_machine: OutboundCallStateMachine
    screening_detector: CallScreeningDetector | None


class _IVRCallbackCoordinator:
    """Own the event callbacks connecting an IVR navigator to call state."""

    def __init__(
        self,
        event_bus: EventBus,
        state_machine: OutboundCallStateMachine,
        navigator: IVRNavigator,
        delivery: DTMFDelivery | None,
    ) -> None:
        self._event_bus = event_bus
        self._state_machine = state_machine
        self._navigator = navigator
        self._delivery = delivery
        self._started = False
        self._accepted_call_sid = ""

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(CallInitiated, self._on_call_initiated)
        self._event_bus.subscribe(CallStateChanged, self._on_state_changed)
        self._event_bus.subscribe(IVRAction, self._on_action)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._event_bus.unsubscribe(CallInitiated, self._on_call_initiated)
        self._event_bus.unsubscribe(CallStateChanged, self._on_state_changed)
        self._event_bus.unsubscribe(IVRAction, self._on_action)
        self._started = False

    async def _on_call_initiated(self, event: CallInitiated) -> None:
        # The placement path and Twilio's initiated webhook can publish the
        # same call twice. The state machine subscribes before this helper and
        # is the authority that rejects stale or overlapping SIDs; mirror only
        # the call it accepted, and reset each accepted SID exactly once.
        if (
            not event.call_sid
            or event.call_sid != self._state_machine.call_sid
            or event.call_sid == self._accepted_call_sid
        ):
            return
        self._accepted_call_sid = event.call_sid
        self._navigator.reset_for_call()
        if self._delivery is not None:
            self._delivery.call_sid = event.call_sid

    def _on_state_changed(self, event: CallStateChanged) -> None:
        if event.new == OutboundCallState.IVR:
            self._navigator.activate()
        elif event.new in {OutboundCallState.HUMAN, OutboundCallState.ENDED}:
            self._navigator.deactivate()

    async def _on_action(self, event: IVRAction) -> None:
        if event.type == IVRActionType.HUMAN_DETECTED:
            await self._transition_from_ivr(OutboundCallState.HUMAN)
        elif event.type == IVRActionType.HANGUP:
            await self._transition_from_ivr(OutboundCallState.ENDED)
        elif event.type == IVRActionType.SPEAK and self._delivery is not None:
            await self._delivery.send_speech(event.text)

    async def _transition_from_ivr(self, target: OutboundCallState) -> None:
        if self._state_machine.state == OutboundCallState.IVR:
            await self._state_machine.transition(target)


class _OutboundHelperBuilder:
    """Assemble the ordered outbound helper graph for one event bus."""

    def __init__(
        self,
        event_bus: EventBus,
        config: OutboundCallConfig,
        manager_cls: type[OutboundCallManager],
        dnc_list: DNCStore | None,
    ) -> None:
        self._event_bus = event_bus
        self._config = config
        self._manager_cls = manager_cls
        self._dnc_list = dnc_list
        self._helpers: list[Any] = []

    def build(self) -> BuiltOutboundHelpers:
        patterns = self._screening_patterns()
        state_machine = self._build_state_machine(patterns)
        self._add_preclassification_helpers(state_machine)
        self._helpers.append(state_machine)
        screening_detector = self._add_screening_detector(patterns, state_machine)
        self._add_ivr(state_machine)
        self._add_policy_helpers(state_machine)
        manager = self._add_manager()
        if manager is not None:
            state_machine.set_max_duration_hangup(manager.hangup_owned_call)
        self._add_retry_strategy(manager)
        return BuiltOutboundHelpers(
            helpers=tuple(self._helpers),
            state_machine=state_machine,
            screening_detector=screening_detector,
        )

    def _add_preclassification_helpers(self, state_machine: OutboundCallStateMachine) -> None:
        # Fusion must run before the state machine, and disposition tracking
        # must record a failure reason before the terminal ENDED transition.
        self._helpers.append(
            STTAMDFusionClassifier(
                self._event_bus,
                call_boundary_acceptor=state_machine.accepts_call_initiation,
            )
        )
        self._helpers.append(
            PostScreeningVoicemailDetector(
                self._event_bus,
                call_boundary_acceptor=state_machine.accepts_call_initiation,
            )
        )
        if self._config.enable_disposition_tracker:
            self._helpers.append(CallDispositionTracker(self._event_bus))

    def _screening_patterns(self) -> ScreeningPatternSet:
        languages = ["en"]
        if self._config.callee_language and self._config.callee_language != "en":
            languages.append(self._config.callee_language)
        return screening_patterns_for_languages(languages)

    def _build_state_machine(self, patterns: ScreeningPatternSet) -> OutboundCallStateMachine:
        return OutboundCallStateMachine(
            self._event_bus,
            classification_timeout_s=float(self._config.voicemail_detection.detection_timeout_s),
            max_call_duration_s=self._config.max_call_duration_s,
            classification_gate=self._config.classification_gate,
            classification_gate_timeout_s=self._config.classification_gate_timeout_s,
            classification_gate_hold_audio=self._config.classification_gate_hold_audio,
            expect_fused_voicemail=True,
            late_voicemail_window_s=self._config.late_voicemail_window_s,
            voicemail_pickup_window_s=self._config.voicemail_pickup_window_s,
            screening_patterns=patterns,
        )

    def _add_screening_detector(
        self,
        patterns: ScreeningPatternSet,
        state_machine: OutboundCallStateMachine,
    ) -> CallScreeningDetector | None:
        if not self._config.enable_screening_detection:
            return None
        detector = CallScreeningDetector(
            self._event_bus,
            enabled=True,
            screening_response=self._config.screening_response,
            screening_use_agent=self._config.screening_use_agent,
            max_screening_turns=self._config.max_screening_turns,
            patterns=patterns,
            # The bot's own speech must not trigger a screening match when
            # the transport transcribes both call legs.
            track_filter="inbound",
            call_boundary_acceptor=state_machine.accepts_call_initiation,
        )
        self._helpers.append(detector)
        return detector

    def _add_ivr(self, state_machine: OutboundCallStateMachine) -> None:
        callback = self._config.ivr_agent_callback
        if callback is None:
            return
        navigator = IVRNavigator(
            self._event_bus,
            agent_callback=callback,
            dtmf_delivery=self._config.ivr_dtmf_delivery,
        )
        self._helpers.append(navigator)
        self._helpers.append(
            _IVRCallbackCoordinator(
                self._event_bus,
                state_machine,
                navigator,
                self._config.ivr_dtmf_delivery,
            )
        )

    def _add_policy_helpers(self, state_machine: OutboundCallStateMachine) -> None:
        self._helpers.append(
            VoicemailPolicyHandler(
                self._event_bus,
                expect_fused=True,
                call_boundary_acceptor=state_machine.accepts_call_initiation,
            )
        )
        if self._config.enable_number_health:
            self._helpers.append(NumberHealthMonitor(self._event_bus))

    def _add_manager(self) -> OutboundCallManager | None:
        if self._config.provider == "telnyx":
            if not (self._config.telnyx_api_key and self._config.telnyx_connection_id):
                logger.warning(
                    "OutboundCallManager enabled with provider='telnyx' but telnyx_api_key / "
                    "telnyx_connection_id are blank — outbound calling is disabled."
                )
                return None
            try:
                from easycat.telephony.outbound import TelnyxOutboundClient

                client = TelnyxOutboundClient(
                    self._config.telnyx_api_key,
                    connection_id=self._config.telnyx_connection_id,
                    webhook_url=self._config.telnyx_webhook_url,
                )
            except ImportError:
                logger.warning(
                    "cryptography/aiohttp missing — Telnyx OutboundCallManager disabled"
                )
                return None
            manager = self._manager_cls(
                self._event_bus,
                from_number=self._config.from_number,
                enable_realtime_transcription=self._config.enable_realtime_transcription,
                client=client,
            )
            manager.dnc_list = self._dnc_list
            self._helpers.append(manager)
            return manager
        if not (self._config.twilio_account_sid and self._config.twilio_auth_token):
            logger.warning(
                "OutboundCallManager enabled but twilio_account_sid / twilio_auth_token "
                "are blank — outbound calling is disabled. Set both credentials to place calls."
            )
            return None
        try:
            manager = self._manager_cls(
                self._event_bus,
                from_number=self._config.from_number,
                enable_realtime_transcription=self._config.enable_realtime_transcription,
                twilio_account_sid=self._config.twilio_account_sid,
                twilio_auth_token=self._config.twilio_auth_token,
                twiml_url=self._config.twiml_url,
                status_callback_url=self._config.status_callback_url,
                **self._config.voicemail_detection.to_twilio_params(),
            )
        except ImportError:
            logger.warning("twilio package not installed — OutboundCallManager disabled")
            return None
        manager.dnc_list = self._dnc_list
        self._helpers.append(manager)
        return manager

    def _add_retry_strategy(self, manager: OutboundCallManager | None) -> None:
        if self._config.enable_retry_strategy and manager is not None:
            manager.retry_strategy = RetryStrategy(self._config.retry_strategy)


def build_outbound_helpers(
    event_bus: EventBus,
    config: OutboundCallConfig,
    *,
    manager_cls: type[OutboundCallManager],
    dnc_list: DNCStore | None = None,
) -> BuiltOutboundHelpers:
    """Build the outbound helper graph without exposing mutable builder state."""
    return _OutboundHelperBuilder(event_bus, config, manager_cls, dnc_list).build()
