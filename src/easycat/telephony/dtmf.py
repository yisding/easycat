"""DTMF parsing and aggregation for EasyCat telephony."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from easycat.events import DTMF, DTMFAggregated, EventBus

logger = logging.getLogger(__name__)

# Valid DTMF digits per ITU-T Q.23
VALID_DTMF_DIGITS = frozenset("0123456789*#ABCD")


# ── Twilio Media Streams DTMF parsing ────────────────────────────


def parse_twilio_dtmf_message(message: str | dict[str, Any]) -> DTMF | None:
    """Parse a Twilio Media Streams WebSocket message for DTMF events.

    Twilio sends DTMF events as JSON messages on the Media Streams WebSocket
    with ``"event": "dtmf"`` and a nested ``"dtmf": {"digit": "5"}`` payload.

    Args:
        message: Raw WebSocket message string or pre-parsed dict.

    Returns:
        A ``DTMF`` event if the message contains a valid DTMF digit,
        otherwise ``None``.
    """
    if isinstance(message, str):
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return None
    else:
        data = message

    if not isinstance(data, dict):
        return None

    if data.get("event") != "dtmf":
        return None

    dtmf_payload = data.get("dtmf")
    if not isinstance(dtmf_payload, dict):
        return None

    digit = dtmf_payload.get("digit", "")
    if not isinstance(digit, str) or len(digit) != 1:
        return None

    digit = digit.upper()
    if digit not in VALID_DTMF_DIGITS:
        return None

    return DTMF(digit=digit)


async def emit_twilio_dtmf(
    message: str | dict[str, Any],
    event_bus: EventBus,
) -> DTMF | None:
    """Parse a Twilio WebSocket message and emit a DTMF event if present.

    Convenience wrapper that combines parsing and emission.

    Returns:
        The emitted ``DTMF`` event, or ``None`` if the message was not DTMF.
    """
    event = parse_twilio_dtmf_message(message)
    if event is not None:
        await event_bus.emit(event)
    return event


# ── DTMF Aggregator ──────────────────────────────────────────────


@dataclass
class DTMFAggregatorConfig:
    """Configuration for DTMFAggregator."""

    timeout_ms: int = 2000
    """Idle time in ms before emitting the aggregated sequence."""

    terminators: frozenset[str] = field(default_factory=lambda: frozenset({"#"}))
    """Characters that trigger immediate emission (e.g. ``#``, ``*``)."""

    max_length: int = 20
    """Maximum digit count before auto-emission."""


class DTMFAggregator:
    """Collects individual DTMF digits into sequences.

    Subscribes to ``DTMF`` events on the event bus, accumulates digits, and
    emits ``DTMFAggregated`` when triggered by timeout, terminator, or max
    length.
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: DTMFAggregatorConfig | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._config = config or DTMFAggregatorConfig()
        self._buffer: list[str] = []
        self._timer_task: asyncio.Task[None] | None = None
        self._started = False

    @property
    def buffer(self) -> str:
        """Current accumulated digit buffer (read-only)."""
        return "".join(self._buffer)

    def start(self) -> None:
        """Subscribe to DTMF events on the event bus."""
        if not self._started:
            self._event_bus.subscribe(DTMF, self._on_dtmf)
            self._started = True

    def stop(self) -> None:
        """Unsubscribe and cancel any pending timer."""
        if self._started:
            self._event_bus.unsubscribe(DTMF, self._on_dtmf)
            self._started = False
        self._cancel_timer()
        self._buffer.clear()

    async def _on_dtmf(self, event: DTMF) -> None:
        """Handle an incoming DTMF digit."""
        digit = event.digit

        # Cancel pending timeout
        self._cancel_timer()

        self._buffer.append(digit)

        # Check terminator (digit itself triggers emit, included in sequence)
        if digit in self._config.terminators:
            await self._emit()
            return

        # Check max length
        if len(self._buffer) >= self._config.max_length:
            await self._emit()
            return

        # Start idle timeout
        self._timer_task = asyncio.create_task(self._timeout())

    async def _timeout(self) -> None:
        """Wait for the idle timeout, then emit."""
        try:
            await asyncio.sleep(self._config.timeout_ms / 1000.0)
            # Clear self-reference before emitting so _emit()'s
            # _cancel_timer() doesn't cancel this (the current) task.
            self._timer_task = None
            if self._buffer:
                await self._emit()
        except asyncio.CancelledError:
            # Cancellation is expected when a new DTMF digit arrives or the
            # aggregator is stopped; we deliberately ignore it.
            pass

    async def _emit(self) -> None:
        """Emit the aggregated sequence and reset."""
        self._cancel_timer()
        if not self._buffer:
            return
        sequence = "".join(self._buffer)
        self._buffer.clear()
        await self._event_bus.emit(DTMFAggregated(sequence=sequence))

    def _cancel_timer(self) -> None:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            self._timer_task = None
