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
    # Pre-roll buffer duration in milliseconds
    pre_roll_ms: int = 300
    # Turn detection mode
    mode: TurnMode = TurnMode.VAD


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

    def __init__(
        self,
        event_bus: EventBus,
        config: TurnManagerConfig | None = None,
        cancel_turn_callback: Any | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._config = config or TurnManagerConfig()

        # Callback for barge-in: expected to call session.cancel_turn(barge_in=True).
        # The callback is the sole emitter of the Interruption event.
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
        """Audio chunks captured for the current turn (including pre-roll)."""
        return self._turn_audio

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

        if self._state == TurnManagerState.USER_PAUSED:
            # Speech resumed before timeout — cancel silence timer
            self._cancel_silence_timer()
            self._state = TurnManagerState.USER_SPEAKING
            logger.debug("Turn: UserPaused -> UserSpeaking (speech resumed)")
            return

        if self._state == TurnManagerState.IDLE:
            # New turn starting
            self._cancel_token = CancelToken()
            self._state = TurnManagerState.USER_SPEAKING

            # Flush pre-roll buffer into turn audio
            self._turn_audio = list(self._pre_roll_buffer)
            self._pre_roll_buffer.clear()
            self._pre_roll_duration_ms = 0.0

            await self._event_bus.emit(TurnStarted())
            logger.debug("Turn: Idle -> UserSpeaking (new turn, pre-roll flushed)")

    async def _handle_speech_stop(self) -> None:
        """Handle VAD speech stop — transition to UserPaused and start timer."""
        if self._state != TurnManagerState.USER_SPEAKING:
            return

        self._state = TurnManagerState.USER_PAUSED
        self._silence_start_time = time.monotonic()
        logger.debug("Turn: UserSpeaking -> UserPaused (silence detected)")

        # Start the end-of-turn silence timer
        self._cancel_silence_timer()
        self._silence_timer_task = asyncio.create_task(self._silence_timeout())

    async def _silence_timeout(self) -> None:
        """Wait for end-of-turn silence timeout, then transition to Processing."""
        try:
            await asyncio.sleep(self._config.end_of_turn_silence_ms / 1000.0)

            if self._state == TurnManagerState.USER_PAUSED:
                self._state = TurnManagerState.PROCESSING
                logger.debug("Turn: UserPaused -> Processing (silence timeout)")
                await self._event_bus.emit(TurnEnded())
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
        """
        logger.debug("Turn: BotSpeaking -> UserSpeaking (barge-in)")

        # Cancel current bot output via the session callback.
        # The callback is responsible for emitting the Interruption event.
        if self._cancel_turn_callback:
            await self._cancel_turn_callback()

        # Start new turn
        self._cancel_token = CancelToken()
        self._state = TurnManagerState.USER_SPEAKING

        # Flush pre-roll buffer into turn audio
        self._turn_audio = list(self._pre_roll_buffer)
        self._pre_roll_buffer.clear()
        self._pre_roll_duration_ms = 0.0

        await self._event_bus.emit(TurnStarted())

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
        self._state = TurnManagerState.USER_SPEAKING

        # Flush pre-roll
        self._turn_audio = list(self._pre_roll_buffer)
        self._pre_roll_buffer.clear()
        self._pre_roll_duration_ms = 0.0

        await self._event_bus.emit(TurnStarted())
        logger.debug("Turn: Idle -> UserSpeaking (manual start)")

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
        self._state = TurnManagerState.PROCESSING
        logger.debug("Turn: -> Processing (manual end)")
        await self._event_bus.emit(TurnEnded())

    # ── Bot speaking lifecycle ──────────────────────────────────

    async def bot_started_speaking(self) -> None:
        """Called when TTS playback begins."""
        self._state = TurnManagerState.BOT_SPEAKING
        await self._event_bus.emit(BotStartedSpeaking())
        logger.debug("Turn: -> BotSpeaking")

    async def bot_stopped_speaking(self) -> None:
        """Called when TTS playback completes."""
        if self._state == TurnManagerState.BOT_SPEAKING:
            self._state = TurnManagerState.IDLE
            await self._event_bus.emit(BotStoppedSpeaking())
            logger.debug("Turn: BotSpeaking -> Idle")

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
        self._cancel_token = None
        self._silence_start_time = None

    async def shutdown(self) -> None:
        """Clean up any pending tasks."""
        self._cancel_silence_timer()
        self.reset()
