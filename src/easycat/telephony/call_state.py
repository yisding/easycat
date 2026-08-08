"""Outbound call state machine: coordinates AMD, screening, voicemail, and IVR detection."""

from __future__ import annotations

__all__ = [
    "SMART_TURN_SUPPRESS_STATES",
    "TERMINAL_CLASSIFICATION_STATES",
    "CallStateChanged",
    "ClassificationGate",
    "OutboundCallState",
    "OutboundCallStateMachine",
]

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from enum import Enum
from typing import TYPE_CHECKING, Any, NoReturn

from easycat._epoch import Epoch, Lease
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
from easycat.runtime.scope import BackgroundTaskScope

if TYPE_CHECKING:
    from easycat.telephony.screening import ScreeningPatternSet

logger = logging.getLogger(__name__)


def _raise_prefer_cancellation(errors: list[BaseException]) -> NoReturn:
    """Raise collected lifecycle failures, preferring caller cancellation."""
    primary = next(
        (error for error in errors if isinstance(error, asyncio.CancelledError)),
        errors[0],
    )
    secondary = next((error for error in errors if error is not primary), None)
    if secondary is not None:
        raise primary from secondary
    raise primary


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

# Tracks that identify human-side audio strongly enough to allow the optional
# live-voicemail pickup path.  Missing/unknown track metadata is intentionally
# not trusted because voicemail greetings and echoed outbound audio are
# attacker-controlled inputs once a call is classified as VOICEMAIL.
_INBOUND_STT_TRACKS = frozenset({"inbound", "inbound_track", "caller"})

