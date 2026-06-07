"""Listen-only supervisor helpers built on top of the Session event bus.

The core runtime remains one session per call/client.  This module adds a
small fan-out layer that taps session audio events and forwards them to
passive listeners without changing transport/session ownership.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from easycat.audio_format import AudioChunk
from easycat.events import (
    AudioIn,
    AudioOut,
    Event,
    SupervisorListenerAttached,
    SupervisorListenerDetached,
)

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


SupervisorConsentHook = Callable[[SupervisorAudioFrame], bool]
SupervisorRedactionHook = Callable[[SupervisorAudioFrame], SupervisorAudioFrame | None]


class SessionAudioBroadcaster:
    """Fan out caller/bot audio from one Session to many passive listeners.

    Each listener receives frames through its own :class:`asyncio.Queue`.
    Slow listeners never block the live call: frames are dropped for that
    listener when its queue is full.  Use ``consent_hook`` to suppress frames
    until recording/listen-in consent is established, and ``redaction_hook`` to
    transform or suppress frames before they reach supervisor listeners.
    """

    def __init__(
        self,
        session: Session,
        *,
        max_listener_queue: int = 256,
        consent_hook: SupervisorConsentHook | None = None,
        redaction_hook: SupervisorRedactionHook | None = None,
    ) -> None:
        self._session = session
        self._max_listener_queue = max(1, max_listener_queue)
        self._consent_hook = consent_hook
        self._redaction_hook = redaction_hook
        self._listeners: dict[int, asyncio.Queue[SupervisorAudioFrame | None]] = {}
        self._listener_dropped_frames: dict[int, int] = {}
        self._next_listener_id = 0
        self._closed = False
        self._dropped_frames = 0
        self._consent_blocked_frames = 0
        self._redacted_frames = 0
        self._audit_tasks: set[asyncio.Task[None]] = set()

        self._session.subscribe_event(AudioIn, self._on_audio_in)
        self._session.subscribe_event(AudioOut, self._on_audio_out)

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    @property
    def consent_blocked_frames(self) -> int:
        return self._consent_blocked_frames

    @property
    def redacted_frames(self) -> int:
        return self._redacted_frames

    @property
    def dropped_frames_by_listener(self) -> dict[int, int]:
        """Return dropped frame counts for currently subscribed listeners."""
        return dict(self._listener_dropped_frames)

    def dropped_frames_for(self, listener_id: int) -> int:
        """Return the dropped frame count for one active listener."""
        return self._listener_dropped_frames.get(listener_id, 0)

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
        self._listener_dropped_frames[listener_id] = 0
        self._emit_audit_event(
            SupervisorListenerAttached(
                listener_id=listener_id,
                queue_size=queue_size,
                session_id=self._session.session_id,
            )
        )
        return listener_id, queue

    def unsubscribe(self, listener_id: int) -> None:
        """Detach one listener and terminate its queue."""
        queue = self._listeners.pop(listener_id, None)
        if queue is None:
            return
        dropped_frames = self._listener_dropped_frames.pop(listener_id, 0)
        self._emit_listener_detached(
            listener_id,
            dropped_frames=dropped_frames,
            reason="unsubscribe",
        )
        self._terminate_queue(queue)

    def close(self) -> None:
        """Detach from the session and terminate all listener queues."""
        if self._closed:
            return
        self._closed = True

        self._session.unsubscribe_event(AudioIn, self._on_audio_in)
        self._session.unsubscribe_event(AudioOut, self._on_audio_out)

        listeners = list(self._listeners.items())
        self._listeners.clear()
        for listener_id, queue in listeners:
            dropped_frames = self._listener_dropped_frames.get(listener_id, 0)
            self._emit_listener_detached(
                listener_id,
                dropped_frames=dropped_frames,
                reason="close",
            )
            self._terminate_queue(queue)
        self._listener_dropped_frames.clear()

    async def drain_audit_events(self) -> None:
        """Await in-flight supervisor audit event emissions."""
        if not self._audit_tasks:
            return
        pending = list(self._audit_tasks)
        await asyncio.gather(*pending, return_exceptions=True)

    def _on_audio_in(self, event: AudioIn) -> None:
        self._forward(event, "caller")

    def _on_audio_out(self, event: AudioOut) -> None:
        self._forward(event, "assistant")

    def _forward(self, event: AudioIn | AudioOut, track: SupervisorTrack) -> None:
        frame = SupervisorAudioFrame(
            session_id=event.session_id or self._session.session_id,
            track=track,
            chunk=event.chunk,
            turn_id=event.turn_id,
            timestamp=event.timestamp,
        )
        frame = self._prepare_frame(frame)
        if frame is None:
            return
        self._broadcast(frame)

    def _prepare_frame(self, frame: SupervisorAudioFrame) -> SupervisorAudioFrame | None:
        if self._consent_hook is not None:
            try:
                consented = self._consent_hook(frame)
            except Exception:
                self._consent_blocked_frames += 1
                logger.warning(
                    "Supervisor consent hook raised for session %s; suppressing frame",
                    self._session.session_id,
                    exc_info=True,
                )
                return None
            if not consented:
                self._consent_blocked_frames += 1
                return None

        if self._redaction_hook is None:
            return frame

        try:
            redacted = self._redaction_hook(frame)
        except Exception:
            self._redacted_frames += 1
            logger.warning(
                "Supervisor redaction hook raised for session %s; suppressing frame",
                self._session.session_id,
                exc_info=True,
            )
            return None
        if redacted is None:
            self._redacted_frames += 1
            return None
        if redacted is not frame:
            self._redacted_frames += 1
        return redacted

    def _broadcast(self, frame: SupervisorAudioFrame) -> None:
        if self._closed or not self._listeners:
            return

        for listener_id, queue in tuple(self._listeners.items()):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                self._dropped_frames += 1
                listener_dropped = self._listener_dropped_frames.get(listener_id, 0) + 1
                self._listener_dropped_frames[listener_id] = listener_dropped
                if listener_dropped == 1 or listener_dropped % 100 == 0:
                    logger.warning(
                        "Dropping supervisor audio frame for listener %s on session %s "
                        "(listener_dropped=%s total_dropped=%s)",
                        listener_id,
                        self._session.session_id,
                        listener_dropped,
                        self._dropped_frames,
                    )

    def _emit_listener_detached(
        self,
        listener_id: int,
        *,
        dropped_frames: int,
        reason: Literal["unsubscribe", "close"],
    ) -> None:
        self._emit_audit_event(
            SupervisorListenerDetached(
                listener_id=listener_id,
                dropped_frames=dropped_frames,
                reason=reason,
                session_id=self._session.session_id,
            )
        )

    def _emit_audit_event(self, event: Event) -> None:
        bus = getattr(self._session, "event_bus", None)
        if bus is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(bus.emit(event))
        self._audit_tasks.add(task)
        task.add_done_callback(self._audit_tasks.discard)

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
