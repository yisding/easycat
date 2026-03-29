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
    ScreeningTimedOut,
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
        # Callback invoked when hold audio should be played (set by session wiring).
        self._on_hold_audio: Callable[[str], Any] | None = None

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def buffer(self) -> list[TTSAudio]:
        return list(self._buffer)

    def set_flush_async_callback(self, callback: Callable[[list[TTSAudio]], Any]) -> None:
        """Set the async callback invoked when the gate releases on timeout."""
        self._on_flush_async = callback

    def set_hold_audio_callback(self, callback: Callable[[str], Any]) -> None:
        """Set the callback invoked when hold audio should be played."""
        self._on_hold_audio = callback

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
            if self._on_hold_audio:
                self._on_hold_audio(self._hold_audio)

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

    async def discard(self) -> None:
        """Cancel timeout and discard buffered audio, keeping the gate closed.

        Used when leaving CLASSIFYING for non-human states (VOICEMAIL,
        SCREENING, IVR) so that the remaining opener TTS chunks are still
        blocked by the gate instead of leaking to the outbound queue.
        Also invokes the async flush callback (with an empty list) so that
        hold audio is cancelled even when no opener audio was buffered.
        """
        self._cancel_timeout()
        self._hold_audio_playing = False
        self._buffer.clear()
        if self._on_flush_async:
            await self._on_flush_async([])

    async def _on_tts_audio(self, event: TTSAudio) -> None:
        if self._closed and not event.bypass_gate:
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
                # Inline the release logic instead of calling self.release(),
                # because release() cancels _timeout_task — which is *this* task.
                # That would inject CancelledError on the next await and drop
                # the buffered audio before _on_flush_async can re-enqueue it.
                self._closed = False
                self._hold_audio_playing = False
                self._timeout_task = None
                buffered = list(self._buffer)
                self._buffer.clear()
                if self._on_flush and buffered:
                    self._on_flush(buffered)
                if self._on_flush_async:
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
        late_voicemail_window_s: float = 0.0,
        voicemail_pickup_window_s: float = 0.0,
    ) -> None:
        self._event_bus = event_bus
        self._call_sid = call_sid
        self._classification_timeout_s = classification_timeout_s
        self._max_call_duration_s = max_call_duration_s
        self._expect_fused_voicemail = expect_fused_voicemail
        self._smart_turn_suppress = smart_turn_suppress
        self._vad_timeout_extension_s = vad_timeout_extension_s
        self._late_voicemail_window_s = late_voicemail_window_s
        self._voicemail_pickup_window_s = voicemail_pickup_window_s

        self._state = OutboundCallState.INITIATING
        self._started = False
        self._classification_timer: asyncio.TimerHandle | None = None
        self._classification_task: asyncio.Task[None] | None = None
        self._max_duration_task: asyncio.Task[None] | None = None
        self._late_voicemail_task: asyncio.Task[None] | None = None
        self._voicemail_pickup_task: asyncio.Task[None] | None = None

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

    @property
    def state(self) -> OutboundCallState:
        return self._state

    @property
    def gate(self) -> ClassificationGate:
        return self._gate

    @property
    def smart_turn_suppressed(self) -> bool:
        return self._smart_turn_suppressed

    def set_gate_flush_callback(self, callback: Callable[[list[TTSAudio]], Any]) -> None:
        """Set the async callback for re-enqueuing gated audio on release.

        This sets the callback on the gate directly so both explicit release
        (from state transition) and timeout release use the same path.
        """
        self._gate.set_flush_async_callback(callback)

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
        self._event_bus.subscribe(ScreeningTimedOut, self._on_screening_timed_out)
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
        self._event_bus.unsubscribe(ScreeningTimedOut, self._on_screening_timed_out)
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
        self._cancel_late_voicemail_window()
        self._cancel_voicemail_pickup_window()

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

    async def transition(self, new_state: OutboundCallState) -> None:
        """Public API for external callers to trigger a state transition."""
        await self._transition(new_state)

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
        # Only re-enqueue buffered opener audio for HUMAN/UNKNOWN — for
        # VOICEMAIL, SCREENING, and IVR the opener should not be played
        # and the gate stays closed to block any remaining TTS chunks.
        if old == OutboundCallState.CLASSIFYING and self._gate.is_closed:
            if new_state in {OutboundCallState.HUMAN, OutboundCallState.UNKNOWN}:
                buffered = self._gate.release()
                if self._gate._on_flush_async:
                    await self._gate._on_flush_async(buffered)
            else:
                await self._gate.discard()

        # Reopen the gate when SCREENING, IVR, or VOICEMAIL resolves to HUMAN.
        # discard() above kept the gate closed to block opener TTS during
        # classification, but once the callee is confirmed human the gate
        # must open so normal agent TTS can reach the transport.
        if (
            old
            in {OutboundCallState.SCREENING, OutboundCallState.IVR, OutboundCallState.VOICEMAIL}
            and new_state == OutboundCallState.HUMAN
            and self._gate.is_closed
        ):
            self._gate.release()

        # Start late voicemail detection window when entering HUMAN.
        if new_state == OutboundCallState.HUMAN and self._late_voicemail_window_s > 0:
            self._start_late_voicemail_window()

        # Start voicemail pickup detection window when entering VOICEMAIL.
        if new_state == OutboundCallState.VOICEMAIL and self._voicemail_pickup_window_s > 0:
            self._start_voicemail_pickup_window()

    async def _on_ringing(self, event: CallRinging) -> None:
        if self._state == OutboundCallState.INITIATING:
            if event.call_sid:
                self._call_sid = event.call_sid
            await self._transition(OutboundCallState.RINGING)

    async def _on_answered(self, event: CallAnswered) -> None:
        if self._state in {OutboundCallState.INITIATING, OutboundCallState.RINGING}:
            if event.call_sid:
                self._call_sid = event.call_sid
            await self._transition(OutboundCallState.CLASSIFYING)
            self._gate.close()
            self._start_classification_timeout()
            self._start_max_duration_timer()

    async def _on_failed(self, event: CallFailed) -> None:
        if event.call_sid:
            self._call_sid = event.call_sid
        self._cancel_timers()
        await self._transition(OutboundCallState.ENDED)

    async def _on_ended(self, event: CallEnded) -> None:
        if event.call_sid:
            self._call_sid = event.call_sid
        self._cancel_timers()
        await self._transition(OutboundCallState.ENDED)

    async def _on_voicemail(self, event: VoicemailDetected) -> None:
        # When a fusion classifier is active, ignore raw AMD events (empty source)
        # but accept both fused and detector-sourced events.
        if self._expect_fused_voicemail and not event.source:
            return
        if event.result == "human" and self._state in {
            OutboundCallState.CLASSIFYING,
            OutboundCallState.SCREENING,
        }:
            self._cancel_classification_timeout()
            await self._transition(OutboundCallState.HUMAN)
        elif event.result == "machine" and self._state in {
            OutboundCallState.CLASSIFYING,
            OutboundCallState.SCREENING,
        }:
            self._cancel_classification_timeout()
            await self._transition(OutboundCallState.VOICEMAIL)
        elif (
            event.result == "machine"
            and self._state == OutboundCallState.HUMAN
            and self._late_voicemail_task is not None
            and not self._late_voicemail_task.done()
        ):
            # Late voicemail detection: beep or long monologue after HUMAN.
            self._cancel_late_voicemail_window()
            logger.info("Late voicemail detected during HUMAN state — transitioning to VOICEMAIL")
            await self._transition(OutboundCallState.VOICEMAIL)
        elif (
            event.result == "human"
            and self._state == OutboundCallState.VOICEMAIL
            and self._voicemail_pickup_task is not None
            and not self._voicemail_pickup_task.done()
        ):
            # Voicemail pickup: human answered during voicemail (e.g. iOS Live Voicemail).
            self._cancel_voicemail_pickup_window()
            logger.info("Human pickup detected during VOICEMAIL state — transitioning to HUMAN")
            await self._transition(OutboundCallState.HUMAN)

    async def _on_screening(self, event: CallScreening) -> None:
        if self._state == OutboundCallState.CLASSIFYING:
            self._cancel_classification_timeout()
            await self._transition(OutboundCallState.SCREENING)

    async def _on_screening_timed_out(self, event: ScreeningTimedOut) -> None:
        if self._state == OutboundCallState.SCREENING:
            await self._transition(OutboundCallState.HUMAN)

    async def _on_stt_final(self, event: STTFinal) -> None:
        """Handle STTFinal for IVR detection (CLASSIFYING) and SCREENING → HUMAN."""
        from easycat.telephony.ivr import classify_ivr_prompt
        from easycat.telephony.screening import is_conversational
        from easycat.telephony.voicemail import classify_greeting

        text = event.text.strip()
        if not text:
            return

        if self._state == OutboundCallState.CLASSIFYING:
            # Skip outbound-track transcripts (bot's own opener fed back
            # when transcription_track="both") to avoid the assistant's
            # greeting satisfying is_conversational() and short-circuiting
            # classification before AMD/screening/voicemail has resolved.
            if getattr(event, "track", None) == "outbound":
                return
            if classify_ivr_prompt(text):
                self._cancel_classification_timeout()
                await self._transition(OutboundCallState.IVR)
            elif classify_greeting(text) == "machine":
                # Short voicemail greetings (e.g. "Please leave a message")
                # pass is_conversational's word-count check but contain known
                # voicemail phrases — let the fusion classifier handle them
                # instead of misrouting to HUMAN.
                pass
            elif is_conversational(text):
                self._cancel_classification_timeout()
                await self._transition(OutboundCallState.HUMAN)
            return

        if self._state == OutboundCallState.SCREENING:
            # Skip outbound-track transcripts (bot's own speech fed back
            # when transcription_track="both") to avoid misclassifying the
            # bot's screening reply as the callee picking up.
            if getattr(event, "track", None) == "outbound":
                return
            if is_conversational(text):
                await self._transition(OutboundCallState.HUMAN)

        if (
            self._state == OutboundCallState.VOICEMAIL
            and self._voicemail_pickup_task is not None
            and not self._voicemail_pickup_task.done()
        ):
            # Skip outbound-track transcripts (bot's own voicemail message).
            if getattr(event, "track", None) == "outbound":
                return
            # Exclude voicemail system prompts from triggering false human detection.
            if classify_greeting(text) == "machine":
                return
            if is_conversational(text):
                self._cancel_voicemail_pickup_window()
                logger.info(
                    "Conversational speech during VOICEMAIL — transitioning to HUMAN"
                )
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
        try:
            await asyncio.sleep(self._classification_timeout_s)
            if self._state == OutboundCallState.CLASSIFYING:
                await self._transition(OutboundCallState.UNKNOWN)
        except asyncio.CancelledError:
            pass

    def _start_max_duration_timer(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._max_duration_task = loop.create_task(self._max_duration_coro())

    async def _max_duration_coro(self) -> None:
        try:
            await asyncio.sleep(self._max_call_duration_s)
            if self._state != OutboundCallState.ENDED:
                await self._transition(OutboundCallState.ENDED)
                await self._event_bus.emit(
                    CallEnded(call_sid=self._call_sid, disposition="max_duration")
                )
        except asyncio.CancelledError:
            pass

    # ── Late voicemail window ────────────────────────────────────

    def _start_late_voicemail_window(self) -> None:
        self._cancel_late_voicemail_window()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._late_voicemail_task = loop.create_task(self._late_voicemail_coro())

    def _cancel_late_voicemail_window(self) -> None:
        if self._late_voicemail_task and not self._late_voicemail_task.done():
            self._late_voicemail_task.cancel()
        self._late_voicemail_task = None

    async def _late_voicemail_coro(self) -> None:
        """After the window expires, stop accepting late voicemail signals."""
        try:
            await asyncio.sleep(self._late_voicemail_window_s)
        except asyncio.CancelledError:
            pass
        finally:
            self._late_voicemail_task = None

    # ── Voicemail pickup window ─────────────────────────────────

    def _start_voicemail_pickup_window(self) -> None:
        self._cancel_voicemail_pickup_window()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._voicemail_pickup_task = loop.create_task(self._voicemail_pickup_coro())

    def _cancel_voicemail_pickup_window(self) -> None:
        if self._voicemail_pickup_task and not self._voicemail_pickup_task.done():
            self._voicemail_pickup_task.cancel()
        self._voicemail_pickup_task = None

    async def _voicemail_pickup_coro(self) -> None:
        """After the window expires, stop accepting voicemail pickup signals."""
        try:
            await asyncio.sleep(self._voicemail_pickup_window_s)
        except asyncio.CancelledError:
            pass
        finally:
            self._voicemail_pickup_task = None
