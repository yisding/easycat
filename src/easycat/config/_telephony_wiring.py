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

from easycat.events import EventBus, EventSubscription, ScreeningResponse, TTSAudio
from easycat.runtime.scope import BackgroundTaskScope
from easycat.session.actions import SessionActionExecutor

if TYPE_CHECKING:
    from easycat.session._session import Session
    from easycat.telephony.call_state import OutboundCallStateMachine
    from easycat.telephony.compliance import DNCStore
    from easycat.telephony.screening import CallScreeningDetector
    from easycat.telephony.voicemail import VoicemailDetector

    from .easy import OutboundCallConfig, TelephonyConfig

logger = logging.getLogger("easycat.config")
_HOLD_AUDIO_MEMBER = "outbound_hold_audio"


@dataclass
class TelephonyHelpers:
    """Typed result of :func:`create_telephony_helpers`.

    ``helpers`` is the full list the session starts/stops. Typed collaborator
    fields let the wiring connect call-boundary and pipeline behavior without
    re-scanning ``list[Any]`` with ``isinstance``.
    """

    helpers: list[Any] = field(default_factory=list)
    state_machine: OutboundCallStateMachine | None = None
    screening_detector: CallScreeningDetector | None = None
    voicemail_detector: VoicemailDetector | None = None


def create_telephony_helpers(
    event_bus: EventBus,
    config: TelephonyConfig | None,
    *,
    dnc_list: DNCStore | None = None,
) -> TelephonyHelpers:
    result = TelephonyHelpers()
    if config is None:
        return result

    if config.enable_dtmf_aggregator:
        from easycat.telephony.dtmf import DTMFAggregator

        result.helpers.append(DTMFAggregator(event_bus, config.dtmf_aggregator))

    if config.enable_voicemail_detector:
        from easycat.telephony.voicemail import VoicemailDetector

        result.voicemail_detector = VoicemailDetector(event_bus, config.voicemail_detector)
        result.helpers.append(result.voicemail_detector)

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
    dnc_list: DNCStore | None = None,
) -> None:
    """Build and wire the outbound call pipeline helpers."""
    from easycat.config import _factory

    from ._outbound_helpers import build_outbound_helpers

    built = build_outbound_helpers(
        event_bus,
        oc,
        manager_cls=_factory.OutboundCallManager,
        dnc_list=dnc_list,
    )
    result.helpers.extend(built.helpers)
    result.state_machine = built.state_machine
    result.screening_detector = built.screening_detector
    if result.voicemail_detector is not None:
        result.voicemail_detector.set_call_boundary_acceptor(
            built.state_machine.accepts_call_initiation
        )


class _OutboundPipelineWiring:
    """Encapsulates mutable state for the outbound pipeline callbacks.

    Replaces bare closures with ``nonlocal`` to avoid unsynchronized
    access to ``_hold_audio_task`` from concurrent async callbacks.
    """

    def __init__(
        self,
        session: Session,
        event_bus: EventBus,
        screening_detector: CallScreeningDetector | None = None,
    ) -> None:
        self._session = session
        self._event_bus = event_bus
        self._screening_detector = screening_detector
        self._lock = asyncio.Lock()
        self._hold_audio_tasks = BackgroundTaskScope(name="outbound-pipeline")
        self._hold_audio_task: asyncio.Task[None] | None = None
        self._screening_subscription: EventSubscription | None = None

    def start(self) -> None:
        """Subscribe the outbound callbacks for the active session lifecycle."""
        if self._screening_subscription is not None:
            return
        self._screening_subscription = self._event_bus.subscribe(
            ScreeningResponse,
            self._on_screening_response,
        )

    def stop(self) -> None:
        """Detach callbacks and cancel hold audio owned by this wiring."""
        subscription = self._screening_subscription
        self._screening_subscription = None
        if subscription is not None:
            subscription.unsubscribe()

        task = self._hold_audio_task
        self._hold_audio_task = None
        self._hold_audio_tasks.cancel(_HOLD_AUDIO_MEMBER)
        if task is None or task.done():
            return
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if task is not current_task:
            task.cancel()

    async def flush_gated_audio(self, events: list[TTSAudio]) -> None:
        async with self._lock:
            task = self._hold_audio_task
            if task is not None and not task.done():
                self._hold_audio_tasks.cancel(_HOLD_AUDIO_MEMBER)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise
                except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
                    pass
                if self._hold_audio_task is task:
                    self._hold_audio_task = None
        await self._session.replay_gated_audio(events)

    def play_hold_audio(self, text: str) -> None:
        if self._screening_subscription is None:
            return

        async def _synthesize_hold() -> None:
            try:
                await self._session.synthesize_bypass(text)
            except Exception:
                logger.exception("Hold audio synthesis failed")
            finally:
                if self._hold_audio_task is asyncio.current_task():
                    self._hold_audio_task = None

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop — hold audio skipped")
            return

        # This synchronous callback cannot acquire the async flush lock. The
        # named scope still makes replacement deterministic, while flush()
        # captures one handle and only clears that same generation.
        self._hold_audio_task = self._hold_audio_tasks.create_task(
            _HOLD_AUDIO_MEMBER,
            _synthesize_hold(),
            replace=True,
        )

    async def _on_screening_response(self, event: ScreeningResponse) -> None:
        detector = self._screening_detector
        if event.mode == "agent" and detector is not None:
            try:
                response_text = await self._session.prompt_agent(
                    "The callee's phone is screening this outbound call. "
                    "Provide only a brief caller identification for the screening service. "
                    "Do not use tools or take external actions for this screening reply.",
                    role="system",
                    speak=False,
                )
                in_time = detector.notify_agent_responded()
                fallback_spoken = not in_time and detector.screening_response
                if response_text and not fallback_spoken:
                    await self._session.synthesize_bypass(response_text)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Agent-mode screening response failed, using static fallback")
                if detector.screening_response:
                    await self._session.synthesize_bypass(detector.screening_response)
        elif event.text:
            await self._session.synthesize_bypass(event.text)


def wire_outbound_pipeline(
    session: Session,
    helpers: TelephonyHelpers,
    event_bus: EventBus,
) -> _OutboundPipelineWiring:
    """Connect the outbound call state machine to the session pipeline.

    Wires the classification gate flush/hold callbacks and the screening
    response handler so that TTS audio is buffered, replayed, and the bot
    responds to screening prompts. Reads ``helpers.state_machine`` and
    ``helpers.screening_detector`` directly — no ``isinstance`` re-scan.
    """
    sm = helpers.state_machine
    assert sm is not None  # only called when an outbound state machine exists

    wiring = _OutboundPipelineWiring(
        session,
        event_bus,
        helpers.screening_detector,
    )

    sm.set_gate_flush_callback(wiring.flush_gated_audio)
    sm.gate.set_hold_audio_callback(wiring.play_hold_audio)
    helpers.helpers.append(wiring)
    session.telephony.helpers.append(wiring)
    return wiring
