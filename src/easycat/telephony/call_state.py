"""Outbound call state machine: coordinates AMD, screening, voicemail, and IVR detection."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

from easycat.events import (
    CallAnswered,
    CallEnded,
    CallFailed,
    CallRinging,
    CallScreening,
    EventBus,
    VoicemailDetected,
)

logger = logging.getLogger(__name__)


class OutboundCallState(Enum):
    INITIATING = "initiating"
    RINGING = "ringing"
    ANSWERED = "answered"
    CLASSIFYING = "classifying"
    HUMAN = "human"
    SCREENING = "screening"
    VOICEMAIL = "voicemail"
    IVR = "ivr"
    UNKNOWN = "unknown"
    ENDED = "ended"


# States that represent a terminal classification (before ENDED).
TERMINAL_CLASSIFICATION_STATES = frozenset(
    {
        OutboundCallState.HUMAN,
        OutboundCallState.VOICEMAIL,
        OutboundCallState.IVR,
        OutboundCallState.UNKNOWN,
        OutboundCallState.ENDED,
    }
)


@dataclass(frozen=True)
class CallStateChanged:
    """Emitted on every state transition."""

    old: OutboundCallState
    new: OutboundCallState
    call_sid: str = ""


class OutboundCallStateMachine:
    """Coordinates all detection signals into a unified call disposition.

    Subscribes to call lifecycle events, AMD results, screening events,
    and voicemail detection.  Emits :class:`CallStateChanged` on each transition.
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        call_sid: str = "",
        classification_timeout_s: float = 10.0,
        max_call_duration_s: int = 300,
    ) -> None:
        self._event_bus = event_bus
        self._call_sid = call_sid
        self._classification_timeout_s = classification_timeout_s
        self._max_call_duration_s = max_call_duration_s

        self._state = OutboundCallState.INITIATING
        self._started = False
        self._classification_timer: asyncio.TimerHandle | None = None
        self._classification_task: asyncio.Task[None] | None = None
        self._max_duration_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> OutboundCallState:
        return self._state

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(CallRinging, self._on_ringing)
        self._event_bus.subscribe(CallAnswered, self._on_answered)
        self._event_bus.subscribe(CallFailed, self._on_failed)
        self._event_bus.subscribe(CallEnded, self._on_ended)
        self._event_bus.subscribe(VoicemailDetected, self._on_voicemail)
        self._event_bus.subscribe(CallScreening, self._on_screening)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._event_bus.unsubscribe(CallRinging, self._on_ringing)
        self._event_bus.unsubscribe(CallAnswered, self._on_answered)
        self._event_bus.unsubscribe(CallFailed, self._on_failed)
        self._event_bus.unsubscribe(CallEnded, self._on_ended)
        self._event_bus.unsubscribe(VoicemailDetected, self._on_voicemail)
        self._event_bus.unsubscribe(CallScreening, self._on_screening)
        self._cancel_timers()
        self._started = False

    def _cancel_timers(self) -> None:
        if self._classification_task and not self._classification_task.done():
            self._classification_task.cancel()
            self._classification_task = None
        if self._max_duration_task and not self._max_duration_task.done():
            self._max_duration_task.cancel()
            self._max_duration_task = None

    # ── State transitions ─────────────────────────────────────────

    async def _transition(self, new_state: OutboundCallState) -> None:
        if self._state == new_state:
            return
        old = self._state
        self._state = new_state
        await self._event_bus.emit(
            CallStateChanged(old=old, new=new_state, call_sid=self._call_sid)
        )

    async def _on_ringing(self, event: CallRinging) -> None:
        if self._state == OutboundCallState.INITIATING:
            await self._transition(OutboundCallState.RINGING)

    async def _on_answered(self, event: CallAnswered) -> None:
        if self._state in {OutboundCallState.INITIATING, OutboundCallState.RINGING}:
            await self._transition(OutboundCallState.CLASSIFYING)
            self._start_classification_timeout()
            self._start_max_duration_timer()

    async def _on_failed(self, event: CallFailed) -> None:
        self._cancel_timers()
        await self._transition(OutboundCallState.ENDED)

    async def _on_ended(self, event: CallEnded) -> None:
        self._cancel_timers()
        await self._transition(OutboundCallState.ENDED)

    async def _on_voicemail(self, event: VoicemailDetected) -> None:
        if event.result == "human" and self._state == OutboundCallState.CLASSIFYING:
            self._cancel_classification_timeout()
            await self._transition(OutboundCallState.HUMAN)
        elif event.result == "machine" and self._state in {
            OutboundCallState.CLASSIFYING,
            OutboundCallState.SCREENING,
        }:
            self._cancel_classification_timeout()
            await self._transition(OutboundCallState.VOICEMAIL)

    async def _on_screening(self, event: CallScreening) -> None:
        if self._state == OutboundCallState.CLASSIFYING:
            self._cancel_classification_timeout()
            await self._transition(OutboundCallState.SCREENING)

    # ── Timers ────────────────────────────────────────────────────

    def _start_classification_timeout(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._classification_task = loop.create_task(self._classification_timeout_coro())

    def _cancel_classification_timeout(self) -> None:
        if self._classification_task and not self._classification_task.done():
            self._classification_task.cancel()
            self._classification_task = None

    async def _classification_timeout_coro(self) -> None:
        await asyncio.sleep(self._classification_timeout_s)
        if self._state == OutboundCallState.CLASSIFYING:
            await self._transition(OutboundCallState.UNKNOWN)

    def _start_max_duration_timer(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._max_duration_task = loop.create_task(self._max_duration_coro())

    async def _max_duration_coro(self) -> None:
        await asyncio.sleep(self._max_call_duration_s)
        if self._state != OutboundCallState.ENDED:
            await self._transition(OutboundCallState.ENDED)
