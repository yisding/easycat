"""Listen-only supervisor helpers built on top of the Session event bus.

The core runtime remains one session per call/client.  This module adds a
small fan-out layer that taps session audio events and forwards them to
passive listeners without changing transport/session ownership.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from websockets.exceptions import ConnectionClosed

from easycat._net import constant_time_strings_equal
from easycat.audio_format import AudioChunk
from easycat.events import (
    AudioIn,
    AudioOut,
    Event,
    SupervisorListenerAttached,
    SupervisorListenerDetached,
)
from easycat.runtime._event_tasks import RuntimeEventTaskScope, RuntimeTaskScope
from easycat.runtime.scope import RuntimeScope, RuntimeTaskAction

if TYPE_CHECKING:
    from easycat.session._session import Session

logger = logging.getLogger(__name__)

SUPERVISOR_TOKEN_ENV = "EASYCAT_SUPERVISOR_TOKEN"
SupervisorTrack = Literal["caller", "assistant"]
_SUPERVISOR_EVENT_TASK_NAME = "supervisor_audit_emit"
_SUPERVISOR_EVENT_COHORT = "supervisor-events"
_SUPERVISOR_EVENT_OWNER_ATTR = "_supervisor_event_tasks"
_SUPERVISOR_STREAM_COHORT = "supervisor-streams"
_SUPERVISOR_STREAM_COUNTER_ATTR = "_supervisor_stream_counter"


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


class SupervisorWebSocket(Protocol):
    """Minimal WebSocket contract used by supervisor listen-in helpers."""

    async def send(self, message: str) -> None: ...
    async def recv(self) -> object: ...
    async def close(self, code: int, reason: str) -> None: ...
    def __aiter__(self) -> AsyncIterator[object]: ...


def supervisor_auth_token_from_env(name: str = SUPERVISOR_TOKEN_ENV) -> str | None:
    """Return a trimmed supervisor token from the environment, if configured."""
    token = os.environ.get(name, "").strip()
    return token or None


def supervisor_message_authorized(
    message: Mapping[str, object],
    expected_token: str | None,
    *,
    allow_unauthenticated: bool = False,
) -> bool:
    """Check a supervisor subscription token.

    Supervisor listen-in streams live caller and assistant audio, so helpers fail
    closed unless a token is configured.  Tests or tightly controlled in-process
    callers can opt into unauthenticated mode explicitly with
    ``allow_unauthenticated=True``.
    """
    if expected_token is None:
        return allow_unauthenticated
    supplied_token = message.get("token")
    return isinstance(supplied_token, str) and constant_time_strings_equal(
        supplied_token, expected_token
    )


def supervisor_audio_frame_to_json(frame: SupervisorAudioFrame) -> str:
    """Serialize one supervisor audio frame for browser WebSocket clients."""
    fmt = frame.chunk.format
    return json.dumps(
        {
            "type": "audio",
            "session_id": frame.session_id,
            "track": frame.track,
            "turn_id": frame.turn_id,
            "timestamp": frame.timestamp,
            "sample_rate": fmt.sample_rate,
            "channels": fmt.channels,
            "sample_width": fmt.sample_width,
            "encoding": fmt.encoding,
            "data": base64.b64encode(frame.chunk.data).decode("ascii"),
        }
    )


async def serve_supervisor_websocket(
    ws: SupervisorWebSocket,
    broadcasters: Mapping[str, SessionAudioBroadcaster],
    *,
    expected_token: str | None = None,
    subscribe_timeout_s: float = 10.0,
    allow_unauthenticated: bool = False,
) -> None:
    """Serve one passive supervisor WebSocket connection.

    The client must send ``{"type": "subscribe", "session_id": "..."}``
    after the initial hello. The subscribe message must include a ``token``
    string matching ``expected_token`` unless the caller explicitly opts into
    unauthenticated mode with ``allow_unauthenticated=True``. Once subscribed, the
    helper streams serialized audio frames from the requested
    :class:`SessionAudioBroadcaster` until the caller session closes or the
    supervisor disconnects.
    """
    if expected_token is None and not allow_unauthenticated:
        await _close_supervisor_with_error(
            ws,
            code=4401,
            reason="Unauthorized",
            message=f"Supervisor token is not configured; set {SUPERVISOR_TOKEN_ENV}.",
        )
        return

    await _send_supervisor_json(
        ws,
        {
            "type": "hello",
            "role": "supervisor",
            "auth_required": not allow_unauthenticated,
            "message": 'Send {"type":"subscribe","session_id":"...","token":"..."}',
        },
    )

    try:
        raw = await asyncio.wait_for(_recv_supervisor_message(ws), timeout=subscribe_timeout_s)
    except TimeoutError:
        await _close_supervisor_with_error(
            ws,
            code=4408,
            reason="Subscribe timed out",
            message="Expected subscribe message with session_id within 10 seconds.",
        )
        return

    message = _decode_supervisor_subscribe(raw)
    if message is None:
        await _close_supervisor_with_error(
            ws,
            code=4400,
            reason="Invalid subscribe payload",
            message="Supervisor subscribe payload must be valid JSON text.",
        )
        return

    session_id = message.get("session_id")
    if message.get("type") != "subscribe" or not isinstance(session_id, str):
        await _close_supervisor_with_error(
            ws,
            code=4400,
            reason="Invalid subscribe message",
            message='Expected {"type":"subscribe","session_id":"..."}.',
        )
        return

    if not supervisor_message_authorized(
        message,
        expected_token,
        allow_unauthenticated=allow_unauthenticated,
    ):
        await _close_supervisor_with_error(
            ws,
            code=4401,
            reason="Unauthorized",
            message="Supervisor token is missing or invalid.",
        )
        return

    broadcaster = broadcasters.get(session_id)
    if broadcaster is None:
        await _close_supervisor_with_error(
            ws,
            code=4404,
            reason="Unknown session",
            message=f"No active caller session found for {session_id}.",
        )
        return

    await _stream_supervisor_audio(ws, broadcaster, session_id=session_id)


def _decode_supervisor_subscribe(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, str):
        return None
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return message if isinstance(message, dict) else None


async def _stream_supervisor_audio(
    ws: SupervisorWebSocket,
    broadcaster: SessionAudioBroadcaster,
    *,
    session_id: str,
) -> None:
    listener_id, queue = broadcaster.subscribe()
    logger.info("Supervisor attached to %s", session_id)

    stream_tasks = _supervisor_stream_tasks(broadcaster._session)
    recv_task = stream_tasks.create_task(
        _drain_supervisor_inbound(ws),
        task_name=f"supervisor-recv-{listener_id}",
    )
    if recv_task is None:
        broadcaster.unsubscribe(listener_id)
        return
    try:
        await _send_supervisor_json(
            ws,
            {
                "type": "subscribed",
                "session_id": session_id,
                "listener_count": broadcaster.listener_count,
            },
        )
        while True:
            get_task = stream_tasks.create_task(
                queue.get(),
                task_name=f"supervisor-queue-{listener_id}",
            )
            if get_task is None:
                break
            done, _pending = await asyncio.wait(
                {get_task, recv_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if recv_task in done:
                await recv_task
                break
            frame = get_task.result()
            if frame is None:
                break
            await _send_supervisor_text(ws, supervisor_audio_frame_to_json(frame))
    except ConnectionClosed:
        logger.info("Supervisor disconnected from %s", session_id)
    finally:
        await stream_tasks.cancel_and_drain()
        dropped_frames = broadcaster.dropped_frames_for(listener_id)
        broadcaster.unsubscribe(listener_id)
        logger.info(
            "Supervisor detached from %s (dropped_frames=%s)",
            session_id,
            dropped_frames,
        )


async def _drain_supervisor_inbound(ws: SupervisorWebSocket) -> None:
    try:
        async for _ in ws:
            pass
    except ConnectionClosed:
        return


async def _recv_supervisor_message(ws: SupervisorWebSocket) -> object:
    return await ws.recv()


def _supervisor_stream_tasks(session: Session) -> RuntimeTaskScope:
    """Create one session-owned task cohort for a supervisor connection."""
    counter = getattr(session, _SUPERVISOR_STREAM_COUNTER_ATTR, 0)
    setattr(session, _SUPERVISOR_STREAM_COUNTER_ATTR, counter + 1)
    tasks = RuntimeTaskScope(
        owner_label="supervisor-stream",
        member_name=f"supervisor_stream_{counter}",
        cohort=_SUPERVISOR_STREAM_COHORT,
        logger=logger,
        failure_message="Supervisor stream worker failed",
        graceful_action=RuntimeTaskAction.CANCEL,
        force_action=RuntimeTaskAction.CANCEL,
    )
    runtime_scope = getattr(session, "_runtime_scope", None)
    if isinstance(runtime_scope, RuntimeScope):
        tasks.bind(runtime_scope)
    return tasks


async def _send_supervisor_json(ws: SupervisorWebSocket, payload: Mapping[str, object]) -> None:
    await _send_supervisor_text(ws, json.dumps(payload))


async def _send_supervisor_text(ws: SupervisorWebSocket, payload: str) -> None:
    await ws.send(payload)


async def _close_supervisor_with_error(
    ws: SupervisorWebSocket,
    *,
    code: int,
    reason: str,
    message: str,
) -> None:
    try:
        await _send_supervisor_json(ws, {"type": "error", "message": message})
    except ConnectionClosed:
        return
    await ws.close(code, reason)


def _close_unawaited(value: object) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


def _supervisor_event_tasks(session: Session) -> RuntimeEventTaskScope:
    """Return the one audit-event owner shared by a Session's broadcasters."""
    current = getattr(session, _SUPERVISOR_EVENT_OWNER_ATTR, None)
    if isinstance(current, RuntimeEventTaskScope):
        return current
    tasks = RuntimeEventTaskScope(
        owner_label="supervisor-audit",
        member_name=_SUPERVISOR_EVENT_TASK_NAME,
        cohort=_SUPERVISOR_EVENT_COHORT,
        logger=logger,
        failure_message="Supervisor audit event emission failed",
    )
    runtime_scope = getattr(session, "_runtime_scope", None)
    if isinstance(runtime_scope, RuntimeScope):
        tasks.attach(runtime_scope, name="supervisor-events")
    setattr(session, _SUPERVISOR_EVENT_OWNER_ATTR, tasks)
    return tasks


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
        self._event_tasks = _supervisor_event_tasks(session)

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
        tasks = self._event_tasks.tasks()
        if not tasks:
            return
        if asyncio.current_task() in tasks:
            # Audit subscribers may synchronously tear down their broadcaster.
            # Do not wait for this emitter or siblings joining the same drain.
            return
        scope = self._event_tasks.scope
        assert scope is not None
        await scope.drain(_SUPERVISOR_EVENT_TASK_NAME, suppress_errors=True)
        await self._event_tasks.release_standalone_if_empty()

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
        prepared = self._prepare_frame(frame)
        if prepared is None:
            return
        self._broadcast(prepared)

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
            if inspect.isawaitable(consented):
                _close_unawaited(consented)
                self._consent_blocked_frames += 1
                logger.warning(
                    "Supervisor consent hook returned an awaitable for session %s; "
                    "suppressing frame",
                    self._session.session_id,
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
        if inspect.isawaitable(redacted):
            _close_unawaited(redacted)
            self._redacted_frames += 1
            logger.warning(
                "Supervisor redaction hook returned an awaitable for session %s; "
                "suppressing frame",
                self._session.session_id,
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
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._event_tasks.create_task(
            bus.emit(event),
            task_name="supervisor:audit-emit",
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