_GATE_TIMEOUT_TASK = "classification_gate_timeout"
_CLASSIFICATION_TIMEOUT_TASK = "call_classification_timeout"
_MAX_DURATION_TASK = "call_max_duration"
_LATE_VOICEMAIL_TASK = "late_voicemail_window"
_VOICEMAIL_PICKUP_TASK = "voicemail_pickup_window"


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
        self._tasks = BackgroundTaskScope()
        self._timeout_epoch: Epoch[None] = Epoch(None)
        # Keep a direct handle after the timeout detaches from the scope so a
        # hard lifecycle reset (stop/new call/discard) can still cancel stale
        # replay work. Ordinary release intentionally uses only the scope and
        # therefore preserves an in-progress replay.
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
        self._timeout_epoch.bump(None)
        self._cancel_timeout()
        self._cancel_detached_timeout()
        self._buffer.clear()
        self._closed = False
        self._started = False
        self._hold_audio_playing = False

    def close(self) -> None:
        """Close the gate — start buffering TTS audio."""
        if not self._enabled:
            return
        self._timeout_epoch.bump(None)
        self._cancel_detached_timeout()
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
        return await self._replay_and_open(buffered)

    async def _replay_and_open(  # noqa: C901 invariant settlement keeps failures ordered
        self,
        buffered: list[TTSAudio],
        *,
        timeout: Lease[None] | None = None,
        include_sync_callback: bool = False,
    ) -> list[TTSAudio]:
        """Replay one dequeued batch and open this gate generation safely."""
        errors: list[BaseException] = []
        if include_sync_callback and self._on_flush and buffered:
            try:
                self._on_flush(buffered)
            except Exception as exc:  # noqa: BLE001 - callback invariant boundary
                errors.append(exc)
        # Replay while the gate is still closed. Invoke the async callback even
        # for an empty explicit flush: session wiring uses it to cancel hold
        # audio synthesized while classification was pending.
        try:
            if self._on_flush_async:
                await self._on_flush_async(buffered)
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - gate invariant
            errors.append(exc)
        if timeout is not None and not timeout.guard():
            if errors:
                _raise_prefer_cancellation(errors)
            return buffered
        # Drain frames that arrived during the async flush (e.g. TTS produced
        # by CallStateChanged subscribers), then open the gate even when replay
        # failed or the caller was cancelled. A committed HUMAN/UNKNOWN state
        # must never retain a closed gate with no release timeout.
        late = list(self._buffer)
        self._buffer.clear()
        self._closed = False
        if self._started:
            try:
                self._event_bus.unsubscribe(TTSAudio, self._on_tts_audio)
            except Exception as exc:  # noqa: BLE001 - unsubscribe invariant boundary
                errors.append(exc)
            finally:
                self._started = False
        # Replay late arrivals now that the gate is open.
        if self._on_flush_async and late:
            try:
                await self._on_flush_async(late)
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - gate invariant
                errors.append(exc)
        if errors:
            _raise_prefer_cancellation(errors)
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
        self._timeout_epoch.bump(None)
        self._cancel_timeout()
        self._cancel_detached_timeout()
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
            asyncio.get_running_loop()
        except RuntimeError:
            return
        timeout = self._timeout_epoch.capture()
        self._timeout_task = self._tasks.create_task(
            _GATE_TIMEOUT_TASK,
            self._timeout_coro(timeout),
            replace=True,
        )

    def _cancel_timeout(self) -> None:
        self._tasks.cancel(_GATE_TIMEOUT_TASK)

    def _cancel_detached_timeout(self) -> None:
        task = self._timeout_task
        self._timeout_task = None
        if task is None or task.done():
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not current:
            task.cancel()

    async def _timeout_coro(self, timeout: Lease[None]) -> None:
        await asyncio.sleep(self._timeout_s)
        if not timeout.guard():
            return
        # Once replay begins, detach this task from cancellation ownership.
        # The buffer is about to be dequeued, so a concurrent classification
        # signal must not cancel the flush and permanently drop its remainder.
        # BackgroundTaskScope recognizes the current task and only detaches it.
        self._tasks.cancel(_GATE_TIMEOUT_TASK)
        if not timeout.guard():
            return
        if self._closed:
            self._hold_audio_playing = False
            buffered = list(self._buffer)
            self._buffer.clear()
            await self._replay_and_open(
                buffered,
                timeout=timeout,
                include_sync_callback=True,
            )


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
        self._transition_epoch = 0
        self._transition_lock = asyncio.Lock()
        self._transition_context: ContextVar[asyncio.Task[Any] | None] = ContextVar(
            f"easycat-call-transition-{id(self):x}",
            default=None,
        )
        self._active_transition_owner: asyncio.Task[Any] | None = None
        self._started = False
        self._timers = BackgroundTaskScope()
        self._max_duration_hangup: Callable[[str], Awaitable[None]] | None = None

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
    def call_sid(self) -> str:
        """The provider identifier for the currently accepted call."""
        return self._call_sid

    def accepts_call_initiation(self, call_sid: str, observer_call_sid: str) -> bool:
        """Return whether a helper may adopt ``call_sid`` as a new call.

        Outbound helpers subscribe to :class:`CallInitiated` on both sides of
        this state machine in the event-bus ordering.  ``observer_call_sid``
        lets the same predicate work before the state machine adopts a new
        sequential SID and after it has done so, while rejecting duplicate and
        overlapping initiations in either position.
        """
        if not call_sid or call_sid == observer_call_sid:
            return False
        if not self._call_sid or self._state == OutboundCallState.ENDED:
            return True
        return call_sid == self._call_sid and self._state == OutboundCallState.INITIATING

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
        self._timers.cancel()

    # ── New-call reset ─────────────────────────────────────────────

    async def _on_call_initiated(self, event: CallInitiated) -> None:
        """Reset the state machine when a new outbound call is placed.

        This allows a single session to handle sequential outbound calls
        without getting stuck in the ENDED state from a previous call.
        """
        if not self.accepts_call_initiation(event.call_sid, self._call_sid):
            if event.call_sid and event.call_sid != self._call_sid:
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
        current_task = asyncio.current_task()
        transition_owner = self._transition_context.get()
        if transition_owner is not None and current_task is transition_owner:
            await self._transition_owned(new_state)
            return
        if transition_owner is not None and transition_owner is self._active_transition_owner:
            # A task spawned by an inline state observer inherits the active
            # ContextVar but is not structurally joined to this transition.
            # Letting it bypass the lock can race the outer invariant
            # settlement; making it wait can deadlock when the observer awaits
            # the child. Require observers to await transition() directly in
            # their own task, where reentry is safely serialized inline. A
            # stale inherited owner (that transition already settled) takes
            # the ordinary serialized path below instead.
            raise RuntimeError(
                "state observers must await transition() directly; "
                "spawned transition tasks are not supported"
            )
        async with self._transition_lock:
            if current_task is None:  # pragma: no cover - coroutine has an asyncio task
                await self._transition_owned(new_state)
                return
            context_token = self._transition_context.set(current_task)
            self._active_transition_owner = current_task
            try:
                await self._transition_owned(new_state)
            finally:
                self._active_transition_owner = None
                self._transition_context.reset(context_token)

    async def _transition_owned(self, new_state: OutboundCallState) -> None:
        """Commit and settle one transition while its owner is serialized."""
        if self._state == new_state:
            return
        old = self._state
        self._state = new_state
        self._transition_epoch += 1
        transition_epoch = self._transition_epoch
        dispatch_error: BaseException | None = None
        try:
            await self._event_bus.emit(
                CallStateChanged(old=old, new=new_state, call_sid=self._call_sid)
            )
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - invariant boundary
            dispatch_error = exc

        invariant_errors = (
            await self._settle_transition_invariants(new_state)
            if self._transition_epoch == transition_epoch
            else []
        )
        errors = ([dispatch_error] if dispatch_error is not None else []) + invariant_errors
        if errors:
            _raise_prefer_cancellation(errors)

    async def _settle_transition_invariants(
        self,
        new_state: OutboundCallState,
    ) -> list[BaseException]:
        """Settle every invariant after state commit, retaining the first failures."""
        invariant_errors: list[BaseException] = []
        try:
            self._update_smart_turn_suppression()
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - invariant boundary
            invariant_errors.append(exc)
        try:
            await self._settle_transition_gate(new_state)
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - invariant boundary
            invariant_errors.append(exc)
        try:
            self._start_transition_windows(new_state)
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - invariant boundary
            invariant_errors.append(exc)
        return invariant_errors

    async def _settle_transition_gate(
        self,
        new_state: OutboundCallState,
    ) -> None:
        """Release or discard classified opener audio after a committed transition."""
        if self._gate.is_buffering and new_state != OutboundCallState.CLASSIFYING:
            if new_state in {OutboundCallState.HUMAN, OutboundCallState.UNKNOWN}:
                await self._gate.flush_and_release()
            else:
                await self._gate.discard()

    def _start_transition_windows(self, new_state: OutboundCallState) -> None:
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
            # Once the answered event is accepted, these timers are owned by the
            # committed CLASSIFYING lifecycle even if a public state observer
            # raises while the transition is being dispatched.
            self._start_classification_timeout()
            self._start_max_duration_timer()
            await self._transition(OutboundCallState.CLASSIFYING)

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
            and self._timers.active(_LATE_VOICEMAIL_TASK)
        ):
            # Late voicemail detection: beep or long monologue after HUMAN.
            self._cancel_late_voicemail_window()
            logger.info("Late voicemail detected during HUMAN state — transitioning to VOICEMAIL")
            await self._transition(OutboundCallState.VOICEMAIL)
        elif (
            event.result == "human"
            and self._state == OutboundCallState.VOICEMAIL
            and self._timers.active(_VOICEMAIL_PICKUP_TASK)
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

    async def _handle_classifying_stt_final(self, text: str) -> None:
        if self._classify_ivr_prompt(text):
            self._cancel_classification_timeout()
            await self._transition(OutboundCallState.IVR)
            return
        if self._classify_greeting(text) == "machine":
            # Short voicemail greetings (e.g. "Please leave a message")
            # pass is_conversational's word-count check but contain known
            # voicemail phrases -- let the fusion classifier handle them
            # instead of misrouting to HUMAN.
            return
        if self._expect_fused_voicemail:
            # When AMD/STT fusion is active, inbound STT is one of the
            # classifier inputs rather than a terminal human signal.
            # Do not let short callee-controlled phrases bypass the
            # classification gate before fusion can classify voicemail.
            return
        if self._is_conversational(text, self._screening_patterns):
            self._cancel_classification_timeout()
            await self._transition(OutboundCallState.HUMAN)

    async def _on_stt_final(self, event: STTFinal) -> None:
        """Handle STTFinal for IVR detection (CLASSIFYING) and SCREENING → HUMAN."""
        text = event.text.strip()
        if not text:
            return

        # Skip non-inbound transcripts (bot's own speech fed back when
        # transcription_track="both").  Applies to all classification states.
        # A track-less event (track is None) is accepted because the Twilio
        # media transport already drops outbound frames at ingest; an explicit
        # track must be one of the trusted inbound labels.  Sharing the
        # ``_INBOUND_STT_TRACKS`` whitelist keeps this filter consistent with
        # the voicemail-pickup guard below so "inbound_track"/"caller" reach
        # every classification state rather than only "inbound".
        if event.track is not None and event.track.lower() not in _INBOUND_STT_TRACKS:
            return

        if self._state == OutboundCallState.CLASSIFYING:
            await self._handle_classifying_stt_final(text)
            return

        if self._state == OutboundCallState.SCREENING:  # noqa: SIM102 nested branches preserve decision context
            if self._is_conversational(text, self._screening_patterns):
                await self._transition(OutboundCallState.HUMAN)

        if self._state == OutboundCallState.VOICEMAIL and self._timers.active(
            _VOICEMAIL_PICKUP_TASK
        ):
            if not self._is_trusted_inbound_stt(event):
                return
            # Exclude voicemail system prompts from triggering false human detection.
            if self._classify_greeting(text) == "machine":
                return
            if self._is_conversational(text, self._screening_patterns):
                self._cancel_voicemail_pickup_window()
                logger.info("Conversational speech during VOICEMAIL — transitioning to HUMAN")
                await self._transition(OutboundCallState.HUMAN)

    @staticmethod
    def _is_trusted_inbound_stt(event: STTFinal) -> bool:
        track = event.track
        return isinstance(track, str) and track.lower() in _INBOUND_STT_TRACKS

    # ── Timers ────────────────────────────────────────────────────

    def _start_classification_timeout(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timers.create_task(
            _CLASSIFICATION_TIMEOUT_TASK,
            self._classification_timeout_coro(),
            replace=True,
        )

    def _cancel_classification_timeout(self) -> None:
        self._timers.cancel(_CLASSIFICATION_TIMEOUT_TASK)

    async def _classification_timeout_coro(self) -> None:
        await asyncio.sleep(self._classification_timeout_s)
        if self._state == OutboundCallState.CLASSIFYING:
            await self._transition(OutboundCallState.UNKNOWN)

    def _start_max_duration_timer(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timers.create_task(
            _MAX_DURATION_TASK,
            self._max_duration_coro(),
            replace=True,
        )

    async def _max_duration_coro(self) -> None:
        await asyncio.sleep(self._max_call_duration_s)
        if self._state != OutboundCallState.ENDED:
            if self._max_duration_hangup is not None:
                try:
                    await self._max_duration_hangup(self._call_sid)
                except Exception:
                    logger.exception("Maximum-duration Twilio hangup failed")
            await self._transition(OutboundCallState.ENDED)
            await self._event_bus.emit(
                CallEnded(call_sid=self._call_sid, disposition="max_duration")
            )

    def set_max_duration_hangup(
        self,
        callback: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        """Set the hard-hangup callback run before terminal lifecycle handlers."""
        self._max_duration_hangup = callback

    # ── Late voicemail window ────────────────────────────────────

    def _start_late_voicemail_window(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timers.create_task(
            _LATE_VOICEMAIL_TASK,
            self._late_voicemail_coro(),
            replace=True,
        )

    def _cancel_late_voicemail_window(self) -> None:
        self._timers.cancel(_LATE_VOICEMAIL_TASK)

    async def _late_voicemail_coro(self) -> None:
        """After the window expires, stop accepting late voicemail signals."""
        await asyncio.sleep(self._late_voicemail_window_s)

    # ── Voicemail pickup window ─────────────────────────────────

    def _start_voicemail_pickup_window(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timers.create_task(
            _VOICEMAIL_PICKUP_TASK,
            self._voicemail_pickup_coro(),
            replace=True,
        )

    def _cancel_voicemail_pickup_window(self) -> None:
        self._timers.cancel(_VOICEMAIL_PICKUP_TASK)

    async def _voicemail_pickup_coro(self) -> None:
        """After the window expires, stop accepting voicemail pickup signals."""
        await asyncio.sleep(self._voicemail_pickup_window_s)
