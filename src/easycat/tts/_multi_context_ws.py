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

logger = logging.getLogger(__name__)

# Sentinels pushed onto a context queue to preserve consumer-visible ordering.
_TERMINAL = object()
_REPLAY_BOUNDARY = object()

# Short timeout for the cancel-frame send. A barge-in must stay near-instant, so
# we never let the cancel send block on a reconnect window; on timeout we fall
# back to closing the socket outright.
_CANCEL_SEND_TIMEOUT = 0.5


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

    def __init__(self, adapter: MultiContextAdapter) -> None:
        self._adapter = adapter
        self._ws: Any | None = None
        self._contexts: dict[str, _Context] = {}
        self._reader_task: asyncio.Task[None] | None = None
        # Serializes every ws.send across the reconnect-replay hook and the
        # synthesize() caller.
        self._send_lock = asyncio.Lock()
        self._closed = False
        # Set during deliberate teardown (aclose / cancel-fallback socket close)
        # so the reader's exit does NOT surface a spurious error on contexts —
        # only an unexpected socket death does.
        self._closing = False

    # ── public surface ────────────────────────────────────────────

    async def connect(self) -> None:
        """Establish the shared socket without opening an utterance context."""
        if self._closed:
            raise RuntimeError("MultiContextWSManager is closed")
        await self._ensure_socket()

    async def open_context(self) -> _Context:
        """Register a fresh context, lazily connecting the socket on first use."""
        if self._closed:
            raise RuntimeError("MultiContextWSManager is closed")
        await self.connect()
        ctx = _Context(
            context_id=str(uuid4()),
            queue=asyncio.Queue(maxsize=self._adapter.context_queue_maxsize),
            on_replay=self._adapter.on_context_replay,
        )
        self._contexts[ctx.context_id] = ctx
        return ctx

    async def send(self, ctx: _Context, frames: list[str]) -> None:
        """Send the caller's frames and arm replay only on success."""
        await self._send_frames(frames)
        # Arm replay only after a successful send so a reconnect during the
        # initial send window does not replay-before-send.
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
        """Close the socket and tear down the reader task (idempotent)."""
        if self._closed:
            return
        self._closed = True
        self._closing = True
        close_frames = self._adapter.socket_close_frames()
        if close_frames and self._ws is not None:
            with contextlib.suppress(Exception):
                await self._send_frames(close_frames)
        # Snapshot the handle before cancelling the reader, whose ``finally``
        # nulls ``self._ws``.
        ws = self._ws
        await self._cancel_background_tasks()
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        # Drain any contexts still waiting.
        for ctx in list(self._contexts.values()):
            self._finish_context(ctx)
        self._contexts.clear()

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
            await ctx.queue.put(_REPLAY_BOUNDARY)
            pending_frames = ctx.pending_frames
            if (
                ctx.cancelled
                or pending_frames is None
                or self._contexts.get(ctx.context_id) is not ctx
            ):
                continue
            with contextlib.suppress(Exception):
                await self._send_frames(pending_frames)

    # ── internals ─────────────────────────────────────────────────

    async def _ensure_socket(self) -> None:
        if self._ws is not None:
            return
        ws = self._adapter.connect_factory(self._on_reconnect)
        self._ws = ws
        try:
            await ws.connect()
        except BaseException:
            # The initial connect failed (retries exhausted). Leave no failed
            # wrapper behind, or the next open_context() would early-return and
            # send() would run against a socket that never connected; clear it
            # so the next open reconnects a fresh one.
            self._ws = None
            with contextlib.suppress(Exception):
                await ws.close()
            raise
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _send_frames(self, frames: list[str]) -> None:
        async with self._send_lock:
            ws = self._ws
            if ws is None:
                raise RuntimeError("MultiContextWSManager socket is not connected")
            for frame in frames:
                await ws.send(frame)

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
        except Exception:
            logger.debug("Multi-context parse_frame raised; ignoring frame")
            return
        if parsed is None:
            # Non-text / unparseable / ignorable frame.
            return
        try:
            key = self._adapter.route_key(parsed)
        except Exception:
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
        # finish_context() drains the queue to release a reader blocked here on
        # cancel/teardown, and cancelling the reader task unblocks it too.
        # (EasyCat drives one active context at a time, so head-of-line blocking
        # across contexts is not a concern here.) The PARSED object is queued so
        # the consumer never re-parses the frame.
        await ctx.queue.put(parsed)

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
        ws = self._ws
        # Mark this as a deliberate teardown so the reader's finally does not
        # surface a spurious connection-death error on still-live contexts;
        # reset afterwards since the manager stays reusable (the awaited task
        # cancel guarantees the reader's finally has already run).
        self._closing = True
        try:
            await self._cancel_background_tasks()
        finally:
            self._closing = False
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _cancel_background_tasks(self) -> None:
        task = self._reader_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._reader_task = None
