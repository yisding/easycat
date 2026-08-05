"""Shared base classes for transports.

Provides the audio-queue and server plumbing used by the built-in transports
and by out-of-tree transports.  :class:`AudioQueueMixin` and
:class:`ServerTransportBase` are public — import them (together with the
:class:`~easycat.events.TransportDegraded` event they emit) from
``easycat.transports``.  See ``docs/extending/transport.md`` for the
provider-author guide.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import websockets
from websockets.asyncio.server import Server, ServerConnection

from easycat import _observability as observability
from easycat._provider_helpers import get_package_version
from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import EventBus, TransportDegraded
from easycat.runtime._event_tasks import RuntimeEventTaskScope
from easycat.runtime.scope import RuntimeScope
from easycat.transports._browser_events import BrowserEventForwarder
from easycat.transports._limits import (
    DEFAULT_INBOUND_AUDIO_MAX_BYTES,
    MAX_WEBSOCKET_MESSAGE_BYTES,
)

logger = logging.getLogger(__name__)

# Canonical cross-transport ``TransportDegraded.reason`` code.  The inbound
# queue-full drop is shared by every ``AudioQueueMixin`` user (WebSocket /
# WebRTC / WebTransport), so it lives here rather than in any one transport.
# Transport-specific codes stay in their own modules.
_DEGRADED_INBOUND_QUEUE_FULL = "inbound_queue_full"
_DEGRADED_EMIT_MIN_INTERVAL_SECONDS = 1.0
_DEGRADED_MAX_PENDING_TASKS = 64
_DEGRADED_MAX_DETAIL_CHARS = 256
_TRANSPORT_EVENT_TASK_NAME = "transport_event_emit"
_TRANSPORT_EVENT_COHORT = "transport-events"


def _require_positive_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer >= 1")
    return value


def _remember_rollback_cancellation(
    retained: asyncio.CancelledError | None,
    current: asyncio.CancelledError,
    startup_error: BaseException,
) -> asyncio.CancelledError | None:
    """Retain the first new caller cancellation during owned rollback."""
    if retained is None and not isinstance(startup_error, asyncio.CancelledError):
        return current
    return retained


def _raise_rollback_cancellation(
    cancellation: asyncio.CancelledError | None,
    startup_error: BaseException,
    cleanup_error: BaseException | None = None,
) -> None:
    """Deliver a retained caller cancellation after rollback settles."""
    if cancellation is None:
        return
    if cleanup_error is not None:
        startup_error.add_note(f"connect rollback failed: {cleanup_error!r}")
    raise cancellation from startup_error


class _InboundAudioQueue(asyncio.Queue[AudioChunk | None]):
    """Count- and byte-bounded queue for decoded inbound audio."""

    def __init__(self, max_pending_chunks: int, max_pending_bytes: int) -> None:
        super().__init__(
            maxsize=_require_positive_int(
                max_pending_chunks,
                name="max_pending_chunks",
            )
        )
        self._max_pending_bytes = _require_positive_int(
            max_pending_bytes,
            name="max_pending_bytes",
        )
        self._pending_bytes = 0

    @property
    def pending_bytes(self) -> int:
        """Number of audio payload bytes currently retained."""
        return self._pending_bytes

    @property
    def max_pending_bytes(self) -> int:
        """Maximum retained audio payload bytes."""
        return self._max_pending_bytes

    def put_nowait(self, item: AudioChunk | None) -> None:
        item_bytes = len(item.data) if item is not None else 0
        if item_bytes > self._max_pending_bytes - self._pending_bytes:
            raise asyncio.QueueFull
        super().put_nowait(item)
        self._pending_bytes += item_bytes

    def get_nowait(self) -> AudioChunk | None:
        item = super().get_nowait()
        if item is not None:
            self._pending_bytes -= len(item.data)
        return item


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
    transport.  ``AudioQueueMixin._enqueue_chunk`` delegates here, and
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


def make_version_info(
    provider: str, sdk_package: str, *, api_version: str = "unknown"
) -> dict[str, str]:
    """Stable-shape transport version dict (provider/model/api_version/sdk_version)."""
    return {
        "provider": provider,
        "model": "unknown",
        "api_version": api_version,
        "sdk_version": get_package_version(sdk_package),
    }


# ── Shared queue / receive_audio logic ────────────────────────────


class AudioQueueMixin:
    """Mixin that provides the inbound audio queue and ``receive_audio`` iterator.

    Transports that accept audio chunks from an external source can inherit
    this mixin to get the queue management, sentinel-based shutdown, and
    ``receive_audio()`` async iterator for free.

    Also provides a ``_client_connected`` :class:`asyncio.Event` and a
    ``wait_for_client`` helper so that server-style transports can signal
    when a remote peer has connected.

    Users must:
      - Call ``_init_audio_queue(max_pending_chunks, max_pending_bytes)`` during
        ``__init__``.
      - Set ``self._connected`` to ``True``/``False`` in ``connect``/``disconnect``.
      - Call ``_enqueue_sentinel()`` during ``disconnect`` to signal end-of-stream.
    """

    _connected: bool
    _in_queue: _InboundAudioQueue
    _client_connected: asyncio.Event
    _event_bus: EventBus | None
    _easycat_session_id: str | None
    _event_tasks: RuntimeEventTaskScope
    _degraded_last_emit: dict[tuple[str, bool], float]
    _degraded_suppressed: dict[tuple[str, bool], int]
    _browser_event_forwarder: BrowserEventForwarder | None

    def _init_audio_queue(
        self,
        max_pending_chunks: int,
        max_pending_bytes: int = DEFAULT_INBOUND_AUDIO_MAX_BYTES,
    ) -> None:
        self._max_pending_chunks = max_pending_chunks
        self._max_pending_bytes = max_pending_bytes
        self._connected = False
        self._in_queue = _InboundAudioQueue(
            max_pending_chunks=max_pending_chunks,
            max_pending_bytes=max_pending_bytes,
        )
        self._client_connected = asyncio.Event()
        # Optional session EventBus. Attached post-construction through the
        # public ``set_event_bus`` capability, so ``_emit_degraded`` reads it
        # live. Preserve a value a subclass already set via constructor
        # injection (e.g. Twilio transports pass ``event_bus`` before calling
        # this) — only default it when unset.
        self._event_bus = getattr(self, "_event_bus", None)
        # Session correlation is attached post-construction, just like the
        # EventBus. Preserve an early binding if a concrete transport already
        # received one before queue initialization.
        self._easycat_session_id = getattr(self, "_easycat_session_id", None)
        # Fire-and-forget event tasks are retained in a runtime scope so they
        # stay strongly owned without creating a second lifecycle registry.
        # Session attaches its root after construction; standalone transports
        # lazily create and drain a local root.
        event_tasks = getattr(self, "_event_tasks", None)
        if event_tasks is None:
            transport = getattr(self, "transport_kind", "unknown")
            self._event_tasks = RuntimeEventTaskScope(
                owner_label=f"{transport}-transport",
                member_name=_TRANSPORT_EVENT_TASK_NAME,
                cohort=_TRANSPORT_EVENT_COHORT,
                logger=logger,
                failure_message="Transport event emission failed",
            )
        # Per-reason coalescing for attacker-triggerable drop/control paths.
        self._degraded_last_emit = getattr(self, "_degraded_last_emit", {})
        self._degraded_suppressed = getattr(self, "_degraded_suppressed", {})
        # Browser event channel (transcripts / interruptions / latency) for
        # transports that opt in via ``_ensure_browser_event_forwarder``.
        self._browser_event_forwarder = getattr(self, "_browser_event_forwarder", None)

    def set_event_bus(self, event_bus: EventBus) -> None:
        """Attach the session bus unless construction supplied an explicit bus."""
        if self._event_bus is None:
            self._event_bus = event_bus

    def set_session_id(self, session_id: str) -> None:
        """Attach the owning Session correlation ID to producer-side events."""
        self._easycat_session_id = session_id

    def set_runtime_scope(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach transport event work beneath the owning application scope."""
        self._event_tasks.attach(parent, name=name)

    @property
    def _emit_scope(self) -> RuntimeScope | None:
        """Compatibility inspection of the transport event scope."""
        return self._event_tasks.scope

    @property
    def _owns_emit_root(self) -> bool:
        """Compatibility inspection of standalone transport ownership."""
        return self._event_tasks.owns_root

    @property
    def _emit_tasks(self) -> set[asyncio.Task[Any]]:
        """Compatibility inspection of transport events owned by the scope."""
        return set(self._event_tasks.tasks())

    def _ensure_emit_scope(self) -> RuntimeScope:
        return self._event_tasks.ensure_scope()

    def _create_emit_task(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        task_name: str,
    ) -> asyncio.Task[Any] | None:
        """Create one scope-owned, self-pruning best-effort event task."""
        return self._event_tasks.create_task(coro, task_name=task_name)

    def _track_emit_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Adopt an existing event task for re-entrant lifecycle helpers."""
        return self._event_tasks.adopt_task(task)

    def _record_transport_disconnect(self, reason: str) -> None:
        """Count one abnormal transport disconnect (a drop, not a clean close).

        Clean, application-initiated teardown (``disconnect()``, a client's
        normal WebSocket close, a Twilio ``stop`` frame) is expected and is
        *not* counted here. Only abnormal drops — a ``ConnectionClosedError``,
        an ICE ``failed``/``disconnected`` transition, or a fatal protocol
        violation that forces the session down — increment the counter, so the
        metric tracks reliability problems rather than ordinary hang-ups. The
        low-cardinality ``easycat.transport`` label carries the transport kind;
        ``reason`` stays in logs/journal and is intentionally not a metric
        attribute (it can be high-cardinality / attacker-influenced).
        """
        observability.increment_counter(
            "easycat.transport.disconnects.total",
            attributes={"easycat.transport": getattr(self, "transport_kind", "unknown")},
        )
        logger.debug("Recorded abnormal transport disconnect: %s", reason)

    def _emit_degraded(self, reason: str, detail: str = "", *, fatal: bool = False) -> None:
        """Publish a :class:`TransportDegraded` on the session event bus.

        Scheduled, never awaited: called from synchronous callbacks and audio
        hot paths where blocking on handler dispatch would stall the
        transport.  A no-op until Session attaches the bus
        (:meth:`Session._maybe_attach_event_bus`) and whenever there is no
        running loop (e.g. a unit test driving the transport synchronously) —
        observability is never load-bearing.

        A ``fatal`` degradation forces the session down (protocol violation,
        rejected-stream flood, poisoned control codec), so it also counts as
        an abnormal transport disconnect.
        """
        if fatal:
            self._record_transport_disconnect(reason)
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

        # Truncate the (attacker-controllable) original detail FIRST, then
        # append the short, bounded suppression summary.  Appending before
        # truncating let a padded >256-char detail evict the suppression
        # count, hiding how many drops were coalesced.
        detail = _truncate_degraded_detail(detail)
        suppressed = self._degraded_suppressed.pop(key, 0)
        if suppressed:
            detail = (
                f"{detail}; suppressed {suppressed} similar events"
                if detail
                else (f"suppressed {suppressed} similar events")
            )
        self._degraded_last_emit[key] = now
        event = TransportDegraded(
            provider=getattr(self, "transport_kind", "unknown"),
            reason=reason,
            detail=detail,
            fatal=fatal,
            session_id=self._easycat_session_id,
        )
        self._create_emit_task(
            bus.emit(event),
            task_name=f"{getattr(self, 'transport_kind', 'unknown')}:degraded-emit",
        )

    async def _drain_emit_tasks(
        self,
        *,
        exclude_task: asyncio.Task[Any] | None = None,
    ) -> None:
        """Await any in-flight fire-and-forget ``_emit_degraded`` tasks.

        Called from ``disconnect`` so a transport torn down with emit tasks
        still pending does not leave them dangling into interpreter shutdown
        ("Task was destroyed but it is pending"). Late emits are already safe
        (the journal sink no-ops after Session finalization), so this is
        lifecycle tidiness, not correctness. ``exclude_task`` lets a child
        cleanup transaction avoid waiting on the event-emitter task that
        initiated its parent teardown.
        """
        scope = self._event_tasks.scope
        if scope is None or not scope.tasks(_TRANSPORT_EVENT_TASK_NAME):
            return
        # A subscriber runs inside its EventBus emitter task. It may call a
        # transport teardown method that drains diagnostics, so never await
        # that current task (or an explicitly supplied parent emitter): doing
        # so creates a self-await cycle and strands the diagnostic forever.
        excluded = {task for task in (asyncio.current_task(), exclude_task) if task is not None}
        pending = [
            task for task in scope.tasks(_TRANSPORT_EVENT_TASK_NAME) if task not in excluded
        ]
        if pending:
            # Keep the established cancellation behavior: cancelling this
            # gather cancels its snapshotted best-effort emitters as well.
            await asyncio.gather(*pending, return_exceptions=True)
            for task in pending:
                scope.discard(task)
        await self._event_tasks.release_standalone_if_empty()

    # ── Browser event channel ─────────────────────────────────────
    #
    # Browser-facing transports (WebSocket / WebRTC) forward session events
    # (transcripts, interruptions, per-turn latency) to the connected client
    # as JSON messages; see ``transports/_browser_events.py`` for the wire
    # format. Transports opt in by implementing ``_send_client_event`` and
    # calling the ensure/close pair from ``connect``/``disconnect``.

    def _ensure_browser_event_forwarder(self) -> None:
        """Start forwarding session events to the browser when a bus is attached."""
        if getattr(self, "_browser_event_forwarder", None) is not None:
            return
        if self._event_bus is None:
            return
        self._browser_event_forwarder = BrowserEventForwarder(
            self._event_bus, self._send_client_event
        )

    def _close_browser_event_forwarder(self) -> None:
        forwarder = getattr(self, "_browser_event_forwarder", None)
        if forwarder is not None:
            forwarder.close()
            self._browser_event_forwarder = None

    async def _send_client_event(self, payload: dict[str, Any]) -> None:
        """Send one JSON event message to the connected client (best effort)."""
        raise NotImplementedError

    def _reset_audio_queue(self) -> None:
        """Reinitialize the queue to clear any stale sentinels from a previous session."""
        self._in_queue = _InboundAudioQueue(
            max_pending_chunks=self._max_pending_chunks,
            max_pending_bytes=self._max_pending_bytes,
        )

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


class ServerTransportBase(AudioQueueMixin):
    """Base for transports that host a ``websockets`` server.

    Subclasses must provide:
      - ``_transport_name`` (str) — used in log messages (e.g. ``"WebSocket"``).
      - ``_handle_connection(ws)`` — the per-connection coroutine passed to
        ``websockets.serve``.
    """

    _transport_name: str = "Server"

    def __init__(
        self,
        host: str,
        port: int,
        max_pending_chunks: int,
        max_pending_bytes: int = DEFAULT_INBOUND_AUDIO_MAX_BYTES,
    ) -> None:
        self._host = host
        self._port = port
        self._init_audio_queue(max_pending_chunks, max_pending_bytes)

        self._server: Server | None = None
        self._ws: ServerConnection | None = None
        self._pending_client_close: ServerConnection | None = None
        self._client_close_error: Exception | None = None
        # A disconnect is a retained transaction spanning the accepted client,
        # listener shutdown, sentinel publication, and diagnostic-task drain.
        # ``_connected`` is cleared before its first await, while this ledger
        # remains set until every exact cleanup owner has completed. A later
        # disconnect retries the unfinished steps; connect must never treat the
        # stale pre-disconnect ``_connected`` value as a live listener.
        self._disconnect_cleanup_pending = False
        self._disconnect_cleanup_error: Exception | None = None
        self._server_wait_task: asyncio.Future[Any] | None = None
        self._disconnect_emit_cleanup_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        """Start the WebSocket server."""
        async with self._lifecycle_lock:
            if self._pending_client_close is not None:
                raise RuntimeError(
                    f"{self._transport_name} client cleanup is incomplete; "
                    "call disconnect() again before reconnecting"
                ) from (self._disconnect_cleanup_error or self._client_close_error)
            if self._disconnect_cleanup_pending:
                raise RuntimeError(
                    f"{self._transport_name} cleanup is incomplete; "
                    "call disconnect() again before reconnecting"
                ) from self._disconnect_cleanup_error
            if self._connected:
                return

            self._reset_audio_queue()

            self._server = await websockets.serve(
                self._handle_connection,
                self._host,
                self._port,
                compression=None,
                max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
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
        async with self._lifecycle_lock:
            if self._disconnect_is_idle():
                return

            self._begin_disconnect_cleanup()
            cleanup_errors: list[Exception] = []
            await self._close_client_for_disconnect(cleanup_errors)
            await self._close_server_for_disconnect(cleanup_errors)
            self._enqueue_sentinel()
            await self._drain_diagnostics_for_disconnect(cleanup_errors)

            if cleanup_errors:
                self._disconnect_cleanup_error = cleanup_errors[0]
                raise cleanup_errors[0]

            # ``wait_closed`` has reaped every accepted handler. Clear any
            # protocol pointer a handler already released, then admit reuse.
            self._ws = None
            self._disconnect_cleanup_pending = False
            self._disconnect_cleanup_error = None

    def _disconnect_is_idle(self) -> bool:
        return (
            not self._connected
            and not self._disconnect_cleanup_pending
            and self._pending_client_close is None
        )

    def _begin_disconnect_cleanup(self) -> None:
        """Publish shutdown before the first await and retain cleanup admission."""
        self._connected = False
        self._client_connected.clear()
        self._disconnect_cleanup_pending = True
        self._disconnect_cleanup_error = None

    async def _close_client_for_disconnect(self, cleanup_errors: list[Exception]) -> None:
        client = self._pending_client_close or self._ws
        if client is None:
            return
        self._pending_client_close = client
        caller = asyncio.current_task()
        cancellation_requests = caller.cancelling() if caller is not None else 0
        try:
            await client.close()
        except asyncio.CancelledError:
            interrupted = RuntimeError(f"{self._transport_name} client close was interrupted")
            self._client_close_error = interrupted
            self._disconnect_cleanup_error = interrupted
            if caller is not None and caller.cancelling() > cancellation_requests:
                raise
            logger.debug(
                "%s WebSocket client close cancelled internally",
                self._transport_name,
                exc_info=True,
            )
            cleanup_errors.append(interrupted)
        except Exception as exc:
            logger.debug(
                "Error closing %s WebSocket",
                self._transport_name,
                exc_info=True,
            )
            cleanup_errors.append(exc)
            self._client_close_error = exc
        else:
            if self._ws is client:
                self._ws = None
            self._pending_client_close = None
            self._client_close_error = None

    def _start_server_wait_for_disconnect(
        self,
        server: Server,
        cleanup_errors: list[Exception],
    ) -> asyncio.Future[Any] | None:
        wait_task = self._server_wait_task
        if wait_task is not None:
            return wait_task
        try:
            server.close()
        except Exception as exc:
            logger.debug(
                "Error closing %s server listener",
                self._transport_name,
                exc_info=True,
            )
            cleanup_errors.append(exc)
            return None
        wait_task = asyncio.ensure_future(server.wait_closed())
        self._server_wait_task = wait_task
        return wait_task

    async def _close_server_for_disconnect(self, cleanup_errors: list[Exception]) -> None:
        server = self._server
        if server is None:
            return
        wait_task = self._start_server_wait_for_disconnect(server, cleanup_errors)
        if wait_task is None:
            return
        caller = asyncio.current_task()
        cancellation_requests = caller.cancelling() if caller is not None else 0
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError as cancellation:
            self._handle_server_wait_cancellation(
                wait_task,
                cleanup_errors,
                cancellation,
                caller=caller,
                cancellation_requests=cancellation_requests,
            )
        except Exception as exc:
            logger.debug(
                "Error waiting for %s server listener to close",
                self._transport_name,
                exc_info=True,
            )
            cleanup_errors.append(exc)
            if self._server_wait_task is wait_task:
                self._server_wait_task = None
        else:
            if self._server is server:
                self._server = None
            if self._server_wait_task is wait_task:
                self._server_wait_task = None

    def _handle_server_wait_cancellation(
        self,
        wait_task: asyncio.Future[Any],
        cleanup_errors: list[Exception],
        cancellation: asyncio.CancelledError,
        *,
        caller: asyncio.Task[Any] | None,
        cancellation_requests: int,
    ) -> None:
        interrupted = RuntimeError(f"{self._transport_name} server close was interrupted")
        self._disconnect_cleanup_error = interrupted
        if wait_task.cancelled() and self._server_wait_task is wait_task:
            self._server_wait_task = None
        if caller is not None and caller.cancelling() > cancellation_requests:
            raise cancellation
        cleanup_errors.append(interrupted)

    async def _drain_diagnostics_for_disconnect(
        self,
        cleanup_errors: list[Exception],
    ) -> None:
        caller = asyncio.current_task()
        if caller is not None and caller in self._emit_tasks:
            # Diagnostic emitters execute application subscribers. More than
            # one subscriber can concurrently request disconnect: the first
            # owns ``_lifecycle_lock`` while a sibling waits for it. Awaiting
            # that sibling here would form caller -> sibling -> lifecycle-lock
            # cycle. Diagnostic draining is lifecycle tidiness only; once this
            # disconnect releases the lock, every emitter can finish normally.
            return
        emit_cleanup_task = self._disconnect_emit_cleanup_task
        if emit_cleanup_task is None:
            emit_cleanup_task = asyncio.create_task(
                self._drain_emit_tasks(),
                name=f"{self._transport_name.lower()}_diagnostic_cleanup",
            )
            self._disconnect_emit_cleanup_task = emit_cleanup_task
        caller = asyncio.current_task()
        cancellation_requests = caller.cancelling() if caller is not None else 0
        try:
            await asyncio.shield(emit_cleanup_task)
        except asyncio.CancelledError:
            interrupted = RuntimeError(
                f"{self._transport_name} diagnostic cleanup was interrupted"
            )
            self._disconnect_cleanup_error = interrupted
            if (
                emit_cleanup_task.cancelled()
                and self._disconnect_emit_cleanup_task is emit_cleanup_task
            ):
                self._disconnect_emit_cleanup_task = None
            if caller is not None and caller.cancelling() > cancellation_requests:
                raise
            cleanup_errors.append(interrupted)
        except Exception as exc:
            logger.debug(
                "Error draining %s diagnostic tasks",
                self._transport_name,
                exc_info=True,
            )
            cleanup_errors.append(exc)
            if self._disconnect_emit_cleanup_task is emit_cleanup_task:
                self._disconnect_emit_cleanup_task = None
        else:
            if self._disconnect_emit_cleanup_task is emit_cleanup_task:
                self._disconnect_emit_cleanup_task = None

    @property
    def has_client(self) -> bool:
        return self._ws is not None
