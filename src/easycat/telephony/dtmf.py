"""DTMF parsing and aggregation for EasyCat telephony."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, TypeGuard

from easycat.events import DTMF, DTMFAggregated, EventBus
from easycat.runtime.scope import BackgroundTaskScope

logger = logging.getLogger(__name__)

# Valid DTMF digits per ITU-T Q.23
VALID_DTMF_DIGITS = frozenset("0123456789*#ABCD")

# Twilio's TwiML ``digits`` attribute also accepts pause markers. Keep this
# transport-output policy beside the core DTMF alphabet so every serializer and
# agent boundary uses the same all-or-nothing validation rule.
VALID_DTMF_OUTPUT_CHARS = VALID_DTMF_DIGITS | frozenset("wW")
_DTMF_TIMER_MEMBER = "dtmf_aggregate_timeout"


def is_valid_dtmf_output(value: object) -> TypeGuard[str]:
    """Return whether *value* is a non-empty, all-or-nothing DTMF output."""
    return (
        isinstance(value, str)
        and bool(value)
        and all(char in VALID_DTMF_OUTPUT_CHARS for char in value)
    )


def _validate_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


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

    def __post_init__(self) -> None:
        _validate_positive_int("timeout_ms", self.timeout_ms)
        _validate_positive_int("max_length", self.max_length)


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
        self._tasks = BackgroundTaskScope(name="dtmf-aggregator")
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
        self._timer_task = self._tasks.create_task(
            _DTMF_TIMER_MEMBER,
            self._timeout(),
            replace=True,
        )

    async def _timeout(self) -> None:
        """Wait for the idle timeout, then emit."""
        try:
            await asyncio.sleep(self._config.timeout_ms / 1000.0)
            if self._buffer:
                await self._emit()
        finally:
            # Keep the task reachable through its aggregate emission. A
            # synchronous stop() can then still cancel a blocked subscriber
            # instead of allowing a stale DTMFAggregated event after teardown.
            if self._timer_task is asyncio.current_task():
                self._timer_task = None

    async def _emit(self) -> None:
        """Emit the aggregated sequence and reset."""
        self._cancel_timer()
        if not self._buffer:
            return
        sequence = "".join(self._buffer)
        self._buffer.clear()
        await self._event_bus.emit(DTMFAggregated(sequence=sequence))

    def _cancel_timer(self) -> None:
        task = self._timer_task
        if task is None:
            return
        self._tasks.cancel(_DTMF_TIMER_MEMBER)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not current and not task.done():
            task.cancel()
        if task is not current:
            self._timer_task = None
