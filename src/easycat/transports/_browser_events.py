"""Server → browser event channel shared by the WebSocket and WebRTC transports.

The bundled browser playground (``transports/static/webrtc_client.html``,
served by :class:`~easycat.transports.webrtc.WebRTCTransport` and launched by
``easycat serve``) renders a live transcript, an interruption indicator, and a
per-turn latency readout. Those widgets are driven by JSON event messages that
this module forwards from the session :class:`~easycat.events.EventBus` to the
browser:

- WebSocket transports send them as **text frames** alongside the existing
  ``ready`` / ``audio_format`` control messages.
- The WebRTC transport sends them over a client-created **data channel**
  named ``"events"``.

Wire format (one JSON object per message, ``schema_version`` 1)::

    {"type": "stt_partial",  "text": str, "turn_id": str | null}
    {"type": "stt_final",    "text": str, "turn_id": str | null}
    {"type": "agent_delta",  "text": str, "turn_id": str | null}
    {"type": "agent_final",  "text": str, "turn_id": str | null}
    {"type": "turn_started", "turn_id": str | null}
    {"type": "interruption", "turn_id": str | null}
    {"type": "turn_latency", "turn_id": str | null, "ms": float}

``turn_latency`` measures the user-perceived response gap: the time from the
final user transcript (``STTFinal``) to the first bot audio
(``BotStartedSpeaking``) of the same turn. The maintained reader-facing
description of this protocol lives in ``docs/browser-playground.md``.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any

from easycat.events import (
    AgentDelta,
    AgentFinal,
    BotStartedSpeaking,
    EventBus,
    EventSubscription,
    Interruption,
    STTFinal,
    STTPartial,
    TurnStarted,
)
from easycat.runtime._event_tasks import RuntimeEventTaskScope
from easycat.runtime.scope import RuntimeScope

logger = logging.getLogger(__name__)

#: Version of the browser event wire format documented above.
BROWSER_EVENT_SCHEMA_VERSION = 1

#: Message types a transport may send to the browser playground.
BROWSER_EVENT_TYPES = (
    "stt_partial",
    "stt_final",
    "agent_delta",
    "agent_final",
    "turn_started",
    "interruption",
    "turn_latency",
)

# Bound the per-turn latency bookkeeping so a session that never produces
# bot audio cannot grow the pending map without limit.
_MAX_PENDING_TURNS = 32

# Browser UI telemetry must never inherit a transport's unbounded write-drain
# wait. A quarter second tolerates ordinary scheduling jitter while keeping a
# backgrounded or congested client off the STT/agent event hot path.
_DEFAULT_SEND_TIMEOUT_S = 0.25
_DEFAULT_MAX_PENDING_EVENTS = 32
_BROWSER_EVENT_COHORT = "transport-events"


class BrowserEventForwarder:
    """Forward session events to a connected browser as JSON messages.

    Subscribes to the session :class:`EventBus` on construction and pushes
    each relevant event through a small bounded writer queue to ``send_json`` —
    an async best-effort sender supplied by the owning transport (WebSocket text
    frame or WebRTC data channel). Delivery is observability, never
    load-bearing: event handlers never await transport I/O, and queue overflow,
    send timeout, or send failure is logged at debug level and dropped.

    Call :meth:`close` (idempotent) to unsubscribe during transport teardown.
    """

    def __init__(
        self,
        bus: EventBus,
        send_json: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        send_timeout_s: float = _DEFAULT_SEND_TIMEOUT_S,
        max_pending_events: int = _DEFAULT_MAX_PENDING_EVENTS,
        runtime_scope: RuntimeScope | None = None,
    ) -> None:
        if not math.isfinite(send_timeout_s) or send_timeout_s <= 0:
            raise ValueError("send_timeout_s must be a finite number > 0")
        if (
            isinstance(max_pending_events, bool)
            or not isinstance(max_pending_events, int)
            or max_pending_events < 1
        ):
            raise ValueError("max_pending_events must be an integer >= 1")
        self._send_json = send_json
        self._send_timeout_s = send_timeout_s
        self._max_send_tasks = max_pending_events
        self._send_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_pending_events)
        self._writer_task: asyncio.Task[None] | None = None
        self._writer_tasks = RuntimeEventTaskScope(
            owner_label="browser-event-writer",
            member_name="browser_event_writer",
            cohort=_BROWSER_EVENT_COHORT,
            logger=logger,
            failure_message="Browser event writer stopped unexpectedly",
        )
        self._send_task_scope = RuntimeEventTaskScope(
            owner_label="browser-event-send",
            member_name="browser_event_send",
            cohort=_BROWSER_EVENT_COHORT,
            logger=logger,
            failure_message="Detached browser event send failed",
        )
        self._detached_send_tasks = RuntimeEventTaskScope(
            owner_label="browser-event-detached-send",
            member_name="browser_event_send",
            cohort=_BROWSER_EVENT_COHORT,
            logger=logger,
            failure_message="Detached browser event send failed",
        )
        if runtime_scope is not None:
            self._writer_tasks.bind(runtime_scope)
            self._send_task_scope.bind(runtime_scope)
        self._closed = False
        # turn_id -> monotonic timestamp of the final user transcript.
        self._stt_final_at: dict[str | None, float] = {}
        self._subscriptions: list[EventSubscription] = [
            bus.subscribe(STTPartial, self._on_stt_partial),
            bus.subscribe(STTFinal, self._on_stt_final),
            bus.subscribe(AgentDelta, self._on_agent_delta),
            bus.subscribe(AgentFinal, self._on_agent_final),
            bus.subscribe(TurnStarted, self._on_turn_started),
            bus.subscribe(Interruption, self._on_interruption),
            bus.subscribe(BotStartedSpeaking, self._on_bot_started_speaking),
        ]

    @property
    def _send_tasks(self) -> set[asyncio.Task[Any]]:
        """Compatibility inspection of scope-owned browser sends."""
        return set(self._send_task_scope.tasks())

    def close(self) -> None:
        """Unsubscribe from the event bus. Safe to call more than once."""
        self._closed = True
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
        self._stt_final_at.clear()
        while True:
            try:
                self._send_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._send_queue.task_done()
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()
        for task in self._send_task_scope.tasks():
            if not task.done():
                task.cancel()
                if not self._send_task_scope.owns_root:
                    self._send_task_scope.discard_task(task)
                    self._detached_send_tasks.adopt_task(task)

    # ── Event handlers ────────────────────────────────────────────

    async def _on_stt_partial(self, event: STTPartial) -> None:
        await self._send({"type": "stt_partial", "text": event.text, "turn_id": event.turn_id})

    async def _on_stt_final(self, event: STTFinal) -> None:
        if len(self._stt_final_at) >= _MAX_PENDING_TURNS:
            self._stt_final_at.pop(next(iter(self._stt_final_at)))
        self._stt_final_at[event.turn_id] = event.timestamp
        await self._send({"type": "stt_final", "text": event.text, "turn_id": event.turn_id})

    async def _on_agent_delta(self, event: AgentDelta) -> None:
        await self._send({"type": "agent_delta", "text": event.text, "turn_id": event.turn_id})

    async def _on_agent_final(self, event: AgentFinal) -> None:
        await self._send({"type": "agent_final", "text": event.text, "turn_id": event.turn_id})

    async def _on_turn_started(self, event: TurnStarted) -> None:
        await self._send({"type": "turn_started", "turn_id": event.turn_id})

    async def _on_interruption(self, event: Interruption) -> None:
        await self._send({"type": "interruption", "turn_id": event.turn_id})

    async def _on_bot_started_speaking(self, event: BotStartedSpeaking) -> None:
        started_at = self._stt_final_at.pop(event.turn_id, None)
        if started_at is None and self._stt_final_at:
            # Correlation ids can differ across pipeline hops (e.g. a greeting
            # or text-injected turn); fall back to the oldest pending turn so
            # the readout stays useful instead of silently missing.
            started_at = self._stt_final_at.pop(next(iter(self._stt_final_at)))
        if started_at is None:
            return
        latency_ms = max(0.0, (event.timestamp - started_at) * 1000.0)
        await self._send(
            {
                "type": "turn_latency",
                "turn_id": event.turn_id,
                "ms": round(latency_ms, 1),
            }
        )

    # ── Plumbing ──────────────────────────────────────────────────

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            self._send_queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.debug(
                "Dropping browser event %s: writer queue full",
                payload.get("type"),
            )
            return
        self._ensure_writer()
        # Give the writer one scheduling opportunity so healthy in-memory /
        # data-channel senders preserve the existing near-immediate behavior.
        # This never awaits transport I/O: the writer owns that independently.
        await asyncio.sleep(0)

    def _ensure_writer(self) -> None:
        if self._writer_task is not None and not self._writer_task.done():
            return
        writer = self._writer_tasks.create_task(
            self._writer_loop(),
            task_name="browser-event-writer",
        )
        if writer is None:
            while True:
                try:
                    self._send_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                else:
                    self._send_queue.task_done()
        self._writer_task = writer
        writer.add_done_callback(self._writer_done)

    async def _writer_loop(self) -> None:
        while True:
            try:
                payload = self._send_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self._send_payload(payload)
            finally:
                self._send_queue.task_done()

    async def _send_payload(self, payload: dict[str, Any]) -> None:
        live_send_tasks = sum(not task.done() for task in self._send_tasks)
        if live_send_tasks >= self._max_send_tasks:
            logger.debug(
                "Dropping browser event %s: too many timed-out sends still running",
                payload.get("type"),
            )
            return
        task = self._send_task_scope.create_task(
            self._call_send_json(payload),
            task_name="browser-event-send",
        )
        if task is None:
            return
        try:
            # Shielding is deliberate: wait_for cancels only the shield at the
            # deadline and therefore returns without waiting for a sender that
            # suppresses cancellation. The detached task is cancelled below
            # and its callback retrieves any eventual exception.
            await asyncio.wait_for(asyncio.shield(task), timeout=self._send_timeout_s)
        except TimeoutError:
            task.cancel()
            logger.debug(
                "Dropping browser event %s: send exceeded %.3fs",
                payload.get("type"),
                self._send_timeout_s,
            )
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            logger.debug("Dropping browser event %s: send failed", payload.get("type"))

    async def _call_send_json(self, payload: dict[str, Any]) -> None:
        await self._send_json(payload)

    def _writer_done(self, task: asyncio.Task[None]) -> None:
        if self._writer_task is task:
            self._writer_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Browser event writer stopped unexpectedly", exc_info=True)
