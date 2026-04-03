"""Structured event-by-event logging helpers for EasyCat sessions."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from easycat.events import (
    AGENT_EVENTS,
    DTMF,
    ERROR_EVENTS,
    LIFECYCLE_EVENTS,
    RECONNECT_EVENTS,
    TELEPHONY_EVENTS,
    TOOL_EVENTS,
    VAD_EVENTS,
    AgentDelta,
    AgentFinal,
    DTMFAggregated,
    Error,
    Event,
    EventBus,
    Interruption,
    ReconnectAttempt,
    ReconnectFailure,
    ReconnectSuccess,
    STTFinal,
    STTPartial,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TTSAudio,
    TTSMarkers,
    VoicemailDetected,
)


@dataclass
class EventLoggingConfig:
    """Configuration for event-by-event logging from the EventBus."""

    enabled: bool = True
    logger_name: str = "easycat.event_trace"
    level: int = logging.INFO
    include_audio_events: bool = False
    include_partials: bool = False
    include_text: bool = True
    text_limit: int = 160
    include_event_index: bool = True
    json_mode: bool = False
    sample_rates: dict[str, float] = field(default_factory=dict)
    min_interval_s: dict[str, float] = field(default_factory=dict)
    ring_buffer_size: int = 200


@dataclass(frozen=True)
class _TraceState:
    start_time: float = field(default_factory=time.monotonic)
    event_index: int = 0


class EventTraceLogger:
    """Subscribe to EventBus events and emit concise log lines for each."""

    def __init__(self, event_bus: EventBus, config: EventLoggingConfig | None = None) -> None:
        self._event_bus = event_bus
        self._config = config or EventLoggingConfig()
        self._logger = logging.getLogger(self._config.logger_name)
        self._active = False
        self._handler: logging.Handler | None = None
        self._state = _TraceState()
        self._last_emit_time: dict[str, float] = {}
        self._seen_counts: dict[str, int] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=max(1, self._config.ring_buffer_size))

    def start(self) -> None:
        """Attach logger as a global EventBus subscriber."""
        if self._active:
            return
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setLevel(self._config.level)
            self._logger.addHandler(handler)
            self._handler = handler
        self._logger.setLevel(self._config.level)
        self._logger.propagate = False
        self._state = _TraceState()
        self._last_emit_time.clear()
        self._seen_counts.clear()
        self._recent.clear()
        self._event_bus.subscribe_all(self._log_event)
        self._active = True

    def stop(self) -> None:
        """Detach logger from EventBus."""
        if not self._active:
            return
        self._event_bus.unsubscribe_all(self._log_event)
        if self._handler is not None:
            self._logger.removeHandler(self._handler)
            self._handler = None
        self._active = False

    async def _log_event(self, event: Event) -> None:
        if not self._should_log(event):
            return
        now = time.monotonic()
        event_type = type(event).__name__
        if not self._passes_rate_limits(event_type, now):
            return
        self._state = _TraceState(self._state.start_time, self._state.event_index + 1)
        payload = self._to_payload(event, self._state.event_index)
        self._recent.append(payload)
        if self._config.json_mode:
            self._logger.log(
                self._config.level, json.dumps(payload, ensure_ascii=False, sort_keys=True)
            )
            return
        self._logger.log(self._config.level, self._format_text(payload))

    def snapshot_recent_events(self) -> list[dict[str, Any]]:
        """Return a copy of the in-memory ring buffer of recently logged events."""
        return list(self._recent)

    def _passes_rate_limits(self, event_type: str, now: float) -> bool:
        sample_rate = self._config.sample_rates.get(event_type, 1.0)
        if sample_rate <= 0:
            return False
        if sample_rate < 1.0:
            sequence = self._seen_counts.get(event_type, 0) + 1
            self._seen_counts[event_type] = sequence
            bucket = int(round(1 / sample_rate))
            if bucket > 1 and sequence % bucket != 0:
                return False

        min_interval = self._config.min_interval_s.get(event_type)
        if min_interval is None:
            return True
        previous = self._last_emit_time.get(event_type)
        if previous is not None and (now - previous) < min_interval:
            return False
        self._last_emit_time[event_type] = now
        return True

    def _should_log(self, event: Event) -> bool:
        if isinstance(event, TTSAudio):
            return self._config.include_audio_events
        if isinstance(event, STTPartial):
            return self._config.include_partials
        return isinstance(event, _DEFAULT_LOGGED_EVENTS)

    def _to_payload(self, event: Event, idx: int) -> dict[str, Any]:
        event_time = getattr(event, "timestamp", time.monotonic())
        payload: dict[str, Any] = {
            "event_type": type(event).__name__,
            "elapsed_s": round(event_time - self._state.start_time, 6),
            "event_index": idx,
            "session_id": getattr(event, "session_id", None),
            "turn_id": getattr(event, "turn_id", None),
        }
        if isinstance(event, (STTPartial, STTFinal, AgentDelta, AgentFinal)):
            if self._config.include_text:
                payload["text"] = self._truncate(event.text)
            else:
                payload["text_chars"] = len(event.text)
        elif isinstance(event, TTSAudio):
            payload["audio_bytes"] = len(event.chunk.data)
            payload["sample_rate"] = event.chunk.format.sample_rate
        elif isinstance(event, TTSMarkers):
            payload["marker_count"] = len(event.markers)
        elif isinstance(event, ToolCallStarted):
            payload["tool_name"] = event.tool_name
            payload["call_id"] = event.call_id
        elif isinstance(event, ToolCallDelta):
            payload["call_id"] = event.call_id
            if self._config.include_text:
                payload["delta"] = self._truncate(event.delta)
            else:
                payload["delta_chars"] = len(event.delta)
        elif isinstance(event, ToolCallResult):
            payload["call_id"] = event.call_id
            if self._config.include_text:
                payload["result"] = self._truncate(event.result)
            else:
                payload["result_chars"] = len(event.result)
        elif isinstance(event, ReconnectAttempt):
            payload["provider"] = event.provider
            payload["attempt"] = event.attempt
        elif isinstance(event, ReconnectSuccess):
            payload["provider"] = event.provider
        elif isinstance(event, ReconnectFailure):
            payload["provider"] = event.provider
            payload["error"] = event.error
        elif isinstance(event, DTMF):
            payload["digit"] = event.digit
        elif isinstance(event, DTMFAggregated):
            payload["sequence"] = event.sequence
        elif isinstance(event, VoicemailDetected):
            payload["result"] = event.result
        elif isinstance(event, Error):
            payload["stage"] = event.stage.value
            if event.provider:
                payload["provider"] = event.provider
            payload["exception"] = repr(event.exception)
        return payload

    def _format_text(self, payload: Mapping[str, Any]) -> str:
        event_type = payload["event_type"]
        if self._config.include_event_index:
            prefix = f"#{payload['event_index']:04d} +{payload['elapsed_s']:.3f}s {event_type}"
        else:
            prefix = str(event_type)
        extras: list[str] = []
        for key in (
            "session_id",
            "turn_id",
            "tool_name",
            "call_id",
            "provider",
            "attempt",
            "digit",
            "sequence",
            "marker_count",
            "audio_bytes",
            "sample_rate",
            "text_chars",
            "stage",
            "exception",
            "error",
            "result",
            "result_chars",
            "text",
            "delta",
            "delta_chars",
        ):
            value = payload.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                extras.append(f'{key}="{value}"')
            else:
                extras.append(f"{key}={value}")
        return f"{prefix} {' '.join(extras)}".strip()

    def _truncate(self, value: str) -> str:
        if len(value) <= self._config.text_limit:
            return value
        return f"{value[: self._config.text_limit]}…"


# Default set: everything except high-frequency audio/partial events
# (TTSAudio, STTPartial, AudioIn) which are gated by config flags.
_DEFAULT_LOGGED_EVENTS = (
    VAD_EVENTS
    + (STTFinal,)
    + AGENT_EVENTS
    + (TTSMarkers,)
    + TOOL_EVENTS
    + LIFECYCLE_EVENTS
    + (Interruption,)
    + RECONNECT_EVENTS
    + TELEPHONY_EVENTS
    + ERROR_EVENTS
)
