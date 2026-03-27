"""Outbound call state machine: coordinates AMD, screening, voicemail, and IVR detection."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from easycat.events import (
    CallAnswered,
    CallEnded,
    CallFailed,
    CallRinging,
    CallScreening,
    EventBus,
    STTFinal,
    TTSAudio,
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

# States where SmartTurn should be suppressed.
SMART_TURN_SUPPRESS_STATES = frozenset(
    {
        OutboundCallState.CLASSIFYING,
        OutboundCallState.SCREENING,
        OutboundCallState.IVR,
    }
)


@dataclass(frozen=True)
class CallStateChanged:
    """Emitted on every state transition."""

    old: OutboundCallState
    new: OutboundCallState
    call_sid: str = ""


class ClassificationGate:
    """Buffers TTS audio during the CLASSIFYING state.

    When the gate is closed, TTS audio frames are buffered. When the gate
    opens (classification complete, or timeout), buffered frames are flushed
    to the transport via the provided callback.
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        enabled: bool = True,
        timeout_s: float = 5.0,
        hold_audio: str = "",
        on_flush: Callable[[list[TTSAudio]], None] | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._enabled = enabled
        self._timeout_s = timeout_s
        self._hold_audio = hold_audio
        self._on_flush = on_flush

        self._closed = False
        self._buffer: list[TTSAudio] = []
        self._timeout_task: asyncio.Task[None] | None = None
        self._started = False
        self._hold_audio_playing = False
        self._on_flush_async: Callable[[list[TTSAudio]], Any] | None = None

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def buffer(self) -> list[TTSAudio]:
        return list(self._buffer)

    def start(self) -> None:
        if not self._enabled or self._started:
            return
        self._event_bus.subscribe(TTSAudio, self._on_tts_audio)
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._event_bus.unsubscribe(TTSAudio, self._on_tts_audio)
        self._cancel_timeout()
        self._buffer.clear()
        self._closed = False
        self._started = False
        self._hold_audio_playing = False

    def close(self) -> None:
        """Close the gate — start buffering TTS audio."""
        if not self._enabled:
            return
        self._closed = True
        self._buffer.clear()
        self._start_timeout()
        if self._hold_audio:
            self._hold_audio_playing = True

    def release(self) -> list[TTSAudio]:
        """Open the gate — flush buffered audio and stop buffering."""
        self._cancel_timeout()
        self._closed = False
        self._hold_audio_playing = False
        buffered = list(self._buffer)
        self._buffer.clear()
        if self._on_flush and buffered:
            self._on_flush(buffered)
        return buffered

    async def _on_tts_audio(self, event: TTSAudio) -> None:
        if self._closed:
            self._buffer.append(event)

    def _start_timeout(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timeout_task = loop.create_task(self._timeout_coro())

    def _cancel_timeout(self) -> None:
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        self._timeout_task = None

    async def _timeout_coro(self) -> None:
        try:
            await asyncio.sleep(self._timeout_s)
            if self._closed:
                buffered = self.release()
                if buffered and self._on_flush_async:
                    await self._on_flush_async(buffered)
        except asyncio.CancelledError:
            pass


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
        classification_gate: bool = False,
        classification_gate_timeout_s: float = 5.0,
        classification_gate_hold_audio: str = "",
        smart_turn_suppress: bool = False,
        vad_timeout_extension_s: float = 0.0,
        expect_fused_voicemail: bool = False,
    ) -> None:
        self._event_bus = event_bus
        self._call_sid = call_sid
        self._classification_timeout_s = classification_timeout_s
        self._max_call_duration_s = max_call_duration_s
        self._expect_fused_voicemail = expect_fused_voicemail
        self._smart_turn_suppress = smart_turn_suppress
        self._vad_timeout_extension_s = vad_timeout_extension_s

        self._state = OutboundCallState.INITIATING
        self._started = False
        self._classification_timer: asyncio.TimerHandle | None = None
        self._classification_task: asyncio.Task[None] | None = None
        self._max_duration_task: asyncio.Task[None] | None = None

        # Classification gate.
        self._gate = ClassificationGate(
            event_bus,
            enabled=classification_gate,
            timeout_s=classification_gate_timeout_s,
            hold_audio=classification_gate_hold_audio,
        )

        # SmartTurn suppression state.
        self._smart_turn_suppressed = False

        # Callback for SmartTurn suppression (set by session integration).
        self._on_smart_turn_suppress: Callable[[bool], None] | None = None
        self._on_vad_timeout_change: Callable[[float], None] | None = None

        # Async callback to re-enqueue gated audio when the gate releases.
        self._on_gate_flush: Callable[[list[TTSAudio]], Any] | None = None

    @property
    def state(self) -> OutboundCallState:
        return self._state

    @property
    def gate(self) -> ClassificationGate:
        return self._gate

    @property
    def smart_turn_suppressed(self) -> bool:
        return self._smart_turn_suppressed

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
        self._event_bus.subscribe(STTFinal, self._on_stt_final)
        self._gate.start()
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
        self._event_bus.unsubscribe(STTFinal, self._on_stt_final)
        self._gate.stop()
        self._cancel_timers()
        self._started = False

    def _cancel_timers(self) -> None:
        if self._classification_task and not self._classification_task.done():
            self._classification_task.cancel()
            self._classification_task = None
        if self._max_duration_task and not self._max_duration_task.done():
            self._max_duration_task.cancel()
            self._max_duration_task = None

    # ── SmartTurn suppression ─────────────────────────────────────

    def _update_smart_turn_suppression(self) -> None:
        """Update SmartTurn suppression based on current state."""
        if not self._smart_turn_suppress:
            return
        should_suppress = self._state in SMART_TURN_SUPPRESS_STATES
        if should_suppress != self._smart_turn_suppressed:
            self._smart_turn_suppressed = should_suppress
            if self._on_smart_turn_suppress:
                self._on_smart_turn_suppress(should_suppress)

        # Extend VAD timeout during screening/IVR states.
        if self._vad_timeout_extension_s > 0 and self._on_vad_timeout_change:
            if self._state in {OutboundCallState.SCREENING, OutboundCallState.IVR}:
                self._on_vad_timeout_change(self._vad_timeout_extension_s)
            elif self._state == OutboundCallState.HUMAN:
                self._on_vad_timeout_change(0.0)  # Reset to default.

    # ── State transitions ─────────────────────────────────────────

    async def _transition(self, new_state: OutboundCallState) -> None:
        if self._state == new_state:
            return
        old = self._state
        self._state = new_state
        await self._event_bus.emit(
            CallStateChanged(old=old, new=new_state, call_sid=self._call_sid)
        )
        self._update_smart_turn_suppression()

        # Release classification gate when leaving CLASSIFYING.
        if old == OutboundCallState.CLASSIFYING and self._gate.is_closed:
            buffered = self._gate.release()
            if buffered and self._on_gate_flush:
                await self._on_gate_flush(buffered)

    async def _on_ringing(self, event: CallRinging) -> None:
        if self._state == OutboundCallState.INITIATING:
            await self._transition(OutboundCallState.RINGING)

    async def _on_answered(self, event: CallAnswered) -> None:
        if self._state in {OutboundCallState.INITIATING, OutboundCallState.RINGING}:
            await self._transition(OutboundCallState.CLASSIFYING)
            self._gate.close()
            self._start_classification_timeout()
            self._start_max_duration_timer()

    async def _on_failed(self, event: CallFailed) -> None:
        self._cancel_timers()
        await self._transition(OutboundCallState.ENDED)

    async def _on_ended(self, event: CallEnded) -> None:
        self._cancel_timers()
        await self._transition(OutboundCallState.ENDED)

    async def _on_voicemail(self, event: VoicemailDetected) -> None:
        # When a fusion classifier is active, ignore raw AMD events.
        if self._expect_fused_voicemail and event.source != "fusion":
            return
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

    async def _on_stt_final(self, event: STTFinal) -> None:
        """Handle STTFinal for IVR detection (CLASSIFYING) and SCREENING → HUMAN."""
        from easycat.telephony.ivr import classify_ivr_prompt
        from easycat.telephony.screening import _is_conversational

        text = event.text.strip()
        if not text:
            return

        if self._state == OutboundCallState.CLASSIFYING:
            if classify_ivr_prompt(text):
                self._cancel_classification_timeout()
                await self._transition(OutboundCallState.IVR)
            return

        if self._state == OutboundCallState.SCREENING:
            if _is_conversational(text):
                await self._transition(OutboundCallState.HUMAN)

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
            await self._event_bus.emit(
                CallEnded(call_sid=self._call_sid, disposition="max_duration")
            )
