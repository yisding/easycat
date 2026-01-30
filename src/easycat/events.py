"""Event types and EventBus dispatch system for EasyCat."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from easycat.audio_format import AudioChunk

logger = logging.getLogger(__name__)

# Type alias for event handlers
EventHandler = Callable[..., None] | Callable[..., Coroutine[Any, Any, None]]


# ── Event dataclasses ──────────────────────────────────────────────


@dataclass(frozen=True)
class AudioIn:
    """Raw audio chunk received from transport."""

    chunk: AudioChunk
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class VADStartSpeaking:
    """VAD detected start of user speech."""

    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class VADStopSpeaking:
    """VAD detected end of user speech."""

    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class STTPartial:
    """Partial transcript from STT provider."""

    text: str
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class STTFinal:
    """Final transcript from STT provider for a completed turn."""

    text: str
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class AgentDelta:
    """Streaming text delta from the agent."""

    text: str
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class AgentFinal:
    """Final complete response from the agent."""

    text: str
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class TTSAudio:
    """Audio chunk produced by TTS provider."""

    chunk: AudioChunk
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class TTSMarkers:
    """Word/viseme alignment markers from TTS."""

    markers: list[dict[str, Any]]
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class DTMF:
    """Single DTMF digit detected."""

    digit: str
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class DTMFAggregated:
    """Aggregated DTMF digit sequence."""

    sequence: str
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class VoicemailDetected:
    """Voicemail / answering machine detection result."""

    result: str  # "human" | "machine" | "unknown"
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class Error:
    """Error event wrapping an exception."""

    exception: BaseException
    context: str = ""
    timestamp: float = field(default_factory=time.monotonic)


# Union of all event types for type-checking convenience
Event = (
    AudioIn
    | VADStartSpeaking
    | VADStopSpeaking
    | STTPartial
    | STTFinal
    | AgentDelta
    | AgentFinal
    | TTSAudio
    | TTSMarkers
    | DTMF
    | DTMFAggregated
    | VoicemailDetected
    | Error
)


# ── EventBus ───────────────────────────────────────────────────────


class EventBus:
    """Publish/subscribe event dispatcher supporting sync and async handlers."""

    def __init__(self) -> None:
        self._handlers: defaultdict[type, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: type, handler: EventHandler) -> None:
        """Register a handler for a specific event type."""
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: type, handler: EventHandler) -> None:
        """Remove a handler for a specific event type."""
        handlers = self._handlers[event_type]
        try:
            handlers.remove(handler)
        except ValueError:
            pass

    async def emit(self, event: Event) -> None:
        """Emit an event to all registered handlers for its type.

        Sync handlers are called directly; async handlers are awaited.
        Exceptions in handlers are logged but do not prevent other handlers from running.
        """
        event_type = type(event)
        for handler in list(self._handlers[event_type]):
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "Error in handler %s for event %s",
                    handler.__name__,
                    event_type.__name__,
                )
