"""Turn-taking state machine for managing conversation turns.

The TurnManager consumes both VAD events and raw audio frames to manage
turn state transitions. It maintains a rolling pre-roll buffer so that
audio before the VAD trigger can be prepended to the STT capture stream.

States:
  - Idle: waiting for speech
  - UserSpeaking: VAD detected speech, capturing audio
  - UserPaused: silence detected, waiting for end-of-turn timeout
  - Processing: user turn complete, waiting for agent + TTS
  - BotSpeaking: TTS audio playing back

Supports two modes:
  - VAD mode (default): automatic turn detection via VAD events
  - Push-to-talk mode: manual turn start/end via end_turn()
"""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from numbers import Integral, Real
from typing import Any, Literal
from uuid import uuid4

from easycat._epoch import Epoch, Lease
from easycat._turn_context import TurnContext
from easycat.audio_format import AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    BotStartedSpeaking,
    BotStoppedSpeaking,
    EventBus,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
    _mark_turn_started_observation,
)
from easycat.runtime.scope import RuntimeScope
from easycat.smart_turn import SmartTurnProvider, _validate_probability_threshold

logger = logging.getLogger(__name__)


def _validate_non_negative_finite_number(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite non-negative number")


def _validate_positive_integer(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


class TurnManagerState(enum.Enum):
    """Turn-taking state machine states."""

    IDLE = "idle"
    USER_SPEAKING = "user_speaking"
    USER_PAUSED = "user_paused"
    PROCESSING = "processing"
    BOT_SPEAKING = "bot_speaking"


@dataclass(frozen=True, slots=True)
class TurnPublication:
    """Private handoff from a turn producer to Session lifecycle ownership."""

    source: Literal["voice", "application", "text", "hand_built"]
    session_id: str | None
    turn_id: str
    cancel_token: CancelToken | None
    activity: Lease[TurnManagerState] | None
    identity: Lease[TurnContext | None] | None = None
    admission_rejected: bool = False


class TurnMode(enum.Enum):
    """Turn detection mode."""

    VAD = "vad"
    PUSH_TO_TALK = "push_to_talk"


def _normalize_turn_mode(mode: object) -> TurnMode:
    """Normalize serialized enum values and reject unknown turn policies."""
    if isinstance(mode, TurnMode):
        return mode
    if isinstance(mode, str):
        try:
            return TurnMode(mode)
        except ValueError:
            pass
    allowed = sorted(candidate.value for candidate in TurnMode)
    raise ValueError(f"Invalid mode={mode!r}. Must be a TurnMode or one of {allowed}.")


@dataclass
class TurnManagerConfig:
    """Configuration for TurnManager."""

    # Grace period after VAD reports speech stopped before the turn ends.
    # Keep this comfortably above ``VADConfig.min_speech_duration_ms``: on the
    # plain-VAD path (no smart-turn) the only event that can cancel a pending
    # endpoint is a *confirmed* VADStartSpeaking, which the default VAD emits
    # only after 250 ms of continuous resumed speech plus frame quantization.
    # This is also the fallback grace a smart-turn "incomplete" verdict
    # grants, so a semantically mid-sentence user keeps the full window.
    end_of_turn_silence_ms: int = 500
    # Shorter silence timeout used when STT finalizes text with terminal
    # punctuation during the pause. None disables punctuation-aware
    # endpointing. Smart-turn incomplete/error decisions still receive the
    # full end_of_turn_silence_ms grace period.
    punctuated_end_of_turn_silence_ms: int | None = 200
    # Silence budget, after VAD stop, before finalizing the current STT segment.
    # 0 means commit the segment immediately when VAD reports a pause.
    #
    # NOTE: This field is *not* read by TurnManager itself.  It is consumed by
    # ``Session``, which forwards it to the ``STTCommitter`` as
    # ``segment_silence_ms`` (see ``session/_builder.py``, which wires it into
    # ``STTCommitter``, and ``session/_stt_committer.py``, which reads it).
    # Setting it on a bare ``TurnManager`` (constructed without a Session)
    # therefore has no effect.  It lives here so the single
    # ``TurnManagerConfig`` object stays the one place callers tune turn/STT
    # segmentation timing.
    stt_segment_silence_ms: int = 0
    # Pre-roll buffer duration in milliseconds. The default covers the
    # 250 ms VAD speech-confirmation gate plus 200 ms of onset context so the
    # leading consonant is retained even with frame quantization/model attack.
    pre_roll_ms: int = 450
    # Turn detection mode
    mode: TurnMode = TurnMode.VAD
    # Optional endpoint detector for smart turn-taking.
    # When set, TurnManager queries it on silence to decide whether
    # to end the turn immediately or wait the full timeout.
    endpoint_detector: SmartTurnProvider | None = None
    # Maximum captured turn-audio window in milliseconds.  Turn audio is retained
    # for pre-roll priming and smart-turn endpoint detection, so it must stay
    # bounded even if a client keeps a speech/noise turn active indefinitely.
    max_turn_audio_ms: int = 8000
    # Maximum number of retained chunks for the pre-roll and active-turn windows.
    # These count caps bound memory even for very small positive-duration frames.
    max_pre_roll_chunks: int = 512
    max_turn_audio_chunks: int = 4096
    # Optional decision threshold applied to the detector's *probability*.
    # When set (not None), TurnManager ends the turn when
    # ``result.probability > endpoint_threshold`` instead of trusting the
    # provider-precomputed ``result.prediction``.  This lets callers tune
    # endpoint sensitivity without reconstructing the provider.  When None
    # (default), the provider's own ``prediction`` int is used, preserving
    # back-compat.  The comparison is strict-greater, matching the provider:
    # ``probability == endpoint_threshold`` stays incomplete.
    #
    # Precedence: this manager-level threshold *wins* over the provider's
    # ``SmartTurnConfig.threshold`` whenever it is set.  When you build a
    # session via ``EasyConfig``/``create_session`` and leave this ``None``,
    # the wiring derives it from ``SmartTurnConfig.threshold`` so the single
    # ``smart_turn.threshold`` knob is authoritative and the two cannot
    # diverge by accident; setting both to different values logs a warning.
    endpoint_threshold: float | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Normalize and validate mutable turn policy before runtime use."""
        # Accept the serialized values used by YAML/JSON manifests, but store
        # the enum so every downstream comparison has one canonical shape.
        self.mode = _normalize_turn_mode(self.mode)
        for name, value in (
            ("end_of_turn_silence_ms", self.end_of_turn_silence_ms),
            ("stt_segment_silence_ms", self.stt_segment_silence_ms),
            ("pre_roll_ms", self.pre_roll_ms),
            ("max_turn_audio_ms", self.max_turn_audio_ms),
        ):
            _validate_non_negative_finite_number(name, value)
        if self.punctuated_end_of_turn_silence_ms is not None:
            _validate_non_negative_finite_number(
                "punctuated_end_of_turn_silence_ms",
                self.punctuated_end_of_turn_silence_ms,
            )
        _validate_positive_integer("max_pre_roll_chunks", self.max_pre_roll_chunks)
        _validate_positive_integer("max_turn_audio_chunks", self.max_turn_audio_chunks)
        if self.endpoint_threshold is not None:
            _validate_probability_threshold("endpoint_threshold", self.endpoint_threshold)


class TurnManager:
    """Manages conversation turn state based on VAD events and raw audio frames.

    The TurnManager subscribes to VAD events (via on_vad_event) and receives
    raw audio frames (via on_audio_frame) to:
      - Maintain a rolling pre-roll buffer of recent audio
      - Track turn state transitions
      - Emit TurnStarted/TurnEnded events via the EventBus
      - Handle barge-in when speech is detected during bot playback
      - Support push-to-talk mode for manual turn control

    Responsibility boundary: TurnManager emits turn.ended, NOT stt.final.
    The Session handles calling end_stream() on the STT provider.
    """

    # Trailing audio window (ms) handed to the endpoint detector.  Smart-turn
    # models only consume the last few seconds of speech, so bounding the
    # window keeps detection latency constant regardless of turn length.
    _DETECTOR_WINDOW_MS: float = 8000.0

    def __init__(
        self,
        event_bus: EventBus,
        config: TurnManagerConfig | None = None,
        cancel_turn_callback: Any | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._config = config or TurnManagerConfig()
        self._config.validate()

        # Callback for barge-in: expected to perform the audible cutoff and
        # arrange old-turn cleanup. The callback is the sole emitter of the
        # Interruption event. Phase 4 of the session decomposition installs it
        # late (after the CancelOrchestrator exists), so it is also settable
        # post-construction via :meth:`set_cancel_callback`.
        self._cancel_turn_callback = cancel_turn_callback

        # Activity phase. Every change, including an IDLE reset while already
        # idle, routes through ``_transition`` after this initial publication.
        self._activity: Epoch[TurnManagerState] = Epoch(TurnManagerState.IDLE)
        self._mode = self._config.mode

        # Pre-roll audio buffer (rolling window of recent audio frames)
        self._pre_roll_buffer: deque[AudioChunk] = deque()
        self._pre_roll_duration_ms: float = 0.0

        # Captured audio for the current turn (pre-roll + speech audio)
        self._turn_audio: deque[AudioChunk] = deque()
        self._turn_audio_duration_ms: float = 0.0

        # Silence timeout tracking
        self._silence_start_time: float | None = None
        self._silence_timer_task: asyncio.Task[None] | None = None
        self._silence_timer_tasks: set[asyncio.Task[None]] = set()
        self._shutting_down = False
        self._punctuated_transcript_event = asyncio.Event()
        self._pause_epoch: Epoch[None] = Epoch(None)

        # Cancel token for the current turn
        self._cancel_token: CancelToken | None = None
        # Claim token for the asynchronous barge-in cutoff window. Manual PTT
        # and VAD entry points can run in different tasks; without a synchronous
        # claim both can observe BOT_SPEAKING/PROCESSING, await the same cutoff,
        # then publish competing successor turns.
        self._barge_in_claim: object | None = None

        # Optional endpoint detector (smart-turn model)
        self._endpoint_detector: SmartTurnProvider | None = self._config.endpoint_detector

        # Optional TurnStage wrapper that journals each detection call.
        # When bound, ``detect`` goes through ``stage.execute()`` so the
        # decision + audio window land in the journal automatically.
        self._endpoint_stage: Any = None
        self._endpoint_ctx_getter: Any = None
        # Optional journal hook for state-transition records.  Session
        # calls :meth:`bind_journal` during wiring; TurnManager stays
        # self-contained (no hard dep on Session) and the hook is a
        # simple callable so downstream consumers can wire their own
        # recorder without pulling in the Session machinery.
        self._journal_state_change: Any = None
        self._endpoint_turn_getter: Any = None
        self._turn_publication_callback: (
            Callable[[TurnPublication], Awaitable[TurnPublication]] | None
        ) = None

        # Correlation identifiers
        self._session_id: str | None = None
        self._turn_counter = 0
        self._current_turn_id: str | None = None

    # ── Properties ──────────────────────────────────────────────

    @property
    def state(self) -> TurnManagerState:
        return self._activity.capture().value

    @property
    def _state(self) -> TurnManagerState:
        """Compatibility view for focused harnesses that predate activity leases."""
        return self.state

    @_state.setter
    def _state(self, state: TurnManagerState) -> None:
        self._transition(
            state,
            reason="compatibility_state_set",
            observe=False,
        )

    def capture_activity(self) -> Lease[TurnManagerState]:
        """Capture the manager activity generation and state atomically."""
        return self._activity.capture()

    @property
    def mode(self) -> TurnMode:
        return self._mode

    @property
    def cancel_token(self) -> CancelToken | None:
        return self._cancel_token

    def capture_pause(self) -> Lease[None]:
        """Capture the exact current pause identity."""
        return self._pause_epoch.capture()

    @property
    def turn_audio(self) -> list[AudioChunk]:
        """Snapshot of audio chunks captured for the current turn (with pre-roll).

        Returns a *copy* of the internal list so callers can safely iterate it
        while awaiting (e.g. priming STT chunk-by-chunk) without risking
        mutation if a future caller feeds ``on_audio_frame`` from another task.
        The list is small (pre-roll + turn frames), so the copy is negligible.
        """
        return list(self._turn_audio)

    def discard_buffered_audio(self) -> None:
        """Drop pre-decision audio when capture changes from denied to allowed."""
        self._turn_audio.clear()
        self._turn_audio_duration_ms = 0.0
        self._pre_roll_buffer.clear()
        self._pre_roll_duration_ms = 0.0

    @property
    def endpoint_detector(self) -> SmartTurnProvider | None:
        """The smart-turn endpoint detector this manager uses, if any.

        Public accessor so Session can wire the ``TurnStage`` without
        reaching into ``_config``; returns the same ``_endpoint_detector``
        the manager consults internally (single source of truth).
        """
        return self._endpoint_detector

    def bind_session(self, session_id: str) -> None:
        """Bind a stable session identifier used for emitted events."""
        self._session_id = session_id

    def set_cancel_callback(self, callback: Any | None) -> None:
        """Install (or replace) the barge-in cancel callback.

        Phase 4 of the session decomposition constructs TurnManager
        before the CancelOrchestrator exists, so the callback is
        installed late.  The callback is expected to be awaitable and
        to return ``False`` when barge-in should be suppressed.
        """
        self._cancel_turn_callback = callback

    def bind_journal_hook(
        self,
        hook: Any,
    ) -> None:
        """Install a callable that journals each turn-state transition.

        The hook is called as ``hook(from_state, to_state, reason, turn_id)``
        at every state change.  Installed by Session during wiring.
        Keeps TurnManager itself free of a hard journal dependency so
        tests that drive it directly can skip the hook.
        """
        self._journal_state_change = hook

    def bind_turn_publication(
        self,
        callback: Callable[[TurnPublication], Awaitable[TurnPublication]],
    ) -> None:
        """Bind the private lifecycle callback that precedes TurnStarted observation."""
        self._turn_publication_callback = callback

    def _transition(
        self,
        to_state: TurnManagerState,
        *,
        reason: str,
        observe: bool = True,
    ) -> Lease[TurnManagerState]:
        """Publish ``to_state`` and optionally log and journal the transition.

        This is the sole activity Epoch writer. Every call bumps the activity
        generation, including same-state IDLE resets. Ordinary lifecycle
        transitions emit a ``turn_state_changed`` record so bundles can answer
        "why did the turn end when it did" from the journal alone. Reset and
        compatibility setup preserve their historical silent behavior with
        ``observe=False`` while still invalidating outstanding activity leases.

        The debug log line is derived from the real ``from_state`` /
        ``to_state`` / ``reason`` so it can never disagree with the journal
        record (callers no longer pass a hardcoded ``log_msg`` that could
        drift from the actual transition — e.g. a barge-in from PROCESSING
        used to falsely log a from-state of BOT_SPEAKING).
        """
        from_state = self.state
        generation = self._activity.bump(to_state)
        lease = self._activity.capture()
        if __debug__:
            assert lease.generation == generation
            assert lease.value is to_state
            assert lease.is_current()
        if not observe:
            return lease
        logger.debug("Turn: %s -> %s (%s)", from_state.value, to_state.value, reason)
        hook = self._journal_state_change
        if hook is not None:
            try:
                hook(from_state, to_state, reason, self._current_turn_id)
            except Exception:
                logger.debug("journal state-change hook raised", exc_info=True)
        return lease

    def bind_endpoint_stage(
        self,
        stage: Any,
        *,
        run_ctx_getter: Any,
        turn_getter: Any,
    ) -> None:
        """Route smart-turn ``detect`` calls through a TurnStage wrapper.

        ``run_ctx_getter`` / ``turn_getter`` are called at detection time
        so each decision lands in the journal under the right session +
        turn id without holding stale references.
        """
        self._endpoint_stage = stage
        self._endpoint_ctx_getter = run_ctx_getter
        self._endpoint_turn_getter = turn_getter

    # ── Audio frame handling ────────────────────────────────────

    def on_audio_frame(self, chunk: AudioChunk) -> None:
        """Feed a raw audio frame to the TurnManager.

        Called for every incoming audio chunk so the TurnManager can:
          - Maintain the rolling pre-roll buffer
          - Capture audio during active speech
        """
        duration_ms = chunk.duration_ms
        if not chunk.data or duration_ms <= 0:
            return

        # Always maintain the pre-roll buffer
        self._pre_roll_buffer.append(chunk)
        self._pre_roll_duration_ms += duration_ms
        self._trim_pre_roll_buffer()

        # If user is speaking, capture the audio
        if self._state in (TurnManagerState.USER_SPEAKING, TurnManagerState.USER_PAUSED):
            self._append_turn_audio(chunk, duration_ms)

    def _trim_pre_roll_buffer(self) -> None:
        """Keep the pre-roll window bounded by duration and chunk count."""
        while (
            self._pre_roll_duration_ms > self._config.pre_roll_ms
            or len(self._pre_roll_buffer) > self._config.max_pre_roll_chunks
        ) and len(self._pre_roll_buffer) > 1:
            removed = self._pre_roll_buffer.popleft()
            self._pre_roll_duration_ms -= removed.duration_ms
        if self._pre_roll_duration_ms < 0:
            self._pre_roll_duration_ms = 0.0

    def _append_turn_audio(self, chunk: AudioChunk, duration_ms: float) -> None:
        """Append active-turn audio while bounding retained memory."""
        self._turn_audio.append(chunk)
        self._turn_audio_duration_ms += duration_ms
        self._trim_turn_audio()

    def _trim_turn_audio(self) -> None:
        """Keep retained active-turn audio bounded by duration and chunk count."""
        while (
            self._turn_audio_duration_ms > self._config.max_turn_audio_ms
            or len(self._turn_audio) > self._config.max_turn_audio_chunks
        ) and len(self._turn_audio) > 1:
            removed = self._turn_audio.popleft()
            self._turn_audio_duration_ms -= removed.duration_ms
        if self._turn_audio_duration_ms < 0:
            self._turn_audio_duration_ms = 0.0

    # ── VAD event handling ──────────────────────────────────────

    async def on_vad_event(self, event: VADStartSpeaking | VADStopSpeaking) -> None:
        """Handle a VAD event. Called by the pipeline when VAD emits events.

        In push-to-talk mode, VAD events are ignored.
        """
        if self._shutting_down or self._mode == TurnMode.PUSH_TO_TALK:
            return

        if isinstance(event, VADStartSpeaking):
            await self._handle_speech_start()
        elif isinstance(event, VADStopSpeaking):
            await self._handle_speech_stop()

    def on_stt_final(self, text: str, *, pause: Lease[None]) -> None:
        """Notify the originating pause that STT finalized a complete sentence.

        Only terminal punctuation from the active pause can shorten its fixed
        endpoint timer. The lease guard prevents a delayed segment final
        from an earlier pause from leaking into a later pause.
        """
        if self._state != TurnManagerState.USER_PAUSED or not pause.guard():
            return
        normalized = text.rstrip().rstrip("\"'”’)]}")
        if normalized.endswith(("...", "…")):
            return
        if normalized.endswith((".", "!", "?", "。", "！", "？", "．")):
            self._punctuated_transcript_event.set()

    async def _handle_speech_start(self) -> None:
        """Handle VAD speech start."""
        if self._state == TurnManagerState.BOT_SPEAKING:
            # Barge-in: user interrupted the bot
            await self._handle_barge_in()
            return

        if self._state == TurnManagerState.PROCESSING:
            # User spoke again while agent is processing — treat as barge-in
            # to cancel the stale response and start a fresh turn.
            await self._handle_barge_in()
            return

        if self._state == TurnManagerState.USER_PAUSED:
            # Speech resumed before timeout — cancel silence timer
            self._cancel_silence_timer()
            self._transition(
                TurnManagerState.USER_SPEAKING,
                reason="speech_resumed",
            )
            return

        if self._state == TurnManagerState.IDLE:
            # New turn starting
            await self._begin_turn("vad_speech_start")

    def _flush_pre_roll_into_turn_audio(self) -> None:
        """Move the pre-roll buffer into the active turn's audio and trim it."""
        self._turn_audio = deque(self._pre_roll_buffer)
        self._turn_audio_duration_ms = self._pre_roll_duration_ms
        self._trim_turn_audio()
        self._pre_roll_buffer.clear()
        self._pre_roll_duration_ms = 0.0

    async def _begin_turn(self, reason: str, *, cancel_previous_token: bool = False) -> None:
        """Start a fresh user turn: the shared turn-start bookkeeping.

        Canonical order (matching the historic VAD-IDLE path): flush the
        pre-roll buffer into ``turn_audio`` *before* the state transition and
        ``TurnStarted`` emit, so the turn's audio is populated the moment the
        turn becomes observable.

        ``cancel_previous_token`` is only set on the barge-in path: when the
        barge-in interrupts a PROCESSING turn there is an in-flight agent run
        bound to the prior token; cancelling it prevents a stale response from
        leaking through once the new turn has started.  The VAD-IDLE and manual
        (push-to-talk) paths start from a state with no live in-flight turn, so
        they leave the (already-detached) prior token alone.
        """
        if self._shutting_down:
            return
        if cancel_previous_token and self._cancel_token is not None:
            self._cancel_token.cancel()
        cancel_token = CancelToken()
        self._cancel_token = cancel_token
        self._flush_pre_roll_into_turn_audio()
        self._turn_counter += 1
        turn_id = f"turn-{self._turn_counter:04d}-{uuid4().hex[:8]}"
        self._current_turn_id = turn_id
        activity = self._transition(
            TurnManagerState.USER_SPEAKING,
            reason=reason,
        )
        publication = TurnPublication(
            source="voice",
            session_id=self._session_id,
            turn_id=turn_id,
            cancel_token=cancel_token,
            activity=activity,
        )
        callback = self._turn_publication_callback
        if callback is not None:
            publication = await callback(publication)
            if __debug__:
                assert publication.activity is activity
                assert publication.cancel_token is cancel_token
                if publication.identity is not None:
                    assert publication.identity.value is not None
                    assert publication.identity.value.id == turn_id
            if publication.admission_rejected:
                # The private Session publication can reject admission when a
                # predecessor provider operation remains lifecycle-owned past
                # its timeout. Roll back the manager epoch and token instead
                # of exposing a TurnStarted that has no Session TurnContext or
                # active STT stream behind it.
                if activity.guard():
                    self.reset()
                return
        await self._event_bus.emit(
            _mark_turn_started_observation(
                TurnStarted(session_id=self._session_id, turn_id=turn_id)
            )
        )

    async def _complete_user_turn(self, reason: str) -> None:
        """Transition to processing and emit the correlated turn-end event."""
        if self._shutting_down:
            return
        self._transition(
            TurnManagerState.PROCESSING,
            reason=reason,
        )
        await self._event_bus.emit(
            TurnEnded(session_id=self._session_id, turn_id=self._current_turn_id)
        )

    async def _handle_speech_stop(self) -> None:
        """Handle VAD speech stop — transition to UserPaused and start timer."""
        if self._state != TurnManagerState.USER_SPEAKING:
            return

        self._cancel_silence_timer()
        self._silence_start_time = time.monotonic()
        self._pause_epoch.bump(None)
        pause = self._pause_epoch.capture()
        self._punctuated_transcript_event.clear()
        self._transition(
            TurnManagerState.USER_PAUSED,
            reason="vad_silence",
        )

        # Bind the timer to this exact pause. A detector is third-party code
        # and may suppress cancellation, so state alone cannot distinguish an
        # old timer from a newer pause after speech resumes.
        timer = asyncio.create_task(self._silence_timeout(pause))
        self._silence_timer_task = timer
        self._silence_timer_tasks.add(timer)
        timer.add_done_callback(self._silence_timer_tasks.discard)
        timer.add_done_callback(RuntimeScope.log_task_exception)

    def _detector_audio_window(self) -> list[AudioChunk]:
        """Return the trailing audio the endpoint detector should consume.

        Smart-turn models only look at the most recent few seconds of speech,
        so we bound the window to the trailing ``_DETECTOR_WINDOW_MS`` instead
        of the whole turn.  This keeps detection latency roughly constant
        regardless of turn length (an unbounded window made a long turn slow to
        score, which in turn ate into the post-pause grace budget).
        """
        chunks = self._turn_audio
        if not chunks:
            return []
        budget_ms = self._DETECTOR_WINDOW_MS
        window: deque[AudioChunk] = deque()
        acc = 0.0
        for chunk in reversed(chunks):
            window.appendleft(chunk)
            acc += chunk.duration_ms
            if acc >= budget_ms:
                break
        return list(window)

    async def _silence_timeout(self, pause: Lease[None]) -> None:
        """Wait for end-of-turn silence timeout, then transition to Processing.

        When an endpoint detector is configured, it is queried first.  If the
        detector predicts "complete", the turn ends immediately.  If it predicts
        "incomplete" (or raises an error), falls back to the normal sleep.

        The detector's own latency is **not** subtracted from the grace budget
        on the "incomplete" path: a model that says "still talking" must grant
        the user the full ``end_of_turn_silence_ms`` grace, and a slow detector
        must never be able to nullify its own "incomplete" verdict by ending
        the turn immediately.
        """
        try:
            punctuated_endpoint = False
            detector = self._endpoint_detector
            detector_attempted = detector is not None and bool(self._turn_audio)
            if detector_attempted and detector is not None:
                try:
                    if (
                        self._endpoint_stage is not None
                        and self._endpoint_ctx_getter is not None
                        and self._endpoint_turn_getter is not None
                    ):
                        result = await self._endpoint_stage.execute(
                            self._detector_audio_window(),
                            self._endpoint_ctx_getter(),
                            self._endpoint_turn_getter(),
                        )
                    else:
                        result = await detector.detect(self._detector_audio_window())
                    logger.debug(
                        "Smart-turn prediction=%d probability=%.3f",
                        result.prediction,
                        result.probability,
                    )
                    # When a manager-level threshold is configured, decide on
                    # the raw probability (strict-greater) so endpoint
                    # sensitivity is tunable without rebuilding the provider.
                    # Otherwise trust the provider's precomputed prediction.
                    if self._config.endpoint_threshold is not None:
                        is_complete = result.probability > self._config.endpoint_threshold
                    else:
                        is_complete = result.prediction == 1
                    if is_complete:
                        if self._state == TurnManagerState.USER_PAUSED and pause.guard():
                            await self._complete_user_turn("smart_turn_complete")
                        return
                    logger.debug(
                        "Smart-turn: incomplete (p=%.3f), falling back to silence timeout",
                        result.probability,
                    )
                except Exception:
                    logger.exception("Endpoint detection failed, falling back to silence timeout")

            if detector_attempted:
                # Grant the full grace budget from the moment of the
                # "incomplete" (or failed) decision — do not penalize the user
                # for detector latency, which would let a slow model collapse
                # the wait to zero. A model verdict also takes precedence over
                # the punctuation hint.
                await asyncio.sleep(self._config.end_of_turn_silence_ms / 1000.0)
            else:
                punctuated_endpoint = await self._wait_for_fixed_endpoint()

            if self._state == TurnManagerState.USER_PAUSED and pause.guard():
                await self._complete_user_turn(
                    "punctuated_silence_timeout" if punctuated_endpoint else "silence_timeout"
                )
        except asyncio.CancelledError:  # noqa: TRY203
            raise

    async def _wait_for_fixed_endpoint(self) -> bool:
        """Wait for the fixed timeout and report whether punctuation shortened it."""
        full_delay_s = self._config.end_of_turn_silence_ms / 1000.0
        punctuated_ms = self._config.punctuated_end_of_turn_silence_ms
        if punctuated_ms is None:
            await asyncio.sleep(full_delay_s)
            return False
        if punctuated_ms >= self._config.end_of_turn_silence_ms:
            logger.debug(
                "Punctuation endpoint shortening disabled: punctuated wait %dms "
                "is not below fixed wait %dms",
                punctuated_ms,
                self._config.end_of_turn_silence_ms,
            )
            await asyncio.sleep(full_delay_s)
            return False
        if full_delay_s <= 0:
            return False

        try:
            await asyncio.wait_for(
                self._punctuated_transcript_event.wait(),
                timeout=full_delay_s,
            )
        except TimeoutError:
            return False

        silence_started = self._silence_start_time
        elapsed_s = time.monotonic() - silence_started if silence_started is not None else 0.0
        remaining_s = punctuated_ms / 1000.0 - elapsed_s
        if remaining_s > 0:
            await asyncio.sleep(remaining_s)
        return True

    def _cancel_silence_timer(self) -> None:
        """Cancel the pending silence timeout task."""
        task = self._silence_timer_task
        if task and not task.done():
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if task is not current_task:
                task.cancel()
        self._silence_timer_task = None
        self._silence_start_time = None

    # ── Barge-in handling ───────────────────────────────────────

    async def _handle_barge_in(self) -> None:
        """Handle user speech during bot playback (barge-in).

        Triggers the cancel callback to cut off TTS and arrange old-turn
        cleanup, then starts a new user turn. The callback is responsible for
        emitting the ``Interruption`` event so it is emitted exactly once per
        barge-in.

        If the callback returns ``False``, barge-in is suppressed (e.g. a
        queued session action has ``no_interrupt=True``).  In that case we
        do **not** start a new turn — the current bot playback continues.
        """
        if self._barge_in_claim is not None:
            return

        expected_state = self._state
        expected_turn_id = self._current_turn_id
        expected_token = self._cancel_token
        claim = object()
        self._barge_in_claim = claim
        try:
            # Cancel current bot output via the session callback.
            # The callback is responsible for emitting the Interruption event.
            if self._cancel_turn_callback:
                result = await self._cancel_turn_callback()
                if result is False:
                    return

            # Another live turn may have advanced the manager while the cutoff
            # awaited transport/provider work. Never let this stale callback
            # create a successor for that different turn. ``IDLE`` is the one
            # expected state change: cancelling a silent application prompt
            # releases its old turn before this callback returns, after which
            # this already-claimed barge-in must install the voice successor.
            if self._state is expected_state:
                if (
                    self._current_turn_id != expected_turn_id
                    or self._cancel_token is not expected_token
                ):
                    return
            elif self._state is not TurnManagerState.IDLE:
                return

            # Start new turn, cancelling the prior token (see ``_begin_turn``).
            await self._begin_turn("barge_in", cancel_previous_token=True)
        finally:
            if self._barge_in_claim is claim:
                self._barge_in_claim = None

    # ── Push-to-talk mode ───────────────────────────────────────

    async def start_turn(self) -> None:
        """Manually start a turn (push-to-talk mode).

        Can also be used in VAD mode to force-start a turn.
        """
        if self._shutting_down:
            return
        if self._state == TurnManagerState.PROCESSING:
            # PTT press while the agent is processing — treat as a barge-in
            # to cancel the stale response and start a fresh turn (mirrors the
            # VAD path in _handle_speech_start).
            await self._handle_barge_in()
            return

        if self._state not in (TurnManagerState.IDLE, TurnManagerState.BOT_SPEAKING):
            return

        if self._state == TurnManagerState.BOT_SPEAKING:
            await self._handle_barge_in()
            return

        await self._begin_turn("manual_start")

    async def end_turn(self) -> None:
        """Manually signal end of user turn (push-to-talk mode).

        Bypasses VAD timeout and immediately transitions to Processing.
        """
        if self._shutting_down:
            return
        if self._state not in (
            TurnManagerState.USER_SPEAKING,
            TurnManagerState.USER_PAUSED,
        ):
            return

        self._cancel_silence_timer()
        await self._complete_user_turn("manual_end")

    def begin_application_turn(self, turn_id: str, cancel_token: CancelToken) -> None:
        """Bind an application-initiated turn directly in the processing state."""
        if self._shutting_down:
            raise RuntimeError("Turn manager is shutting down")
        if self._state != TurnManagerState.IDLE:
            raise RuntimeError(
                f"Cannot start an application turn while turn manager is {self._state.value}"
            )
        self._cancel_token = cancel_token
        self._current_turn_id = turn_id
        self._transition(
            TurnManagerState.PROCESSING,
            reason="application_prompt",
        )

    # ── Bot speaking lifecycle ──────────────────────────────────

    async def bot_started_speaking(self) -> Lease[TurnManagerState] | None:
        """Enter bot playback and return the exact published activity lease."""
        if self._state in (TurnManagerState.USER_SPEAKING, TurnManagerState.USER_PAUSED):
            logger.warning(
                "bot_started_speaking called in unexpected state %s, ignoring",
                self._state.value,
            )
            return None
        # Defensive cleanup: there should be no pending silence timer once a
        # turn is complete, but cancel any stale timer to avoid cross-turn
        # races in non-standard/manual integrations.
        self._cancel_silence_timer()
        activity = self._transition(
            TurnManagerState.BOT_SPEAKING,
            reason="bot_started",
        )
        await self._event_bus.emit(
            BotStartedSpeaking(session_id=self._session_id, turn_id=self._current_turn_id)
        )
        return activity

    async def bot_stopped_speaking(self) -> Lease[TurnManagerState] | None:
        """Leave bot playback and return the exact published activity lease."""
        if self._state == TurnManagerState.BOT_SPEAKING:
            activity = self._transition(
                TurnManagerState.IDLE,
                reason="bot_done",
            )
            await self._event_bus.emit(
                BotStoppedSpeaking(session_id=self._session_id, turn_id=self._current_turn_id)
            )
            return activity
        return None

    # ── State management ────────────────────────────────────────

    def set_mode(self, mode: TurnMode) -> None:
        """Switch between VAD and push-to-talk mode."""
        normalized = _normalize_turn_mode(mode)
        self._mode = normalized
        self._config.mode = normalized
        logger.debug("Turn mode set to %s", normalized.value)

    def reset(self, *, preserve_token: bool = False) -> None:
        """Reset turn manager to idle state.

        By default the active token is cancelled before being dropped,
        mirroring ``_handle_barge_in`` so any work bound to it is cooperatively
        stopped rather than left referencing an abandoned (uncancelled) token.

        Pass ``preserve_token=True`` when the current turn's token must stay
        live (e.g. the gated-replay keep-alive path, where a concurrently
        running agent stream still depends on it): the reference is still
        dropped, but the token is left uncancelled.
        """
        self._cancel_silence_timer()
        self._transition(
            TurnManagerState.IDLE,
            reason="reset",
            observe=False,
        )
        self._turn_audio.clear()
        self._turn_audio_duration_ms = 0.0
        self._pre_roll_buffer.clear()
        self._pre_roll_duration_ms = 0.0
        if not preserve_token and self._cancel_token is not None:
            self._cancel_token.cancel()
        self._cancel_token = None
        self._barge_in_claim = None
        self._silence_start_time = None
        self._current_turn_id = None

    def close_admission(self) -> None:
        """Prevent new voice, manual, or application turns during teardown."""
        self._shutting_down = True

    async def shutdown(self) -> None:
        """Clean up any pending tasks."""
        self.close_admission()
        current = asyncio.current_task()
        while True:
            timers = tuple(task for task in self._silence_timer_tasks if task is not current)
            if not timers:
                break
            completed = tuple(task for task in timers if task.done())
            if completed:
                # Do not rely on done callbacks running before this coroutine
                # resumes. On Python 3.14 an already-complete gather can resume
                # synchronously and repeatedly observe the same retained task,
                # starving its scheduled discard callback.
                self._silence_timer_tasks.difference_update(completed)
            pending = tuple(task for task in timers if not task.done())
            if not pending:
                continue
            for task in pending:
                task.cancel()
            # Bound the join so cancellation-resistant detectors do not hang shutdown (gh 995).
            # A detector that suppresses CancelledError remains owned for a later retry.
            try:
                await asyncio.wait_for(asyncio.wait(pending), timeout=1.0)
            except TimeoutError:
                # Leave still-running tasks owned for a later retry instead of hanging.
                still_pending = tuple(t for t in pending if not t.done())
                pending_done = tuple(t for t in pending if t.done())
                self._silence_timer_tasks.difference_update(pending_done)
                # Do not discard still-pending; will be retried on next loop iteration, but
                # break to avoid infinite hang if detector never completes.
                if still_pending:
                    logger.warning(
                        "TurnManager.shutdown: %d silence timer(s) resisted cancellation; "
                        "leaving for later retry",
                        len(still_pending),
                    )
                    break
                continue
            self._silence_timer_tasks.difference_update(pending)
        if current is not None:
            self._silence_timer_tasks.discard(current)
        self._silence_timer_task = None
        self.reset()
