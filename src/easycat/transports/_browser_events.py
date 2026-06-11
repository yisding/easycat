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

import logging
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


class BrowserEventForwarder:
    """Forward session events to a connected browser as JSON messages.

    Subscribes to the session :class:`EventBus` on construction and pushes
    each relevant event through ``send_json`` — an async best-effort sender
    supplied by the owning transport (WebSocket text frame or WebRTC data
    channel). Delivery is observability, never load-bearing: send failures
    are logged at debug level and dropped.

    Call :meth:`close` (idempotent) to unsubscribe during transport teardown.
    """

    def __init__(
        self,
        bus: EventBus,
        send_json: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._send_json = send_json
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

    def close(self) -> None:
        """Unsubscribe from the event bus. Safe to call more than once."""
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
        self._stt_final_at.clear()

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
        try:
            await self._send_json(payload)
        except Exception:
            logger.debug("Dropping browser event %s: send failed", payload.get("type"))
