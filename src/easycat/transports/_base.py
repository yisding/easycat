"""Internal base classes for transports.

Provides shared infrastructure used by multiple transport implementations.
Not part of the public API — ``__init__.py`` does not export this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

import websockets
from websockets.asyncio.server import Server, ServerConnection

from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import EventBus, TransportDegraded

logger = logging.getLogger(__name__)

# Canonical cross-transport ``TransportDegraded.reason`` code.  The inbound
# queue-full drop is shared by every ``_AudioQueueMixin`` user (WebSocket /
# WebRTC / WebTransport), so it lives here rather than in any one transport.
# Transport-specific codes stay in their own modules.
_DEGRADED_INBOUND_QUEUE_FULL = "inbound_queue_full"
_DEGRADED_EMIT_MIN_INTERVAL_SECONDS = 1.0
_DEGRADED_MAX_PENDING_TASKS = 64
_DEGRADED_MAX_DETAIL_CHARS = 256


def _truncate_degraded_detail(detail: str) -> str:
    """Bound attacker-controlled diagnostic detail before task/journal emission."""
    if len(detail) <= _DEGRADED_MAX_DETAIL_CHARS:
        return detail
    omitted = len(detail) - _DEGRADED_MAX_DETAIL_CHARS
    return f"{detail[:_DEGRADED_MAX_DETAIL_CHARS]}… (truncated {omitted} chars)"


def _enqueue_inbound_chunk(
    queue: asyncio.Queue[AudioChunk | None],
    chunk: AudioChunk,
    *,
    emit_degraded: Callable[..., None],
    context: str,
) -> None:
    """Best-effort enqueue for inbound audio, dropping + degrading when full.

    The single definition of the inbound queue-full drop path, shared by every
    transport.  ``_AudioQueueMixin._enqueue_chunk`` delegates here, and
    standalone session helpers that hold an injected queue + emitter (e.g.
    WebTransport's per-session helper) call it directly so the drop message,
    degraded code, and logging stay in lock-step.

    Parameters
    ----------
    queue:
        The inbound audio queue.
    chunk:
        The audio chunk to enqueue.
    emit_degraded:
        Callable matching ``_emit_degraded(reason, detail, *, fatal=False)``
        used to surface the drop on the session event bus.
    context:
        Log-friendly transport/context name used when the queue is full.
    """
    try:
        queue.put_nowait(chunk)
    except asyncio.QueueFull:
        logger.warning("Inbound %s audio queue full — dropping frame", context)
        emit_degraded(
            _DEGRADED_INBOUND_QUEUE_FULL,
            f"dropped {len(chunk.data)}-byte {context} frame; inbound queue full",
        )


# ── Shared queue / receive_audio logic ────────────────────────────


class _AudioQueueMixin:
    """Mixin that provides the inbound audio queue and ``receive_audio`` iterator.

    Transports that accept audio chunks from an external source can inherit
    this mixin to get the queue management, sentinel-based shutdown, and
    ``receive_audio()`` async iterator for free.

    Also provides a ``_client_connected`` :class:`asyncio.Event` and a
    ``wait_for_client`` helper so that server-style transports can signal
    when a remote peer has connected.

    Users must:
      - Call ``_init_audio_queue(max_pending_chunks)`` during ``__init__``.
      - Set ``self._connected`` to ``True``/``False`` in ``connect``/``disconnect``.
      - Call ``_enqueue_sentinel()`` during ``disconnect`` to signal end-of-stream.
    """

    _connected: bool
    _in_queue: asyncio.Queue[AudioChunk | None]
    _client_connected: asyncio.Event
    _event_bus: EventBus | None
    _emit_tasks: set[asyncio.Task[None]]
    _degraded_last_emit: dict[tuple[str, bool], float]
    _degraded_suppressed: dict[tuple[str, bool], int]

    def _init_audio_queue(self, max_pending_chunks: int) -> None:
        self._max_pending_chunks = max_pending_chunks
        self._connected = False
        self._in_queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(
            maxsize=max_pending_chunks,
        )
        self._client_connected = asyncio.Event()
        # Optional session EventBus.  Attached post-construction by Session
        # via ``_maybe_attach_event_bus`` (which only sets ``_event_bus``
        # while it is None), so ``_emit_degraded`` reads it live.  Preserve a
        # value a subclass already set via constructor injection (e.g.
        # Twilio transports pass ``event_bus`` before calling this) — only
        # default it when unset.
        self._event_bus = getattr(self, "_event_bus", None)
        # Fire-and-forget ``bus.emit`` tasks, tracked so they are not GC'd
        # mid-flight.  Observability must never block a transport hot path,
        # so emission is scheduled, not awaited.
        self._emit_tasks = getattr(self, "_emit_tasks", set())
        # Per-reason coalescing for attacker-triggerable drop/control paths.
        self._degraded_last_emit = getattr(self, "_degraded_last_emit", {})
        self._degraded_suppressed = getattr(self, "_degraded_suppressed", {})

    def _emit_degraded(self, reason: str, detail: str = "", *, fatal: bool = False) -> None:
        """Publish a :class:`TransportDegraded` on the session event bus.

        Scheduled, never awaited: called from synchronous callbacks and audio
        hot paths where blocking on handler dispatch would stall the
        transport.  A no-op until Session attaches the bus
        (:meth:`Session._maybe_attach_event_bus`) and whenever there is no
        running loop (e.g. a unit test driving the transport synchronously) —
        observability is never load-bearing.
        """
        bus = self._event_bus
        if bus is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        key = (reason, fatal)
        now = loop.time()
        last = self._degraded_last_emit.get(key)
        if not fatal and last is not None and now - last < _DEGRADED_EMIT_MIN_INTERVAL_SECONDS:
            self._degraded_suppressed[key] = self._degraded_suppressed.get(key, 0) + 1
            return
        if not fatal and len(self._emit_tasks) >= _DEGRADED_MAX_PENDING_TASKS:
            self._degraded_suppressed[key] = self._degraded_suppressed.get(key, 0) + 1
            return

        suppressed = self._degraded_suppressed.pop(key, 0)
        if suppressed:
            detail = (
                f"{detail}; suppressed {suppressed} similar events"
                if detail
                else (f"suppressed {suppressed} similar events")
            )
        detail = _truncate_degraded_detail(detail)
        self._degraded_last_emit[key] = now
        event = TransportDegraded(
            provider=getattr(self, "transport_kind", "unknown"),
            reason=reason,
            detail=detail,
            fatal=fatal,
        )
        task = loop.create_task(bus.emit(event))
        self._emit_tasks.add(task)
        task.add_done_callback(self._emit_tasks.discard)

    async def _drain_emit_tasks(self) -> None:
        """Await any in-flight fire-and-forget ``_emit_degraded`` tasks.

        Called from ``disconnect`` so a transport torn down with emit tasks
        still pending does not leave them dangling into interpreter shutdown
        ("Task was destroyed but it is pending"). Late emits are already safe
        (the journal sink no-ops after :meth:`Session._destroy`), so this is
        lifecycle tidiness, not correctness.
        """
        if not self._emit_tasks:
            return
        # Snapshot: the done-callback mutates ``_emit_tasks`` during gather.
        pending = list(self._emit_tasks)
        await asyncio.gather(*pending, return_exceptions=True)

    def _reset_audio_queue(self) -> None:
        """Reinitialize the queue to clear any stale sentinels from a previous session."""
        self._in_queue = asyncio.Queue(maxsize=self._max_pending_chunks)

    def _drain_audio_queue(self) -> int:
        """Remove all currently queued inbound audio without replacing the queue."""
        drained = 0
        while True:
            try:
                self._in_queue.get_nowait()
            except asyncio.QueueEmpty:
                return drained
            drained += 1

    def _enqueue_sentinel(self) -> None:
        """Put ``None`` on the queue to signal end-of-stream.

        The sentinel is critical for unblocking ``receive_audio()``, so if the
        queue is full we drain one item to make room rather than silently
        dropping the signal.
        """
        try:
            self._in_queue.put_nowait(None)
        except asyncio.QueueFull:
            # Drop one audio chunk to make room — shutdown signals must not
            # be silently lost.
            try:
                self._in_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._in_queue.put_nowait(None)
            except asyncio.QueueFull:
                logger.debug("Input queue full when enqueueing sentinel; ignoring")

    def _enqueue_chunk(self, chunk: AudioChunk, *, context: str) -> None:
        """Best-effort enqueue for inbound audio data.

        Parameters
        ----------
        chunk:
            The audio chunk to enqueue.
        context:
            Log-friendly transport/context name used when the queue is full.
        """
        _enqueue_inbound_chunk(
            self._in_queue,
            chunk,
            emit_degraded=self._emit_degraded,
            context=context,
        )

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        """Yield audio chunks until a ``None`` sentinel is received."""
        while True:
            chunk = await self._in_queue.get()
            if chunk is None:
                break
            yield chunk

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def wait_for_client(self, timeout: float | None = None) -> None:
        """Block until a remote peer / client connects (or *timeout* expires)."""
        await asyncio.wait_for(self._client_connected.wait(), timeout=timeout)

    def version_info(self) -> dict[str, str]:
        """Return stable-shape dict identifying this transport."""
        return {
            "provider": "unknown",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": "unknown",
        }

    @property
    def audio_format(self) -> AudioFormat:
        """Expose the transport's internal PCM contract when available."""
        try:
            return self._audio_format  # type: ignore[attr-defined]
        except AttributeError as exc:  # pragma: no cover - defensive guard
            raise AttributeError(f"{type(self).__name__} does not expose an audio_format") from exc


# ── WebSocket server base ─────────────────────────────────────────


class _ServerTransportBase(_AudioQueueMixin):
    """Base for transports that host a ``websockets`` server.

    Subclasses must provide:
      - ``_transport_name`` (str) — used in log messages (e.g. ``"WebSocket"``).
      - ``_handle_connection(ws)`` — the per-connection coroutine passed to
        ``websockets.serve``.
    """

    _transport_name: str = "Server"

    def __init__(self, host: str, port: int, max_pending_chunks: int) -> None:
        self._host = host
        self._port = port
        self._init_audio_queue(max_pending_chunks)

        self._server: Server | None = None
        self._ws: ServerConnection | None = None

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        """Start the WebSocket server."""
        if self._connected:
            return

        self._reset_audio_queue()

        self._server = await websockets.serve(
            self._handle_connection,
            self._host,
            self._port,
        )
        self._connected = True
        logger.info(
            "%s transport listening on ws://%s:%d",
            self._transport_name,
            self._host,
            self._port,
        )

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """Override in subclasses to handle a new WebSocket connection."""
        raise NotImplementedError

    async def disconnect(self) -> None:
        """Disconnect the current client and stop the server."""
        if not self._connected:
            return

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("Error closing %s WebSocket", self._transport_name, exc_info=True)
            self._ws = None
        self._client_connected.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        self._enqueue_sentinel()
        self._connected = False
        await self._drain_emit_tasks()

    @property
    def has_client(self) -> bool:
        return self._ws is not None
