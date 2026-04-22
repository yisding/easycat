"""Listen-only supervisor helpers built on top of the Session event bus.

The core runtime remains one session per call/client.  This module adds a
small fan-out layer that taps session audio events and forwards them to
passive listeners without changing transport/session ownership.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from easycat.audio_format import AudioChunk
from easycat.events import AudioIn, AudioOut

if TYPE_CHECKING:
    from easycat.session._session import Session

logger = logging.getLogger(__name__)

SupervisorTrack = Literal["caller", "assistant"]


@dataclass(frozen=True, slots=True)
class SupervisorAudioFrame:
    """One audio frame delivered to a passive supervisor listener."""

    session_id: str
    track: SupervisorTrack
    chunk: AudioChunk
    turn_id: str | None
    timestamp: float


class SessionAudioBroadcaster:
    """Fan out caller/bot audio from one Session to many passive listeners.

    Each listener receives frames through its own :class:`asyncio.Queue`.
    Slow listeners never block the live call: frames are dropped for that
    listener when its queue is full.
    """

    def __init__(
        self,
        session: Session,
        *,
        max_listener_queue: int = 256,
    ) -> None:
        self._session = session
        self._max_listener_queue = max(1, max_listener_queue)
        self._listeners: dict[int, asyncio.Queue[SupervisorAudioFrame | None]] = {}
        self._next_listener_id = 0
        self._closed = False
        self._dropped_frames = 0

        self._session.subscribe_event(AudioIn, self._on_audio_in)
        self._session.subscribe_event(AudioOut, self._on_audio_out)

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    def subscribe(
        self,
        *,
        max_queue_size: int | None = None,
    ) -> tuple[int, asyncio.Queue[SupervisorAudioFrame | None]]:
        """Register a new passive listener and return ``(id, queue)``."""
        if self._closed:
            raise RuntimeError("SessionAudioBroadcaster is closed")

        listener_id = self._next_listener_id
        self._next_listener_id += 1
        queue_size = self._max_listener_queue if max_queue_size is None else max(1, max_queue_size)
        queue: asyncio.Queue[SupervisorAudioFrame | None] = asyncio.Queue(maxsize=queue_size)
        self._listeners[listener_id] = queue
        return listener_id, queue

    def unsubscribe(self, listener_id: int) -> None:
        """Detach one listener and terminate its queue."""
        queue = self._listeners.pop(listener_id, None)
        if queue is None:
            return
        self._terminate_queue(queue)

    def close(self) -> None:
        """Detach from the session and terminate all listener queues."""
        if self._closed:
            return
        self._closed = True

        self._session.unsubscribe_event(AudioIn, self._on_audio_in)
        self._session.unsubscribe_event(AudioOut, self._on_audio_out)

        listeners = list(self._listeners.values())
        self._listeners.clear()
        for queue in listeners:
            self._terminate_queue(queue)

    def _on_audio_in(self, event: AudioIn) -> None:
        self._forward(event, "caller")

    def _on_audio_out(self, event: AudioOut) -> None:
        self._forward(event, "assistant")

    def _forward(self, event: AudioIn | AudioOut, track: SupervisorTrack) -> None:
        self._broadcast(
            SupervisorAudioFrame(
                session_id=event.session_id or self._session.session_id,
                track=track,
                chunk=event.chunk,
                turn_id=event.turn_id,
                timestamp=event.timestamp,
            )
        )

    def _broadcast(self, frame: SupervisorAudioFrame) -> None:
        if self._closed or not self._listeners:
            return

        for listener_id, queue in tuple(self._listeners.items()):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                self._dropped_frames += 1
                if self._dropped_frames == 1 or self._dropped_frames % 100 == 0:
                    logger.warning(
                        "Dropping supervisor audio frame for listener %s on session %s "
                        "(dropped=%s)",
                        listener_id,
                        self._session.session_id,
                        self._dropped_frames,
                    )

    def _terminate_queue(self, queue: asyncio.Queue[SupervisorAudioFrame | None]) -> None:
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                logger.debug(
                    "Supervisor listener queue refused shutdown sentinel for session %s",
                    self._session.session_id,
                )
