"""Shared TTS synthesis logic for both basic and streaming agent paths.

Extracts the duplicated TTS iteration loop — event dispatching, metrics
recording, audio queueing, and span management — into one reusable helper.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from easycat._span_manager import SpanManager
from easycat.bounded_queue import BoundedAudioQueue
from easycat.events import EventBus, TTSAudio, TTSEventType, TTSMarkers
from easycat.metrics import TTS_TTFB, TURN_E2E, MetricsCollector
from easycat.timeouts import TimeoutConfig, with_tts_timeout
from easycat.tracing import SpanStatus, Tracer

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
    events (TTSAudio/TTSMarkers), record TTFB/E2E metrics, queue audio
    to the outbound queue, and manage the TTS tracing span.
    """

    def __init__(
        self,
        tts: Any,  # TTSProvider (duck-typed)
        event_bus: EventBus,
        outbound_queue: BoundedAudioQueue,
        spans: SpanManager,
        metrics: MetricsCollector | None = None,
        timeout_config: TimeoutConfig | None = None,
    ) -> None:
        self._tts = tts
        self._event_bus = event_bus
        self._outbound_queue = outbound_queue
        self._spans = spans
        self._metrics = metrics
        self._timeout_config = timeout_config

    async def synthesize(
        self,
        text: str,
        token: Any | None,
        *,
        turn_end_time: float | None = None,
        is_active: Callable[[], bool] | None = None,
        record_latency: bool = True,
    ) -> TTSSynthResult:
        """Synthesize text and stream audio to the outbound queue.

        Starts a TTS tracing span, iterates the provider's audio events,
        emits EasyCat-level events, records latency metrics, and queues
        audio chunks for transport.

        Args:
            text: Text to synthesize.
            token: CancelToken to check between chunks.
            turn_end_time: Monotonic timestamp of turn end (for E2E latency).
            is_active: Optional predicate; iteration stops when it returns False.
                Typically ``lambda: self._turn_state == TurnState.BOT_SPEAKING``.
            record_latency: Whether to record TTS_TTFB and TURN_E2E metrics.
                Set to False for subsequent sentence chunks in a streaming turn
                to avoid recording multiple latency samples per turn.

        Returns:
            TTSSynthResult indicating whether audio was produced.

        Raises:
            TTSTimeoutError: If first-byte timeout is exceeded.
            Exception: Any other TTS provider error (propagated to caller).
        """
        result = TTSSynthResult()
        tts_start = time.monotonic()
        tts_span = self._spans.start(Tracer.TTS)
        tts_status = SpanStatus.OK

        try:
            tts_iter = self._tts.synthesize(text)
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
                    await self._event_bus.emit(TTSAudio(chunk=tts_event.audio))
                    if not result.audio_produced:
                        result.audio_produced = True
                        result.first_audio_time = time.monotonic()
                        if record_latency and self._metrics:
                            self._metrics.record_latency(
                                TTS_TTFB,
                                (result.first_audio_time - tts_start) * 1000,
                            )
                            if turn_end_time is not None:
                                self._metrics.record_latency(
                                    TURN_E2E,
                                    (result.first_audio_time - turn_end_time) * 1000,
                                )
                    await self._outbound_queue.put(tts_event.audio)

                elif tts_event.type == TTSEventType.MARKERS and tts_event.markers:
                    await self._event_bus.emit(TTSMarkers(markers=tts_event.markers))

        except asyncio.CancelledError:
            result.completed = False
        except Exception as exc:
            if tts_span:
                tts_span.set_error(exc)
            tts_status = SpanStatus.ERROR
            raise
        finally:
            self._spans.finish(Tracer.TTS, tts_status)

        return result

    async def cancel(self) -> None:
        """Cancel the TTS provider."""
        try:
            await self._tts.cancel()
        except Exception:
            pass
