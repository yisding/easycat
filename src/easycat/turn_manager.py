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
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

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
)
from easycat.smart_turn import SmartTurnProvider

logger = logging.getLogger(__name__)


class TurnManagerState(enum.Enum):
    """Turn-taking state machine states."""

    IDLE = "idle"
    USER_SPEAKING = "user_speaking"
    USER_PAUSED = "user_paused"
    PROCESSING = "processing"
    BOT_SPEAKING = "bot_speaking"


class TurnMode(enum.Enum):
    """Turn detection mode."""

    VAD = "vad"
    PUSH_TO_TALK = "push_to_talk"


@dataclass
class TurnManagerConfig:
    """Configuration for TurnManager."""

    # End-of-turn silence timeout in milliseconds
    end_of_turn_silence_ms: int = 1000
    # Silence budget, after VAD stop, before finalizing the current STT segment.
    # 0 means commit the segment immediately when VAD reports a pause.
    #
    # NOTE: This field is *not* read by TurnManager itself.  It is consumed by
    # ``Session``, which forwards it to the ``STTCommitter`` as
    # ``segment_silence_ms`` (see ``session/_session.py`` and
    # ``session/_stt_committer.py``).  Setting it on a bare ``TurnManager``
    # (constructed without a Session) therefore has no effect.  It lives here so
    # the single ``TurnManagerConfig`` object stays the one place callers tune
    # turn/STT segmentation timing.
    stt_segment_silence_ms: int = 0
    # Pre-roll buffer duration in milliseconds
    pre_roll_ms: int = 300
    # Turn detection mode
    mode: TurnMode = TurnMode.VAD
    # Optional endpoint detector for smart turn-taking.
    # When set, TurnManager queries it on silence to decide whether
    # to end the turn immediately or wait the full timeout.
    endpoint_detector: SmartTurnProvider | None = None
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
        if self.end_of_turn_silence_ms < 0:
            raise ValueError("end_of_turn_silence_ms must be non-negative")
        if self.stt_segment_silence_ms < 0:
            raise ValueError("stt_segment_silence_ms must be non-negative")
        if self.pre_roll_ms < 0:
            raise ValueError("pre_roll_ms must be non-negative")


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

        # Callback for barge-in: expected to call session.cancel_turn(barge_in=True).
        # The callback is the sole emitter of the Interruption event.  Phase 4
        # of the session decomposition installs this callback late (after the
        # CancelOrchestrator exists), so it is also settable post-construction
        # via :meth:`set_cancel_callback`.
        self._cancel_turn_callback = cancel_turn_callback

        # State
        self._state = TurnManagerState.IDLE
        self._mode = self._config.mode

        # Pre-roll audio buffer (rolling window of recent audio frames)
        self._pre_roll_buffer: deque[AudioChunk] = deque()
        self._pre_roll_duration_ms: float = 0.0

        # Captured audio for the current turn (pre-roll + speech audio)
        self._turn_audio: list[AudioChunk] = []

        # Silence timeout tracking
        self._silence_start_time: float | None = None
        self._silence_timer_task: asyncio.Task[None] | None = None

        # Cancel token for the current turn
        self._cancel_token: CancelToken | None = None

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

        # Correlation identifiers
        self._session_id: str | None = None
        self._turn_counter = 0
        self._current_turn_id: str | None = None

    # ── Properties ──────────────────────────────────────────────

    @property
    def state(self) -> TurnManagerState:
        return self._state

    @property
    def mode(self) -> TurnMode:
        return self._mode

    @property
    def cancel_token(self) -> CancelToken | None:
        return self._cancel_token

    @property
    def turn_audio(self) -> list[AudioChunk]:
        """Snapshot of audio chunks captured for the current turn (with pre-roll).

        Returns a *copy* of the internal list so callers can safely iterate it
        while awaiting (e.g. priming STT chunk-by-chunk) without risking
        mutation if a future caller feeds ``on_audio_frame`` from another task.
        The list is small (pre-roll + turn frames), so the copy is negligible.
        """
        return list(self._turn_audio)

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

    def _transition(
        self,
        to_state: TurnManagerState,
        *,
        reason: str,
    ) -> None:
        """Move to ``to_state``, log the transition, and journal it.

        Centralises what used to be a scattered set of ``self._state = X``
        + ``logger.debug(...)`` pairs.  Every transition now gets a
        ``turn_state_changed`` record so bundles can answer "why did the
        turn end when it did" from the journal alone.

        The debug log line is derived from the real ``from_state`` /
        ``to_state`` / ``reason`` so it can never disagree with the journal
        record (callers no longer pass a hardcoded ``log_msg`` that could
        drift from the actual transition — e.g. a barge-in from PROCESSING
        used to falsely log a from-state of BOT_SPEAKING).
        """
        from_state = self._state
        self._state = to_state
        logger.debug("Turn: %s -> %s (%s)", from_state.value, to_state.value, reason)
        hook = self._journal_state_change
        if hook is not None:
            try:
                hook(from_state, to_state, reason, self._current_turn_id)
            except Exception:  # noqa: BLE001 - never break the state machine
                logger.debug("journal state-change hook raised", exc_info=True)

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
        # Always maintain the pre-roll buffer
        self._pre_roll_buffer.append(chunk)
        self._pre_roll_duration_ms += chunk.duration_ms

        # Trim pre-roll to configured duration
        while self._pre_roll_duration_ms > self._config.pre_roll_ms and self._pre_roll_buffer:
            removed = self._pre_roll_buffer.popleft()
            self._pre_roll_duration_ms -= removed.duration_ms

        # If user is speaking, capture the audio
        if self._state in (TurnManagerState.USER_SPEAKING, TurnManagerState.USER_PAUSED):
            self._turn_audio.append(chunk)

    # ── VAD event handling ──────────────────────────────────────

    async def on_vad_event(self, event: VADStartSpeaking | VADStopSpeaking) -> None:
        """Handle a VAD event. Called by the pipeline when VAD emits events.

        In push-to-talk mode, VAD events are ignored.
        """
        if self._mode == TurnMode.PUSH_TO_TALK:
            return

        if isinstance(event, VADStartSpeaking):
            await self._handle_speech_start()
        elif isinstance(event, VADStopSpeaking):
            await self._handle_speech_stop()

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
            self._cancel_token = CancelToken()

            # Flush pre-roll buffer into turn audio
            self._turn_audio = list(self._pre_roll_buffer)
            self._pre_roll_buffer.clear()
            self._pre_roll_duration_ms = 0.0

            self._turn_counter += 1
            self._current_turn_id = f"turn-{self._turn_counter:04d}-{uuid4().hex[:8]}"
            self._transition(
                TurnManagerState.USER_SPEAKING,
                reason="vad_speech_start",
            )
            await self._event_bus.emit(
                TurnStarted(session_id=self._session_id, turn_id=self._current_turn_id)
            )

    async def _handle_speech_stop(self) -> None:
        """Handle VAD speech stop — transition to UserPaused and start timer."""
        if self._state != TurnManagerState.USER_SPEAKING:
            return

        self._silence_start_time = time.monotonic()
        self._transition(
            TurnManagerState.USER_PAUSED,
            reason="vad_silence",
        )

        # Start the end-of-turn silence timer
        self._cancel_silence_timer()
        self._silence_timer_task = asyncio.create_task(self._silence_timeout())

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

    async def _silence_timeout(self) -> None:
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
            if self._endpoint_detector is not None and self._turn_audio:
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
                        result = await self._endpoint_detector.detect(
                            self._detector_audio_window()
                        )
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
                        if self._state == TurnManagerState.USER_PAUSED:
                            self._transition(
                                TurnManagerState.PROCESSING,
                                reason="smart_turn_complete",
                            )
                            await self._event_bus.emit(
                                TurnEnded(
                                    session_id=self._session_id, turn_id=self._current_turn_id
                                )
                            )
                        return
                    logger.debug(
                        "Smart-turn: incomplete (p=%.3f), falling back to silence timeout",
                        result.probability,
                    )
                except Exception:
                    logger.exception("Endpoint detection failed, falling back to silence timeout")

            # Grant the full grace budget from the moment of the "incomplete"
            # (or failed) decision — do not penalize the user for detector
            # latency, which would let a slow model collapse the wait to zero.
            await asyncio.sleep(self._config.end_of_turn_silence_ms / 1000.0)

            if self._state == TurnManagerState.USER_PAUSED:
                self._transition(
                    TurnManagerState.PROCESSING,
                    reason="silence_timeout",
                )
                await self._event_bus.emit(
                    TurnEnded(session_id=self._session_id, turn_id=self._current_turn_id)
                )
        except asyncio.CancelledError:
            pass

    def _cancel_silence_timer(self) -> None:
        """Cancel the pending silence timeout task."""
        if self._silence_timer_task and not self._silence_timer_task.done():
            self._silence_timer_task.cancel()
        self._silence_timer_task = None
        self._silence_start_time = None

    # ── Barge-in handling ───────────────────────────────────────

    async def _handle_barge_in(self) -> None:
        """Handle user speech during bot playback (barge-in).

        Triggers the cancel callback to stop TTS/agent, then starts a new
        user turn.  The callback (typically ``session.cancel_turn(barge_in=True)``)
        is responsible for emitting the ``Interruption`` event so that it is
        emitted exactly once per barge-in.

        If the callback returns ``False``, barge-in is suppressed (e.g. a
        queued session action has ``no_interrupt=True``).  In that case we
        do **not** start a new turn — the current bot playback continues.
        """
        # Cancel current bot output via the session callback.
        # The callback is responsible for emitting the Interruption event.
        if self._cancel_turn_callback:
            result = await self._cancel_turn_callback()
            if result is False:
                return

        # Cancel the prior turn's token before issuing a fresh one.  When the
        # barge-in interrupts a PROCESSING turn there is an in-flight agent run
        # bound to this token; cancelling it prevents a stale response from
        # leaking through once the new turn has started.
        if self._cancel_token is not None:
            self._cancel_token.cancel()

        # Start new turn
        self._cancel_token = CancelToken()
        self._turn_counter += 1
        self._current_turn_id = f"turn-{self._turn_counter:04d}-{uuid4().hex[:8]}"
        self._transition(
            TurnManagerState.USER_SPEAKING,
            reason="barge_in",
        )

        # Flush pre-roll buffer into turn audio
        self._turn_audio = list(self._pre_roll_buffer)
        self._pre_roll_buffer.clear()
        self._pre_roll_duration_ms = 0.0

        await self._event_bus.emit(
            TurnStarted(session_id=self._session_id, turn_id=self._current_turn_id)
        )

    # ── Push-to-talk mode ───────────────────────────────────────

    async def start_turn(self) -> None:
        """Manually start a turn (push-to-talk mode).

        Can also be used in VAD mode to force-start a turn.
        """
        if self._state not in (TurnManagerState.IDLE, TurnManagerState.BOT_SPEAKING):
            return

        if self._state == TurnManagerState.BOT_SPEAKING:
            await self._handle_barge_in()
            return

        self._cancel_token = CancelToken()
        self._turn_counter += 1
        self._current_turn_id = f"turn-{self._turn_counter:04d}-{uuid4().hex[:8]}"
        self._transition(
            TurnManagerState.USER_SPEAKING,
            reason="manual_start",
        )

        # Flush pre-roll
        self._turn_audio = list(self._pre_roll_buffer)
        self._pre_roll_buffer.clear()
        self._pre_roll_duration_ms = 0.0

        await self._event_bus.emit(
            TurnStarted(session_id=self._session_id, turn_id=self._current_turn_id)
        )

    async def end_turn(self) -> None:
        """Manually signal end of user turn (push-to-talk mode).

        Bypasses VAD timeout and immediately transitions to Processing.
        """
        if self._state not in (
            TurnManagerState.USER_SPEAKING,
            TurnManagerState.USER_PAUSED,
        ):
            return

        self._cancel_silence_timer()
        self._transition(
            TurnManagerState.PROCESSING,
            reason="manual_end",
        )
        await self._event_bus.emit(
            TurnEnded(session_id=self._session_id, turn_id=self._current_turn_id)
        )

    # ── Bot speaking lifecycle ──────────────────────────────────

    async def bot_started_speaking(self) -> None:
        """Called when TTS playback begins."""
        if self._state == TurnManagerState.USER_SPEAKING:
            logger.warning(
                "bot_started_speaking called in unexpected state %s, ignoring",
                self._state.value,
            )
            return
        # Defensive cleanup: there should be no pending silence timer once a
        # turn is complete, but cancel any stale timer to avoid cross-turn
        # races in non-standard/manual integrations.
        self._cancel_silence_timer()
        self._transition(
            TurnManagerState.BOT_SPEAKING,
            reason="bot_started",
        )
        await self._event_bus.emit(
            BotStartedSpeaking(session_id=self._session_id, turn_id=self._current_turn_id)
        )

    async def bot_stopped_speaking(self) -> None:
        """Called when TTS playback completes."""
        if self._state == TurnManagerState.BOT_SPEAKING:
            self._transition(
                TurnManagerState.IDLE,
                reason="bot_done",
            )
            await self._event_bus.emit(
                BotStoppedSpeaking(session_id=self._session_id, turn_id=self._current_turn_id)
            )

    # ── State management ────────────────────────────────────────

    def set_mode(self, mode: TurnMode) -> None:
        """Switch between VAD and push-to-talk mode."""
        self._mode = mode
        logger.debug("Turn mode set to %s", mode.value)

    def reset(self) -> None:
        """Reset turn manager to idle state."""
        self._cancel_silence_timer()
        self._state = TurnManagerState.IDLE
        self._turn_audio.clear()
        self._pre_roll_buffer.clear()
        self._pre_roll_duration_ms = 0.0
        # Cancel the active token before dropping it, mirroring
        # ``_handle_barge_in``.  Both teardown paths now share the same token
        # semantics, so any work bound to the token is cooperatively stopped
        # rather than left referencing an abandoned (uncancelled) token.
        if self._cancel_token is not None:
            self._cancel_token.cancel()
        self._cancel_token = None
        self._silence_start_time = None
        self._current_turn_id = None

    async def shutdown(self) -> None:
        """Clean up any pending tasks."""
        if self._silence_timer_task and not self._silence_timer_task.done():
            self._silence_timer_task.cancel()
            try:
                await self._silence_timer_task
            except asyncio.CancelledError:
                pass
        self._silence_timer_task = None
        self.reset()
