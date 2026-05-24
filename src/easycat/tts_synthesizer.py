"""Shared TTS synthesis logic for both basic and streaming agent paths.

Extracts the duplicated TTS iteration loop — event dispatching,
audio queueing — into one reusable helper.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from easycat.bounded_queue import BoundedAudioQueue
from easycat.events import EventBus, TTSAudio, TTSEventType, TTSMarkers
from easycat.timeouts import TimeoutConfig, with_tts_timeout
from easycat.tts.input import TTSInput, coerce_tts_input

logger = logging.getLogger(__name__)


@dataclass
class TTSSynthResult:
    """Result from a single TTS synthesis call."""

    audio_produced: bool = False
    first_audio_time: float | None = None
    audio_bytes: int = 0
    completed: bool = True


class TTSSynthesizer:
    """Encapsulates the TTS iteration loop shared by both agent paths.

    Handles: iterate provider events, check cancellation, emit EasyCat
    events (TTSAudio/TTSMarkers), and queue audio to the outbound queue.
    """

    def __init__(
        self,
        tts: Any,  # TTSProvider (duck-typed)
        event_bus: EventBus,
        outbound_queue: BoundedAudioQueue,
        timeout_config: TimeoutConfig | None = None,
        correlation_ids: Callable[[], tuple[str | None, str | None]] | None = None,
        audio_gate: Callable[[], bool] | None = None,
    ) -> None:
        self._tts = tts
        self._event_bus = event_bus
        self._outbound_queue = outbound_queue
        self._timeout_config = timeout_config
        self._audio_gate = audio_gate
        self._correlation_ids = correlation_ids
        # Optional TTSStage wrapper.  When bound, ``synthesize`` calls
        # ``stage.execute(payload, ctx, turn)`` instead of the raw
        # provider so the stage can journal start/complete/frame records
        # and capture audio bytes as replay artifacts.
        self._stage: Any = None
        self._run_ctx_getter: Callable[[], Any] | None = None
        self._turn_getter: Callable[[], Any] | None = None

    def replace_outbound_queue(self, queue: BoundedAudioQueue) -> None:
        """Swap the outbound queue (used by Session.start when re-creating it).

        Mirrors :meth:`AudioRouter.replace_outbound_queue`; both the
        synthesizer (producer) and the router (drain) must point at the
        same instance after Session rebuilds the queue post-teardown.
        """
        self._outbound_queue = queue

    def bind_stage(
        self,
        stage: Any,
        *,
        run_ctx_getter: Callable[[], Any],
        turn_getter: Callable[[], Any],
    ) -> None:
        """Attach a :class:`TTSStage` wrapper for journal + artifact capture.

        The getters defer RunContext / TurnContext lookup to call time so
        stage records are stamped with the currently-active turn rather
        than a snapshot taken during Session construction.
        """
        self._stage = stage
        self._run_ctx_getter = run_ctx_getter
        self._turn_getter = turn_getter

    async def synthesize(
        self,
        payload: TTSInput | str,
        token: Any | None,
        *,
        is_active: Callable[[], bool] | None = None,
        bypass_gate: bool = False,
    ) -> TTSSynthResult:
        """Synthesize text and stream audio to the outbound queue.

        Iterates the provider's audio events, emits EasyCat-level events,
        and queues audio chunks for transport.

        Args:
            payload: Text payload to synthesize.
            token: CancelToken to check between chunks.
            is_active: Optional predicate; iteration stops when it returns False.
            bypass_gate: Whether to bypass the audio gate.

        Returns:
            TTSSynthResult indicating whether audio was produced.

        Raises:
            TTSTimeoutError: If first-byte timeout is exceeded.
            Exception: Any other TTS provider error (propagated to caller).
        """
        result = TTSSynthResult()
        # Snapshot the gate state at the start of synthesis.
        gated_at_start = not bypass_gate and bool(self._audio_gate and self._audio_gate())

        coerced = coerce_tts_input(payload)
        if (
            self._stage is not None
            and self._run_ctx_getter is not None
            and self._turn_getter is not None
        ):
            tts_iter = await self._stage.execute(
                coerced, self._run_ctx_getter(), self._turn_getter()
            )
        else:
            tts_iter = self._tts.synthesize(coerced)
        if self._timeout_config and self._timeout_config.tts_first_byte_timeout:
            tts_iter = with_tts_timeout(
                tts_iter,
                timeout=self._timeout_config.tts_first_byte_timeout,
                provider_name="tts",
                event_bus=self._event_bus,
            )

        async for tts_event in tts_iter:
            if token and token.is_cancelled:
                result.completed = False
                break
            if is_active and not is_active():
                result.completed = False
                break

            if tts_event.type == TTSEventType.AUDIO and tts_event.audio:
                result.audio_bytes += len(tts_event.audio.data)
                session_id, turn_id = (
                    self._correlation_ids() if self._correlation_ids else (None, None)
                )
                await self._event_bus.emit(
                    TTSAudio(
                        chunk=tts_event.audio,
                        session_id=session_id,
                        turn_id=turn_id,
                        bypass_gate=bypass_gate,
                    )
                )
                if not result.audio_produced:
                    result.audio_produced = True
                    result.first_audio_time = time.monotonic()
                if not gated_at_start and (
                    bypass_gate or not (self._audio_gate and self._audio_gate())
                ):
                    await self._outbound_queue.put(tts_event.audio)

            elif tts_event.type == TTSEventType.MARKERS and tts_event.markers:
                session_id, turn_id = (
                    self._correlation_ids() if self._correlation_ids else (None, None)
                )
                await self._event_bus.emit(
                    TTSMarkers(markers=tts_event.markers, session_id=session_id, turn_id=turn_id)
                )

        return result

    async def cancel(self) -> None:
        """Cancel the TTS provider."""
        try:
            await self._tts.cancel()
        except Exception:
            pass
