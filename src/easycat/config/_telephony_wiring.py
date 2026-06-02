"""Outbound / telephony runtime wiring.

Imported LAZILY by :func:`easycat.config._factory.create_session` only when
``config.telephony`` is set, so a non-telephony session never loads the
outbound stack (state machines, IVR navigator, screening detector, …). The
heavy telephony runtime classes are likewise imported inside the functions
that build the pipeline, preserving the no-eager-telephony-import property
that :mod:`tests.test_public_api` guards.

:func:`create_telephony_helpers` returns a typed :class:`TelephonyHelpers`
bundle — the state machine and screening detector are populated directly as
the helpers are built, so ``create_session`` and :func:`wire_outbound_pipeline`
never re-scan a ``list[Any]`` with ``isinstance`` to recover them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from easycat.events import CallInitiated, CallScreening, CallStateChanged, EventBus, TTSAudio
from easycat.integrations.agents.base import NULL_RECORDER, AgentTurnInput
from easycat.session.actions import SessionActionExecutor

if TYPE_CHECKING:
    from easycat.session._session import Session
    from easycat.telephony.call_state import OutboundCallStateMachine
    from easycat.telephony.screening import CallScreeningDetector

    from .easy import OutboundCallConfig, TelephonyConfig

logger = logging.getLogger("easycat.config")


@dataclass
class TelephonyHelpers:
    """Typed result of :func:`create_telephony_helpers`.

    ``helpers`` is the full list the session starts/stops; ``state_machine``
    and ``screening_detector`` are the two members ``create_session`` and
    :func:`wire_outbound_pipeline` need by name, populated inline as the
    helpers are built (no ``isinstance`` re-scan).
    """

    helpers: list[Any] = field(default_factory=list)
    state_machine: OutboundCallStateMachine | None = None
    screening_detector: CallScreeningDetector | None = None


def create_telephony_helpers(
    event_bus: EventBus,
    config: TelephonyConfig | None,
    *,
    dnc_list: Any | None = None,
) -> TelephonyHelpers:
    result = TelephonyHelpers()
    if config is None:
        return result

    if config.enable_dtmf_aggregator:
        from easycat.telephony.dtmf import DTMFAggregator

        result.helpers.append(DTMFAggregator(event_bus, config.dtmf_aggregator))

    if config.enable_voicemail_detector:
        from easycat.telephony.voicemail import VoicemailDetector

        result.helpers.append(VoicemailDetector(event_bus, config.voicemail_detector))

    if config.enable_outbound_call_manager and config.outbound:
        _build_outbound_helpers(event_bus, config.outbound, result, dnc_list=dnc_list)

    return result


def create_action_executors(config: TelephonyConfig | None) -> list[SessionActionExecutor]:
    executors: list[SessionActionExecutor] = []
    if config is None:
        return executors
    if config.twilio_actions is not None:
        from easycat.telephony.session_actions import TwilioSessionActionExecutor

        executors.append(TwilioSessionActionExecutor(config.twilio_actions))
    return executors


def _build_outbound_helpers(
    event_bus: EventBus,
    oc: OutboundCallConfig,
    result: TelephonyHelpers,
    *,
    dnc_list: Any | None = None,
) -> None:
    """Build and wire the outbound call pipeline helpers."""
    # Telephony runtime classes are imported here (not at module scope) so a
    # non-telephony session never loads the outbound stack — see the module
    # docstring.
    # Resolve the manager through the factory module namespace (PEP 562
    # ``__getattr__``) rather than a direct ``from ... import`` so tests can
    # ``monkeypatch`` it via ``easycat.config._factory.OutboundCallManager``.
    from easycat.config import _factory
    from easycat.telephony.call_state import OutboundCallState, OutboundCallStateMachine
    from easycat.telephony.ivr import IVRAction, IVRActionType, IVRNavigator
    from easycat.telephony.number_health import CallDispositionTracker, NumberHealthMonitor
    from easycat.telephony.retry import RetryStrategy
    from easycat.telephony.screening import (
        CallScreeningDetector,
        screening_patterns_for_languages,
    )
    from easycat.telephony.voicemail import (
        PostScreeningVoicemailDetector,
        STTAMDFusionClassifier,
        VoicemailPolicyHandler,
    )

    OutboundCallManager = _factory.OutboundCallManager

    helpers = result.helpers

    # STT+AMD fusion classifier — must be wired before the state machine
    # so that raw AMD events are intercepted and re-emitted with source="fusion".
    fusion = STTAMDFusionClassifier(event_bus)
    helpers.append(fusion)

    # Post-screening voicemail detector — re-classifies after screening.
    post_screening_vm = PostScreeningVoicemailDetector(event_bus)
    helpers.append(post_screening_vm)

    # Disposition tracking must subscribe before the state machine: on
    # CallFailed, the tracker records the specific failure reason before
    # the state machine emits the terminal ENDED transition.
    if oc.enable_disposition_tracker:
        helpers.append(CallDispositionTracker(event_bus))

    def _on_screening_for_post_vm(event: CallScreening) -> None:
        post_screening_vm.activate()

    event_bus.subscribe(CallScreening, _on_screening_for_post_vm)

    # Build language-aware screening patterns once so both the state
    # machine and the screening detector share the same set.
    screening_langs = ["en"]
    if oc.callee_language and oc.callee_language != "en":
        screening_langs.append(oc.callee_language)
    _screening_patterns = screening_patterns_for_languages(screening_langs)

    # State machine — expect fused voicemail events (ignore raw AMD).
    sm = OutboundCallStateMachine(
        event_bus,
        classification_timeout_s=float(oc.voicemail_detection.detection_timeout_s),
        max_call_duration_s=oc.max_call_duration_s,
        classification_gate=oc.classification_gate,
        classification_gate_timeout_s=oc.classification_gate_timeout_s,
        classification_gate_hold_audio=oc.classification_gate_hold_audio,
        expect_fused_voicemail=True,
        late_voicemail_window_s=oc.late_voicemail_window_s,
        voicemail_pickup_window_s=oc.voicemail_pickup_window_s,
        screening_patterns=_screening_patterns,
    )
    helpers.append(sm)
    result.state_machine = sm

    # Screening detector.
    if oc.enable_screening_detection:
        screening = CallScreeningDetector(
            event_bus,
            enabled=True,
            screening_response=oc.screening_response,
            screening_use_agent=oc.screening_use_agent,
            max_screening_turns=oc.max_screening_turns,
            patterns=_screening_patterns,
            # Defense-in-depth: only analyze inbound (callee) transcripts so
            # the bot's own speech (when transcription_track="both") cannot
            # trigger a false screening match — mirroring the hard-coded
            # inbound filter in call_state._on_stt_final.
            track_filter="inbound",
        )
        helpers.append(screening)
        result.screening_detector = screening

    # IVR navigator — only created when an agent callback is configured.
    if oc.ivr_agent_callback is not None:
        ivr_delivery = oc.ivr_dtmf_delivery
        ivr = IVRNavigator(
            event_bus,
            agent_callback=oc.ivr_agent_callback,
            dtmf_delivery=ivr_delivery,
        )
        helpers.append(ivr)

        # Propagate the live call SID so DTMFDelivery can send digits/speech.
        if ivr_delivery is not None:

            async def _on_call_initiated_for_ivr(event: CallInitiated) -> None:
                ivr_delivery.call_sid = event.call_sid

            event_bus.subscribe(CallInitiated, _on_call_initiated_for_ivr)

        def _on_state_changed_for_ivr(event: CallStateChanged) -> None:
            if event.new == OutboundCallState.IVR:
                ivr.activate()
            elif event.new in {OutboundCallState.HUMAN, OutboundCallState.ENDED}:
                ivr.deactivate()

        event_bus.subscribe(CallStateChanged, _on_state_changed_for_ivr)

        # React to IVR navigator actions: human pickup, speech, and hangup.
        async def _on_ivr_action(event: IVRAction) -> None:
            if event.type == IVRActionType.HUMAN_DETECTED:
                if sm.state == OutboundCallState.IVR:
                    await sm.transition(OutboundCallState.HUMAN)
            elif event.type == IVRActionType.HANGUP:
                if sm.state == OutboundCallState.IVR:
                    await sm.transition(OutboundCallState.ENDED)
            elif event.type == IVRActionType.SPEAK:
                if ivr_delivery is not None:
                    await ivr_delivery.send_speech(event.text)

        event_bus.subscribe(IVRAction, _on_ivr_action)

    # Voicemail policy handler.
    helpers.append(VoicemailPolicyHandler(event_bus, expect_fused=True))

    # Observability helpers — pure event-bus listeners, on by default.
    if oc.enable_number_health:
        helpers.append(NumberHealthMonitor(event_bus))

    # Outbound call manager (requires Twilio credentials).
    manager: OutboundCallManager | None = None
    if oc.twilio_account_sid and oc.twilio_auth_token:
        try:
            manager = OutboundCallManager(
                event_bus,
                from_number=oc.from_number,
                enable_realtime_transcription=oc.enable_realtime_transcription,
                twilio_account_sid=oc.twilio_account_sid,
                twilio_auth_token=oc.twilio_auth_token,
                twiml_url=oc.twiml_url,
                status_callback_url=oc.status_callback_url,
                **oc.voicemail_detection.to_twilio_params(),
            )
            manager.dnc_list = dnc_list
            helpers.append(manager)
        except ImportError:
            logger.warning("twilio package not installed — OutboundCallManager disabled")

    # Retry strategy — stateless object the caller asks
    # ``strategy.record_attempt(number, reason)`` to decide whether to
    # re-place a failed call.  We attach it to the manager (when
    # present) so app code can reach it via
    # ``session.telephony.outbound_call_manager.retry_strategy``.
    if oc.enable_retry_strategy and manager is not None:
        manager.retry_strategy = RetryStrategy(oc.retry_strategy)


class _OutboundPipelineWiring:
    """Encapsulates mutable state for the outbound pipeline callbacks.

    Replaces bare closures with ``nonlocal`` to avoid unsynchronized
    access to ``_hold_audio_task`` from concurrent async callbacks.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._lock = asyncio.Lock()
        self._hold_audio_task: asyncio.Task[None] | None = None

    async def flush_gated_audio(self, events: list[TTSAudio]) -> None:
        async with self._lock:
            if self._hold_audio_task is not None and not self._hold_audio_task.done():
                self._hold_audio_task.cancel()
                try:
                    await self._hold_audio_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._hold_audio_task = None
        await self._session.replay_gated_audio(events)

    def play_hold_audio(self, text: str) -> None:
        async def _synthesize_hold() -> None:
            try:
                await self._session.synthesize_bypass(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Hold audio synthesis failed")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop — hold audio skipped")
            return

        # The lock is async-only; since this is a sync callback we just
        # do a best-effort swap — the flush side holds the lock and will
        # cancel whatever task reference it sees.
        self._hold_audio_task = loop.create_task(_synthesize_hold())


