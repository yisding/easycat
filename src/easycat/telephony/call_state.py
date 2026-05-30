"""Outbound call state machine: coordinates AMD, screening, voicemail, and IVR detection."""

from __future__ import annotations

__all__ = [
    "CallStateChanged",
    "ClassificationGate",
    "OutboundCallState",
    "OutboundCallStateMachine",
    "SMART_TURN_SUPPRESS_STATES",
    "TERMINAL_CLASSIFICATION_STATES",
]

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any

from easycat.events import (
    CallAnswered,
    CallEnded,
    CallFailed,
    CallInitiated,
    CallRinging,
    CallScreening,
    CallStateChanged,
    EventBus,
    ScreeningTimedOut,
    STTFinal,
    TTSAudio,
    VoicemailDetected,
)

if TYPE_CHECKING:
    from easycat.telephony.screening import ScreeningPatternSet

logger = logging.getLogger(__name__)


class OutboundCallState(Enum):
    INITIATING = "initiating"
    RINGING = "ringing"
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

# States that accept voicemail detection signals (CLASSIFYING or SCREENING).
_VOICEMAIL_ACCEPT_STATES = frozenset(
    {
        OutboundCallState.CLASSIFYING,
        OutboundCallState.SCREENING,
    }
)


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

        # Cap the buffer from the gate timeout so the bound scales with how
        # long the opener may legitimately be held, rather than an arbitrary
        # fixed count.  ~50 frames/s (20 ms PCMU/PCM frames) is a generous
        # upper bound for telephony TTS; a 1 s floor keeps very short timeouts
        # usable.  On overflow we drop the *newest* frames (see
        # :meth:`_on_tts_audio`) so the intelligible start of the opener
        # survives for replay after HUMAN classification.
        _frames_per_s = 50
        self._buffer_max = max(int(timeout_s * _frames_per_s), _frames_per_s)
        self._closed = False
        self._buffer: deque[TTSAudio] = deque()
        self._buffer_warned = False
        self._dropped_frames = 0
        self._timeout_task: asyncio.Task[None] | None = None
        self._started = False
        self._hold_audio_playing = False
        self._on_flush_async: Callable[[list[TTSAudio]], Any] | None = None
        # Callback invoked when hold audio should be played (set by session wiring).
        self._on_hold_audio: Callable[[str], Any] | None = None

    @property
    def is_buffering(self) -> bool:
        """Whether the gate is currently buffering (blocking) TTS audio."""
        return self._closed

    @property
    def buffer(self) -> list[TTSAudio]:
        return list(self._buffer)

    @property
    def dropped_frames(self) -> int:
        """Number of TTS frames dropped due to gate buffer overflow.

        Exposed as a metric so overflow (a sign the opener exceeded the
        gate's hold capacity) is observable rather than only logged once.
        """
        return self._dropped_frames

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
        self._buffer_warned = False
        self._dropped_frames = 0
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
        if self._started:
            self._event_bus.unsubscribe(TTSAudio, self._on_tts_audio)
            self._started = False
        buffered = list(self._buffer)
        self._buffer.clear()
        if self._on_flush and buffered:
            self._on_flush(buffered)
        return buffered

    async def flush_and_release(self) -> list[TTSAudio]:
        """Replay buffered audio via the async callback, then open the gate.

        Unlike :meth:`release`, the async flush callback is invoked while the
        gate is still closed.  This prevents in-flight TTS chunks from reaching
        the outbound queue (and being dropped by ``queue.flush()`` inside the
        callback) between gate release and replay.
        """
        self._cancel_timeout()
        self._hold_audio_playing = False
        buffered = list(self._buffer)
        self._buffer.clear()
        # Replay while gate is still closed.
        if self._on_flush_async and buffered:
            await self._on_flush_async(buffered)
        # Drain frames that arrived during the async flush (e.g. TTS
        # produced by CallStateChanged subscribers while the gate was
        # still closed).
        late = list(self._buffer)
        self._buffer.clear()
        # Now open the gate for future TTS chunks.
        self._closed = False
        if self._started:
            self._event_bus.unsubscribe(TTSAudio, self._on_tts_audio)
            self._started = False
        # Replay late arrivals now that the gate is open.
        if self._on_flush_async and late:
            await self._on_flush_async(late)
        return buffered + late

    async def discard(self) -> None:
        """Cancel timeout, discard buffered opener audio, and open the gate.

        Used when leaving CLASSIFYING for non-human states (VOICEMAIL,
        SCREENING, IVR): the opener must not play, so its buffered chunks are
        dropped.  The gate is then fully opened (``_closed = False`` and the
        TTSAudio subscription removed) so that any later TTS — e.g. a
        ``VoicemailPolicy.LEAVE_MESSAGE`` voicemail drop, or the agent's
        speech once a non-human state resolves to HUMAN — reaches the
        transport instead of being silently buffered with no timeout to
        release it.  Also invokes the async flush callback (with an empty
        list) so that hold audio is cancelled even when no opener audio was
        buffered.
        """
        self._cancel_timeout()
        self._hold_audio_playing = False
        self._buffer.clear()
        self._closed = False
        if self._started:
            self._event_bus.unsubscribe(TTSAudio, self._on_tts_audio)
            self._started = False
        if self._on_flush_async:
            await self._on_flush_async([])

    async def _on_tts_audio(self, event: TTSAudio) -> None:
        if self._closed and not event.bypass_gate:
            if len(self._buffer) >= self._buffer_max:
                # Drop the *newest* frame rather than the oldest: the start of
                # the opener carries the intelligible greeting that must survive
                # for replay after HUMAN classification.  Dropping from the
                # front (deque maxlen) would truncate the opener mid-sentence.
                self._dropped_frames += 1
                if not self._buffer_warned:
                    self._buffer_warned = True
                    logger.warning(
                        "Classification gate buffer full (%d frames) — "
                        "newest TTS frames will be dropped to preserve the "
                        "opener start",
                        self._buffer_max,
                    )
                return
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
                self._hold_audio_playing = False
                self._timeout_task = None
                buffered = list(self._buffer)
                self._buffer.clear()
                if self._on_flush and buffered:
                    self._on_flush(buffered)
                if self._on_flush_async:
                    await self._on_flush_async(buffered)
                # Open the gate after flushing so late TTS chunks cannot
                # slip past the buffer during the async replay — matching
                # the ordering in flush_and_release().
                self._closed = False
                if self._started:
                    self._event_bus.unsubscribe(TTSAudio, self._on_tts_audio)
                    self._started = False
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
        screening_patterns: ScreeningPatternSet | None = None,
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
        self._screening_patterns = screening_patterns

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

        # Cache cross-module helpers at start() to avoid per-event import overhead.
        from easycat.telephony.ivr import classify_ivr_prompt
        from easycat.telephony.screening import is_conversational
        from easycat.telephony.voicemail import classify_greeting

        self._classify_ivr_prompt = classify_ivr_prompt
        self._is_conversational = is_conversational
        self._classify_greeting = classify_greeting

        self._event_bus.subscribe(CallInitiated, self._on_call_initiated)
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
        self._event_bus.unsubscribe(CallInitiated, self._on_call_initiated)
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

    # ── New-call reset ─────────────────────────────────────────────

    async def _on_call_initiated(self, event: CallInitiated) -> None:
        """Reset the state machine when a new outbound call is placed.

        This allows a single session to handle sequential outbound calls
        without getting stuck in the ENDED state from a previous call.
        """
        if not event.call_sid:
            return
        if event.call_sid == self._call_sid:
            return
        if self._call_sid and self._state != OutboundCallState.ENDED:
            logger.debug(
                "Ignoring CallInitiated for %s while %s is active",
                event.call_sid,
                self._call_sid,
            )
            return
        self._cancel_timers()
        self._gate.stop()
        self._gate.start()
        self._call_sid = event.call_sid
        self._smart_turn_suppressed = False
        self._state = OutboundCallState.INITIATING

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
        # VOICEMAIL, SCREENING, and IVR the opener should not be played, so
        # discard() drops the buffered opener and fully opens the gate.
        # Opening it (rather than leaving it closed) means later TTS — e.g. a
        # leave-message voicemail drop, or agent speech once a non-human
        # state resolves to HUMAN — is no longer silently buffered with no
        # timeout to flush it.
        if old == OutboundCallState.CLASSIFYING and self._gate.is_buffering:
            if new_state in {OutboundCallState.HUMAN, OutboundCallState.UNKNOWN}:
                await self._gate.flush_and_release()
            else:
                await self._gate.discard()

        # Defensive reopen: if the gate is somehow still buffering when
        # SCREENING, IVR, or VOICEMAIL resolves to HUMAN (e.g. it was closed
        # by a future code path other than the CLASSIFYING entry above),
        # flush it so normal agent TTS can reach the transport.  In the
        # current flow discard() has already opened the gate, so this is a
        # no-op.
        if (
            old
            in {OutboundCallState.SCREENING, OutboundCallState.IVR, OutboundCallState.VOICEMAIL}
            and new_state == OutboundCallState.HUMAN
            and self._gate.is_buffering
        ):
            await self._gate.flush_and_release()

        if new_state == OutboundCallState.HUMAN and self._late_voicemail_window_s > 0:
            self._start_late_voicemail_window()

        if new_state == OutboundCallState.VOICEMAIL and self._voicemail_pickup_window_s > 0:
            self._start_voicemail_pickup_window()

    def _matches_active_call(self, call_sid: str) -> bool:
        """Return whether a lifecycle event belongs to the current call."""
        if not call_sid:
            return False
        if not self._call_sid:
            return True
        if call_sid == self._call_sid:
            return True
        logger.debug(
            "Ignoring stale call event for %s; active call is %s",
            call_sid,
            self._call_sid,
        )
        return False

    async def _on_ringing(self, event: CallRinging) -> None:
        if not self._matches_active_call(event.call_sid):
            return
        if self._state == OutboundCallState.INITIATING:
            self._call_sid = event.call_sid
            await self._transition(OutboundCallState.RINGING)

    async def _on_answered(self, event: CallAnswered) -> None:
        if not self._matches_active_call(event.call_sid):
            return
        if self._state in {OutboundCallState.INITIATING, OutboundCallState.RINGING}:
            self._call_sid = event.call_sid
            # Close the gate before transitioning so that any TTS emitted by
            # CallStateChanged subscribers is captured by the buffer.
            self._gate.close()
            await self._transition(OutboundCallState.CLASSIFYING)
            self._start_classification_timeout()
            self._start_max_duration_timer()

    async def _on_failed(self, event: CallFailed) -> None:
        if not self._matches_active_call(event.call_sid):
            return
        await self._terminate_call(event.call_sid)

    async def _on_ended(self, event: CallEnded) -> None:
        if not self._matches_active_call(event.call_sid):
            return
        await self._terminate_call(event.call_sid)

    async def _terminate_call(self, call_sid: str) -> None:
        self._call_sid = call_sid
        self._cancel_timers()
        await self._transition(OutboundCallState.ENDED)

    async def _on_voicemail(self, event: VoicemailDetected) -> None:
        event_call_sid = getattr(event, "call_sid", "")
        if event_call_sid and not self._matches_active_call(event_call_sid):
            return
        # When a fusion classifier is active, ignore raw AMD events (empty source)
        # but accept both fused and detector-sourced events.
        if self._expect_fused_voicemail and not event.source:
            return
        if event.result == "human" and self._state in _VOICEMAIL_ACCEPT_STATES:
            self._cancel_classification_timeout()
            await self._transition(OutboundCallState.HUMAN)
        elif event.result == "machine" and self._state in _VOICEMAIL_ACCEPT_STATES:
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
        if not self._matches_active_call(event.call_sid):
            return
        if self._state == OutboundCallState.CLASSIFYING:
            self._cancel_classification_timeout()
            await self._transition(OutboundCallState.SCREENING)

    async def _on_screening_timed_out(self, event: ScreeningTimedOut) -> None:
        if event.call_sid and not self._matches_active_call(event.call_sid):
            return
        if self._state == OutboundCallState.SCREENING:
            await self._transition(OutboundCallState.HUMAN)

    async def _on_stt_final(self, event: STTFinal) -> None:
        """Handle STTFinal for IVR detection (CLASSIFYING) and SCREENING → HUMAN."""
        text = event.text.strip()
        if not text:
            return

        # Skip non-inbound transcripts (bot's own speech fed back when
        # transcription_track="both").  Applies to all classification states.
        if event.track is not None and event.track != "inbound":
            return

        if self._state == OutboundCallState.CLASSIFYING:
            if self._classify_ivr_prompt(text):
                self._cancel_classification_timeout()
                await self._transition(OutboundCallState.IVR)
            elif self._classify_greeting(text) == "machine":
                # Short voicemail greetings (e.g. "Please leave a message")
                # pass is_conversational's word-count check but contain known
                # voicemail phrases — let the fusion classifier handle them
                # instead of misrouting to HUMAN.
                pass
            elif self._is_conversational(text, self._screening_patterns):
                self._cancel_classification_timeout()
                await self._transition(OutboundCallState.HUMAN)
            return

        if self._state == OutboundCallState.SCREENING:
            if self._is_conversational(text, self._screening_patterns):
                await self._transition(OutboundCallState.HUMAN)

        if (
            self._state == OutboundCallState.VOICEMAIL
            and self._voicemail_pickup_task is not None
            and not self._voicemail_pickup_task.done()
        ):
            # Exclude voicemail system prompts from triggering false human detection.
            if self._classify_greeting(text) == "machine":
                return
            if self._is_conversational(text, self._screening_patterns):
                self._cancel_voicemail_pickup_window()
                logger.info("Conversational speech during VOICEMAIL — transitioning to HUMAN")
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
