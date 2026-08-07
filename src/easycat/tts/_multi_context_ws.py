"""Shared persistent multi-context WebSocket manager for streaming TTS.

This is the opt-in counterpart to the one-shot-per-socket model used by the
WebSocket TTS providers. Where the default path opens a fresh
:class:`~easycat.reconnecting_ws.ReconnectingWebSocket` per ``synthesize`` call
and tears it down in a ``finally`` block, this manager keeps ONE socket alive
across many sequential utterances, each scoped by a per-utterance
``context_id``.

Design goals and the correctness hardenings baked in:

- **Pure orchestration.** This module knows nothing about wire formats, JSON
  shapes, base64, or terminators. All provider-specific behaviour lives in the
  frozen :class:`MultiContextAdapter` callback bundle, so each provider keeps
  its decode/encode logic verbatim.
- **Single demux reader, parse once.** Exactly ONE background task iterates the
  socket's ``recv_iter()``, parses each frame ONCE via
  :meth:`MultiContextAdapter.parse_frame`, routes the parsed object to the owning
  context's bounded queue via :meth:`MultiContextAdapter.route_key`, and queues
  the parsed object so the consumer's decode loop never re-parses. A persistent
  socket can never have two concurrent ``recv_iter()`` calls — the classic
  concurrent-recv hazard on ``websockets`` — because the reader is the sole
  consumer.
- **Send serialization.** The persistent model has genuinely concurrent
  senders: the reader-driven reconnect replay hook and the ``synthesize()``
  caller. A single :class:`asyncio.Lock` serializes every ``ws.send`` so frames
  never interleave on the wire.
- **Socket warmth via WebSocket ping/pong.** There is no application-level
  keepalive. The underlying ``websockets`` client sends protocol-level
  ping frames on its default ``ping_interval`` to keep the socket warm between
  turns. After a very long idle gap that exceeds the server's per-context
  inactivity timeout the socket may be closed server-side; the next utterance
  transparently reconnects a fresh socket (graceful degradation), at the cost
  of one cold connect on that turn.
- **Mandatory recv-side context filtering.** Callers must still drop frames
  whose context id does not match the active utterance; here the reader drops
  any frame routed to a context that has been cancelled, so a stray late frame
  from a superseded turn cannot bleed into the next one.
- **Manager-owned reconnect replay.** The socket is built with the manager's
  own ``on_reconnect`` hook (NOT the provider's single-context replay), which
  replays every live, armed, non-cancelled context.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from easycat.runtime.scope import (
    BackgroundTaskScope,
    RuntimeMemberPolicy,
    RuntimeScope,
    RuntimeScopeState,
    RuntimeTaskAction,
    RuntimeTaskPolicy,
)

logger = logging.getLogger(__name__)

# Sentinels pushed onto a context queue to preserve consumer-visible ordering.
_TERMINAL = object()
_REPLAY_BOUNDARY = object()

# Short timeout for the cancel-frame send. A barge-in must stay near-instant, so
# we never let the cancel send block on a reconnect window; on timeout we fall
# back to closing the socket outright.
_CANCEL_SEND_TIMEOUT = 0.5
# A graceful close frame is best-effort too: a wedged sender must not hold the
# connection lock and strand every close/connect waiter forever.
_SOCKET_CLOSE_SEND_TIMEOUT = 0.5
_READER_TASK = "tts_receive_loop"
_CLOSE_FINALIZER = "tts-socket-close"
_TTS_RECEIVE_FINISH_POLICY = RuntimeTaskPolicy(
    graceful=RuntimeMemberPolicy(
        cohort="tts-receive",
        signal_token=False,
        task_action=RuntimeTaskAction.FINISH,
    ),
    force=RuntimeMemberPolicy(
        cohort="tts-receive",
        signal_token=False,
        task_action=RuntimeTaskAction.FINISH,
    ),
)


def validate_context_queue_maxsize(value: object, *, provider: str | None = None) -> None:
    """Require a truly bounded positive integer context queue."""
    label = f"{provider} context_queue_maxsize" if provider else "context_queue_maxsize"
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be an integer >= 1")


@dataclass(frozen=True)
class MultiContextAdapter:
    """Provider callbacks describing how to drive a multi-context socket.

    Frozen so it is safe to share. The manager calls these to build the
    socket, route incoming frames, and produce the provider-specific control
    frames; it never inspects frame contents itself.
    """

    # Build a fresh ReconnectingWebSocket. The provider MUST wire the manager's
    # ``on_reconnect`` hook here (passed in as the single argument), not its own
    # single-context replay, so reconnect replays every live context.
    connect_factory: Callable[[Callable[[], Awaitable[None]]], Any]
    # Parse a raw wire frame into the provider's decoded object (a dict), or
    # ``None`` to ignore it (non-text/binary, unparseable). The frame is parsed
    # exactly ONCE here; the parsed object is what gets routed, queued, and
    # handed to the consumer — the decode loop never re-parses.
    parse_frame: Callable[[Any], Any | None]
    # Map a *parsed* frame to the context id that owns it, or ``None`` for a
    # global/unroutable frame (errors without a context, etc.).
    route_key: Callable[[Any], str | None]
    # Frames to cancel/close a single context without closing the socket.
    context_cancel_frames: Callable[[str], list[str]]
    # Called by the context consumer at the replay boundary (resets sample carry).
    on_context_replay: Callable[[str], None]
    # Frames to send to gracefully close the whole socket.
    socket_close_frames: Callable[[], list[str]]
    # Handle a *parsed* frame with no routable context id (e.g. emit an error).
    on_global_frame: Callable[[Any], None]
    # Bounded per-context queue size for the demux reader.
    context_queue_maxsize: int = 256

    def __post_init__(self) -> None:
        validate_context_queue_maxsize(self.context_queue_maxsize)


@dataclass
class _Context:
    """Per-utterance handle owned by the manager."""

    context_id: str
    queue: asyncio.Queue[Any]
    on_replay: Callable[[str], None]
    done: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False
    # ``None`` until the caller's frames have been sent successfully; once
    # armed, the reconnect hook replays them.
    pending_frames: list[str] | None = None
    # Terminal failure (connection death / reconnect-budget exhaustion) recorded
    # by the reader when the socket dies mid-utterance. Re-raised from frames()
    # so the persistent path surfaces a provider error + raise, matching the
    # one-shot path instead of looking like a clean truncated completion.
    error: BaseException | None = None

    async def frames(self):
        """Yield raw frames until a terminal sentinel.

        Frames are silently dropped once the context is cancelled so a stray
        late frame from a superseded turn cannot leak to the caller. If the
        reader recorded a terminal error (socket died mid-utterance) and the
        context was not deliberately cancelled, that error is raised after the
        sentinel so the caller sees a real failure rather than a clean end.
        """
        while True:
            # Drain whatever is buffered first. If the socket died but a
            # terminal frame (done/isFinal) was already buffered, the consumer
            # processes it and completes normally — so only a genuine truncation
            # (queue fully drained with no terminal consumed) surfaces the error.
            if self.queue.empty() and self.done.is_set():
                if self.error is not None and not self.cancelled:
                    raise self.error
                return
            item = await self.queue.get()
            if item is _TERMINAL:
                if self.error is not None and not self.cancelled:
                    raise self.error
                return
            if item is _REPLAY_BOUNDARY:
                if not self.cancelled:
                    self.on_replay(self.context_id)
                continue
            if self.cancelled:
                continue
            yield item


class MultiContextWSManager:
    """Owns one persistent socket shared across many context-scoped utterances."""

    def __init__(
        self,
        adapter: MultiContextAdapter,
        *,
        runtime_scope: RuntimeScope | None = None,
    ) -> None:
        self._adapter = adapter
        self._runtime_scope = runtime_scope or RuntimeScope(name="tts-multi-context-runtime")
        self._owns_runtime_scope = runtime_scope is None
        self._ws: Any | None = None
        # Exact socket retained when physical close fails. No replacement may
        # be created until that same wrapper closes successfully.
        self._pending_socket_close: Any | None = None
        self._socket_close_error: BaseException | None = None
        self._contexts: dict[str, _Context] = {}
        self._reader_task: asyncio.Task[None] | None = None
        # Serializes every ws.send across the reconnect-replay hook and the
        # synthesize() caller.
        self._send_lock = asyncio.Lock()
        # Makes initial connection establishment single-flight. ``self._ws``
        # is published before ``connect()`` so the reconnect hook can refer to
        # the manager, but cold callers must not treat that wrapper as usable
        # until the first caller has finished connecting it.
        self._connect_lock = asyncio.Lock()
        # RuntimeScope owns the shared physical-close transaction as a
        # retryable finalizer. Track its current task only for reentrant close
        # detection and local-scope release; the scope owns its result ledger.
        self._close_owner_task: asyncio.Task[Any] | None = None
        self._close_waiters = BackgroundTaskScope(name="tts-close-finalizer-waiters")
        self._close_waiter_sequence = 0
        self._runtime_scope.add_finalizer(_CLOSE_FINALIZER, self._aclose_transaction)
        self._closed = False
        # Set during deliberate teardown (aclose / cancel-fallback socket close)
        # so the reader's exit does NOT surface a spurious error on contexts —
        # only an unexpected socket death does.
        self._closing = False
        self._fallback_close_waiters = 0

    @property
    def runtime_cleanup_complete(self) -> bool:
        """Whether no socket or reader cleanup remains for a scope owner."""
        return (
            self._pending_socket_close is None
            and self._ws is None
            and self._reader_task is None
            and self._close_owner_task is None
        )

    def rehome_runtime_scope(self, source: RuntimeScope, target: RuntimeScope) -> None:
        """Move standalone reader ownership beneath an application lifecycle."""
        if target is self._runtime_scope:
            return
        reader = self._reader_task
        source_tasks = source.tasks()
        if self._runtime_scope is not source or any(task is not reader for task in source_tasks):
            raise RuntimeError("Cannot reattach active TTS manager runtime work")
        moved_reader: asyncio.Task[None] | None = None
        if reader is not None and reader in source_tasks:
            source.discard(reader)
            try:
                target.add_task(
                    _READER_TASK,
                    reader,
                    policy=_TTS_RECEIVE_FINISH_POLICY,
                )
                moved_reader = reader
            except BaseException:
                source.add_task(
                    _READER_TASK,
                    reader,
                    policy=_TTS_RECEIVE_FINISH_POLICY,
                )
                raise
        try:
            source._move_finalizer_to(_CLOSE_FINALIZER, target)
        except BaseException:
            if moved_reader is not None:
                target.discard(moved_reader)
                source.add_task(
                    _READER_TASK,
                    moved_reader,
                    policy=_TTS_RECEIVE_FINISH_POLICY,
                )
            raise
        self._runtime_scope = target
        self._owns_runtime_scope = False

    # ── public surface ────────────────────────────────────────────

    async def connect(self) -> None:
        """Establish the shared socket without opening an utterance context.

        This is safe to call concurrently and lets provider warmup move DNS,
        TLS, and WebSocket-upgrade latency out of the first spoken reply.
        """
        if self._closed:
            raise RuntimeError("MultiContextWSManager is closed")
        await self._ensure_socket()

    async def warmup(self) -> None:
        """Open the shared socket without creating a synthesis context.

        Session startup calls this before user traffic, moving DNS, TLS, and
        WebSocket-upgrade latency out of the first spoken reply. The next
        :meth:`open_context` reuses the connected socket.
        """
        await self.connect()

    async def open_context(self) -> _Context:
        """Register a fresh context, lazily connecting the socket on first use."""
        if self._closed:
            raise RuntimeError("MultiContextWSManager is closed")
        await self.connect()
        # close() can begin after connect() returns but before this coroutine is
        # scheduled again. Do not publish a context into a manager whose close
        # transaction has already closed admission.
        if self._closed:
            raise RuntimeError("MultiContextWSManager is closed")
        ctx = _Context(
            context_id=str(uuid4()),
            queue=asyncio.Queue(maxsize=self._adapter.context_queue_maxsize),
            on_replay=self._adapter.on_context_replay,
        )
        self._contexts[ctx.context_id] = ctx
        return ctx

    async def send(self, ctx: _Context, frames: list[str]) -> None:
        """Send the caller's frames and arm replay only on success."""
        async with self._send_lock:
            await self._send_frames_unlocked(frames, ctx=ctx)
            # Keep replay arming in the same ownership section as the writes.
            # close cannot finish/remove this context between the last frame
            # and this assignment.
            ctx.pending_frames = list(frames)

    async def cancel_context(self, ctx: _Context) -> None:
        """Cancel one context best-effort, keeping the socket open.

        A barge-in must stay near-instant, so the cancel send is never allowed
        to block on a reconnect window: if the socket is not currently connected
        we skip the send and close it outright, and even when connected the send
        runs under a short timeout. On timeout or any send failure we fall back
        to closing the whole socket so a wedged connection does not strand the
        next utterance (the next ``open_context`` lazily reconnects).
        """
        ctx.cancelled = True
        ctx.pending_frames = None
        try:
            cancel_frames = self._adapter.context_cancel_frames(ctx.context_id)
            if cancel_frames and self._ws is not None:
                if not getattr(self._ws, "is_connected", True):
                    # Mid-reconnect: do not block waiting for the socket to come
                    # back. Drop the socket and let the next turn reconnect.
                    await self._close_socket_only()
                else:
                    try:
                        await asyncio.wait_for(
                            self._send_frames(cancel_frames),
                            timeout=_CANCEL_SEND_TIMEOUT,
                        )
                    except Exception:
                        # Covers TimeoutError from wait_for and any send failure.
                        logger.debug(
                            "Multi-context cancel send failed/timed out; closing socket",
                            exc_info=True,
                        )
                        await self._close_socket_only()
        finally:
            # Even a failed physical socket close must not strand the cancelled
            # consumer. Socket ownership is retained separately for retry.
            self._finish_context(ctx)

    async def cancel_all(self) -> None:
        """Cancel every live context (barge-in/stop), keeping the socket open.

        Providers call this instead of tracking the in-flight context in a
        shared field that the synthesize task's ``finally`` can null underneath
        a concurrent ``cancel()`` — so a barge-in always reaches whatever
        context is actually live.
        """
        for ctx in list(self._contexts.values()):
            if not ctx.cancelled:
                await self.cancel_context(ctx)

    def finish_context(self, ctx: _Context) -> None:
        """Terminate one context's iterator and unregister it (idempotent).

        Public teardown verb for providers winding down a finished utterance;
        delegates to the internal :meth:`_finish_context`.
        """
        self._finish_context(ctx)

    async def aclose(self) -> None:
        """Close the socket and tear down the reader task.

        Successful close is idempotent. A failed physical socket close remains
        retryable through a later call, even though new contexts are blocked as
        soon as the first close begins.
        """
        if self._close_owner_task is asyncio.current_task():
            # A socket callback may re-enter provider teardown from inside the
            # owned close transaction. It is already performing this cleanup;
            # awaiting itself would deadlock.
            return
        if self._closed and self.runtime_cleanup_complete:
            await self._close_owned_runtime_scope_if_idle()
            return
        # Close admission synchronously before invoking the finalizer: a
        # connect() scheduled on the next loop turn must observe closure even
        # if this caller is cancelled while joining the shared transaction.
        self._closed = True
        self._closing = True
        await self._await_close_finalizer()
        await self._close_owned_runtime_scope_if_idle()

    # ── reconnect hook ────────────────────────────────────────────

    async def _on_reconnect(self) -> None:
        """Replay every live, armed, non-cancelled context after a reconnect."""
        for ctx in list(self._contexts.values()):
            if ctx.cancelled or ctx.pending_frames is None:
                continue
            # Queue the decoder reset behind every pre-drop frame and before
            # replay responses can be read. Running the callback here would
            # mutate consumer-owned decoder state while buffered old-connection
            # frames are still waiting under backpressure.
            if not await self._put_context_item(ctx, _REPLAY_BOUNDARY):
                continue
            pending_frames = ctx.pending_frames
            if (
                ctx.cancelled
                or pending_frames is None
                or self._contexts.get(ctx.context_id) is not ctx
            ):
                continue
            try:
                # ReconnectingWebSocket invokes this primer while the send
                # that observed the drop can still own ``_send_lock``. Its
                # connection fence already excludes ordinary writers, so the
                # primer must bypass the outer lock to avoid self-deadlock.
                await self._send_frames_unlocked(pending_frames, ctx=ctx)
            except Exception:
                # A context can be cancelled while its replay waits for the
                # shared send lock. That race no longer needs recovery, but a
                # real socket/request replay failure must abort transactional
                # connection installation instead of leaving the context live
                # forever on an unprimed provider socket.
                if (
                    ctx.cancelled
                    or ctx.done.is_set()
                    or self._contexts.get(ctx.context_id) is not ctx
                ):
                    continue
                raise

    # ── internals ─────────────────────────────────────────────────

    async def _ensure_socket(self) -> None:
        async with self._connect_lock:
            # A connect caller can pass the public pre-check and then queue
            # behind close. Recheck after acquiring the shared transition lock.
            if self._closed:
                raise RuntimeError("MultiContextWSManager is closed")
            await self._retry_pending_socket_close()
            if self._ws is not None:
                return
            ws = self._adapter.connect_factory(self._on_reconnect)
            self._ws = ws
            try:
                await ws.connect()
            except BaseException as connect_error:
                # The initial connect failed (retries exhausted). Leave no
                # failed wrapper behind, or the next open_context() would
                # early-return and send() would run against a socket that never
                # connected. Retain it separately if rollback close fails.
                self._ws = None
                try:
                    await self._close_owned_socket(ws)
                except BaseException as cleanup_error:
                    # The connect failure remains primary; the cleanup error is
                    # chained and retained for the next connect()/aclose().
                    raise connect_error from cleanup_error
                raise
            if self._closed:
                # aclose() began while ws.connect() was suspended. Leave the
                # exact published wrapper for the close transaction waiting on
                # this lock; never publish a reader or report connect success.
                raise RuntimeError("MultiContextWSManager closed during connect")
            self._reader_task = self._runtime_scope.create_task(
                _READER_TASK,
                self._reader_loop(),
                task_name="tts_multi_context_reader",
                policy=_TTS_RECEIVE_FINISH_POLICY,
            )

    async def _aclose_transaction(self) -> None:
        """Run one physical close transaction after any connect owner settles."""
        self._closed = True
        self._closing = True
        self._close_owner_task = asyncio.current_task()
        try:
            async with self._connect_lock:  # noqa: SIM117 intentional transition lock order
                # Lock order is always connect -> send. This joins an admitted
                # frame write before contexts or the exact socket are released.
                async with self._send_lock:
                    # Snapshot the handle before cancelling the reader, whose
                    # ``finally`` nulls ``self._ws``.
                    ws = self._pending_socket_close
                    if ws is None:
                        ws = self._ws
                    close_frames: list[str] = []
                    if self._pending_socket_close is None and self._ws is not None:
                        with contextlib.suppress(Exception):
                            close_frames = self._adapter.socket_close_frames()
                    if close_frames:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                self._send_frames_unlocked(close_frames, allow_closing=True),
                                timeout=_SOCKET_CLOSE_SEND_TIMEOUT,
                            )
                    await self._cancel_background_tasks()
                    self._ws = None
                    try:
                        if ws is not None:
                            await self._close_owned_socket(ws)
                    finally:
                        # Drain contexts even when physical close failed. They no
                        # longer own the retained socket cleanup.
                        for ctx in list(self._contexts.values()):
                            self._finish_context(ctx)
                        self._contexts.clear()
        finally:
            self._close_owner_task = None

    async def _await_close_finalizer(self) -> None:
        """Join the close finalizer without misclassifying child cancellation."""
        self._close_waiter_sequence += 1
        attempt = self._close_waiters.create_task(
            f"tts_close_finalizer_waiter_{self._close_waiter_sequence}",
            self._runtime_scope.run_finalizer(_CLOSE_FINALIZER),
            log_errors=False,
        )
        waiter = asyncio.current_task()
        # Preserve a cancellation already pending at helper entry. A
        # previously caught request keeps cancelling() non-zero but does not
        # raise at this checkpoint.
        if waiter is not None and waiter.cancelling():
            await asyncio.sleep(0)
        cancellation_requests = waiter.cancelling() if waiter is not None else 0
        try:
            await asyncio.shield(attempt)
        except asyncio.CancelledError as exc:
            if waiter is not None and waiter.cancelling() > cancellation_requests:
                # This caller acquired a real cancellation request. Shielding
                # inside RuntimeScope leaves the shared finalizer running for
                # every other waiter (or a later retry).
                raise
            # A close implementation may raise CancelledError despite its task
            # receiving no cancellation request. The owned child then has a
            # cancelled result, but the caller itself is not cancelled. Surface
            # that as an ordinary retryable cleanup failure while preserving
            # the exact socket and original error in the ownership ledger.
            close_error = self._socket_close_error
            if close_error is None:
                close_error = exc
            raise RuntimeError(
                "Multi-context WebSocket cleanup was cancelled internally; "
                "retry close() to finish cleanup"
            ) from close_error

    async def _close_owned_socket(self, ws: Any) -> None:
        """Close one exact wrapper, retaining ownership on every failure."""
        try:
            await ws.close()
        except BaseException as exc:
            self._pending_socket_close = ws
            self._socket_close_error = exc
            raise
        else:
            if self._pending_socket_close is ws:
                self._pending_socket_close = None
            self._socket_close_error = None

    async def _retry_pending_socket_close(self) -> None:
        ws = self._pending_socket_close
        if ws is None:
            return
        try:
            await self._close_owned_socket(ws)
        except BaseException as exc:
            raise RuntimeError(
                "Previous multi-context WebSocket cleanup is incomplete; "
                "retry close() or connect() after cleanup recovers"
            ) from exc

    async def _close_owned_runtime_scope_if_idle(self) -> None:
        """Close the standalone scope after every retained resource settles."""
        scope = self._runtime_scope
        if (
            not self._owns_runtime_scope
            or scope.state is not RuntimeScopeState.OPEN
            or not scope.empty
            or not self.runtime_cleanup_complete
        ):
            return
        await scope.close()

    def _require_send_admission(self, ctx: _Context | None = None) -> None:
        if self._closed or self._closing:
            raise RuntimeError("MultiContextWSManager is closing")
        if ctx is not None and (
            ctx.cancelled or ctx.done.is_set() or self._contexts.get(ctx.context_id) is not ctx
        ):
            raise RuntimeError("MultiContextWSManager context is not active")

    async def _send_frames(
        self,
        frames: list[str],
        *,
        ctx: _Context | None = None,
    ) -> None:
        async with self._send_lock:
            await self._send_frames_unlocked(frames, ctx=ctx)

    async def _send_frames_unlocked(
        self,
        frames: list[str],
        *,
        ctx: _Context | None = None,
        allow_closing: bool = False,
    ) -> None:
        if not allow_closing:
            self._require_send_admission(ctx)
        ws = self._ws
        if ws is None:
            raise RuntimeError("MultiContextWSManager socket is not connected")
        for frame in frames:
            await ws.send(frame)
            if not allow_closing:
                # Permanent close flips admission synchronously while an
                # already-admitted write may be suspended in provider I/O.
                self._require_send_admission(ctx)

    async def _reader_loop(self) -> None:
        """Single demux reader: route every frame to its owning context."""
        ws = self._ws
        if ws is None:
            return
        err: BaseException | None = None
        try:
            async for frame in ws.recv_iter():
                await self._dispatch_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Connection death / reconnect-budget exhaustion / on_reconnect
            # raising: record it so live contexts surface a real failure.
            err = exc
            logger.debug("Multi-context reader loop ended on error", exc_info=True)
        finally:
            self._finalize_reader(err)

    async def _dispatch_frame(self, frame: Any) -> None:
        """Parse one incoming frame ONCE and route it to its owning context."""
        # Parsing/routing must never crash the reader (which would tear down the
        # shared socket and silently truncate every live context). A
        # valid-but-non-object frame, etc., is treated as global/ignored.
        try:
            parsed = self._adapter.parse_frame(frame)
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            logger.debug("Multi-context parse_frame raised; ignoring frame")
            return
        if parsed is None:
            # Non-text / unparseable / ignorable frame.
            return
        try:
            key = self._adapter.route_key(parsed)
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            logger.debug("Multi-context route_key raised; treating as global")
            key = None
        ctx = self._contexts.get(key) if key is not None else None
        if ctx is None:
            # Unroutable / unknown / stray context: treat as global so errors
            # surface rather than vanishing. on_global_frame must never crash the
            # reader (that would tear down the shared socket and, post-F1,
            # abort the live turn with a spurious error).
            try:
                self._adapter.on_global_frame(parsed)
            except Exception:
                logger.debug("Multi-context on_global_frame raised; ignoring", exc_info=True)
            return
        if ctx.cancelled:
            # Drop frames for a superseded/cancelled context.
            return
        # Backpressure rather than drop: a blocking put means a slow consumer
        # stalls THIS reader, which stops draining recv_iter and lets TCP
        # backpressure flow to the server — mirroring the one-shot path. Never
        # silently drop a frame (e.g. the terminal done/isFinal).
        # finish_context() marks the context done before draining. Race the
        # bounded put with that marker so a late frame cannot re-fill the queue
        # after teardown and leave this shared reader stuck forever.
        # (EasyCat drives one active context at a time, so head-of-line blocking
        # across contexts is not a concern here.) The PARSED object is queued so
        # the consumer never re-parses the frame.
        await self._put_context_item(ctx, parsed)

    async def _put_context_item(self, ctx: _Context, item: Any) -> bool:
        """Queue *item* unless context teardown wins a full-queue race.

        A normal full queue deliberately applies backpressure.  Once a context
        is finished, however, its queue is drained and a terminal sentinel is
        installed; a previously blocked ``queue.put()`` must be cancelled
        instead of waiting behind that sentinel forever.
        """
        if ctx.done.is_set():
            return False
        try:
            ctx.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            pass

        async with asyncio.TaskGroup() as race:
            put_task = race.create_task(
                ctx.queue.put(item),
                name="tts_context_queue_put",
            )
            done_task = race.create_task(
                ctx.done.wait(),
                name="tts_context_done_wait",
            )
            done, _ = await asyncio.wait(
                (put_task, done_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if put_task in done:
                await put_task
                queued = True
            else:
                queued = False
            for task in (put_task, done_task):
                if not task.done():
                    task.cancel()
        return queued

    def _finalize_reader(self, err: BaseException | None) -> None:
        """Tear down after the reader loop exits (clean or error).

        Drops the socket so the next open_context reconnects a fresh one, and
        terminates every live context. Unless this was a deliberate teardown
        (aclose / cancel-fallback), surfaces a terminal error on still-live
        contexts so a mid-utterance socket death is not downgraded to a clean
        truncated completion — matching the one-shot path, where recv_iter
        raising aborts synthesize().
        """
        self._ws = None
        if not self._closing:
            terminal_error = err or ConnectionError("TTS WebSocket closed mid-stream")
            for ctx in list(self._contexts.values()):
                if not ctx.cancelled:
                    ctx.error = terminal_error
        # Do NOT drain here: the reader has stopped (no producer to unblock), so
        # preserve whatever is buffered. If a terminal frame (done/isFinal) was
        # already queued, the consumer drains it and completes successfully — the
        # error attached above only surfaces for a genuine truncation (frames()
        # exits on done+empty). Draining would discard the buffered terminal and
        # tail audio, turning a finished turn into a spurious failure.
        for ctx in list(self._contexts.values()):
            self._finish_context(ctx, drain=False)

    def _finish_context(self, ctx: _Context, *, drain: bool = True) -> None:
        """Terminate one context's iterator and unregister it (idempotent).

        ``drain=True`` (cancel / aclose) discards buffered frames so a reader
        blocked in ``queue.put()`` is released and the terminal sentinel has
        room to land. ``drain=False`` (reader-end finalize) preserves buffered
        frames so the consumer can still drain a buffered terminal + tail audio;
        ``frames()`` exits on done+empty even if the sentinel can't be enqueued.
        """
        if not ctx.done.is_set():
            ctx.done.set()
            if drain:
                while True:
                    try:
                        ctx.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
            with contextlib.suppress(asyncio.QueueFull):
                ctx.queue.put_nowait(_TERMINAL)
        self._contexts.pop(ctx.context_id, None)

    async def _close_socket_only(self) -> None:
        """Close the live socket and cancel background tasks, but stay reusable.

        Used by the cancel fallback: the next ``open_context`` lazily
        reconnects a fresh socket.
        """
        self._fallback_close_waiters += 1
        try:
            async with self._connect_lock:
                # Permanent close owns every remaining resource once admission
                # is closed. Do not retry or double-close its exact socket.
                if self._closed:
                    return
                # Mark deliberate teardown only after this caller owns the
                # transition. Setting it while waiting for a slow connect
                # would mask a genuine reader failure as clean completion.
                self._closing = True
                await self._retry_pending_socket_close()
                ws = self._ws
                # This fallback abandons the shared connection because one
                # context's cancel frame could not be sent. The cancelled
                # context ends quietly, but every sibling was interrupted and
                # must see a terminal error rather than wait forever.
                terminal_error = ConnectionError(
                    "TTS WebSocket closed after context cancellation failed"
                )
                for ctx in list(self._contexts.values()):
                    if not ctx.cancelled:
                        ctx.error = terminal_error
                # The reader's deliberate cancellation must not surface a
                # spurious connection-death error on live contexts.
                await self._cancel_background_tasks()
                for ctx in list(self._contexts.values()):
                    self._finish_context(ctx)
                self._ws = None
                if ws is not None:
                    # The cancel-frame timeout may have been caused by a send
                    # wedged while owning _send_lock. Close first so the socket
                    # can unblock it, then join the admitted send before
                    # clearing the closing admission flag.
                    await self._close_owned_socket(ws)
                async with self._send_lock:
                    pass
        finally:
            self._fallback_close_waiters -= 1
            if self._fallback_close_waiters == 0 and not self._closed:
                self._closing = False

    async def _cancel_background_tasks(self) -> None:
        task = self._reader_task
        if task is not None:
            if task in self._runtime_scope.tasks(_READER_TASK):
                await self._runtime_scope.cancel_and_drain(_READER_TASK)
            elif not task.done():
                # Compatibility for an externally supplied/test reader task;
                # production readers are always registered above.
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reader_task = None