async def _run_agent_once(agent: Any, prompt: str) -> str:
    """Drive the agent once and return its final text response.

    Works whether ``agent`` is an :class:`AgentRunner` (has ``run()``) or
    a raw :class:`ExternalAgentBridge` (only implements ``invoke()``), so
    ``wrap_agent=False`` sessions still support agent-mode screening.
    """
    run_fn = getattr(agent, "run", None)
    if callable(run_fn):
        return await run_fn(prompt)
    accumulated = ""
    async for event in agent.invoke(AgentTurnInput.from_text(prompt), NULL_RECORDER):
        if event.kind == "text_delta" and event.text:
            accumulated += event.text
        elif event.kind == "done" and event.text:
            accumulated = event.text
    return accumulated


def wire_outbound_pipeline(
    session: Session,
    helpers: TelephonyHelpers,
    event_bus: EventBus,
) -> None:
    """Connect the outbound call state machine to the session pipeline.

    Wires the classification gate flush/hold callbacks and the screening
    response handler so that TTS audio is buffered, replayed, and the bot
    responds to screening prompts. Reads ``helpers.state_machine`` and
    ``helpers.screening_detector`` directly — no ``isinstance`` re-scan.
    """
    from easycat.telephony.screening import ScreeningResponse

    sm = helpers.state_machine
    assert sm is not None  # only called when an outbound state machine exists

    wiring = _OutboundPipelineWiring(session)

    sm.set_gate_flush_callback(wiring.flush_gated_audio)
    sm.gate.set_hold_audio_callback(wiring.play_hold_audio)

    _screening_detector = helpers.screening_detector

    async def _on_screening_response(event: ScreeningResponse) -> None:
        if event.mode == "agent" and _screening_detector is not None:
            try:
                response_text = await _run_agent_once(
                    session.agent,
                    "The callee's phone is screening this outbound call. "
                    "Provide only a brief caller identification for the screening service. "
                    "Do not use tools or take external actions for this screening reply.",
                )
                in_time = _screening_detector.notify_agent_responded()
                fallback_spoken = not in_time and _screening_detector.screening_response
                if response_text and not fallback_spoken:
                    await session.synthesize_bypass(response_text)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Agent-mode screening response failed, using static fallback")
                if _screening_detector.screening_response:
                    await session.synthesize_bypass(_screening_detector.screening_response)
        elif event.text:
            await session.synthesize_bypass(event.text)

    event_bus.subscribe(ScreeningResponse, _on_screening_response)
