"""Event types and EventBus dispatch system for EasyCat."""

from __future__ import annotations

import asyncio
import enum
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


# ── EasyCat-level event dataclasses ────────────────────────────────


# Audio
@dataclass(frozen=True)
class AudioIn:
    """Raw audio chunk received from transport."""

    chunk: AudioChunk
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


# VAD
@dataclass(frozen=True)
class VADStartSpeaking:
    """VAD detected start of user speech."""

    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class VADStopSpeaking:
    """VAD detected end of user speech."""

    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


# STT
@dataclass(frozen=True)
class STTPartial:
    """Partial transcript from STT provider."""

    text: str
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class STTFinal:
    """Final transcript from STT provider for a completed turn."""

    text: str
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


# Agent
@dataclass(frozen=True)
class AgentDelta:
    """Streaming text delta from the agent."""

    text: str
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class AgentFinal:
    """Final complete response from the agent.

    When the agent uses a structured ``output_type``, ``structured_output``
    carries the raw typed value (e.g. a Pydantic model instance) while
    ``text`` contains its serialized string form.
    """

    text: str
    structured_output: Any = None
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


# TTS
@dataclass(frozen=True)
class TTSAudio:
    """Audio chunk produced by TTS provider."""

    chunk: AudioChunk
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class TTSMarkers:
    """Word/viseme alignment markers from TTS."""

    markers: list[dict[str, Any]]
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


# Lifecycle
@dataclass(frozen=True)
class BotStartedSpeaking:
    """Bot began playing TTS audio."""

    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class BotStoppedSpeaking:
    """Bot finished playing TTS audio."""

    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class TurnStarted:
    """A new user turn has begun (VAD triggered)."""

    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class TurnEnded:
    """User turn has ended (speech capture complete)."""

    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


# Interruption
@dataclass(frozen=True)
class Interruption:
    """User barged in while bot was speaking."""

    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class PlaybackMarkAck:
    """Transport acknowledged playback reaching a previously queued mark."""

    mark_name: str
    timestamp: float = field(default_factory=time.monotonic)


# Tools
@dataclass(frozen=True)
class ToolCallStarted:
    """An agent tool call has started."""

    tool_name: str
    call_id: str
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class ToolCallDelta:
    """Streaming delta from an in-progress tool call."""

    call_id: str
    delta: str
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class ToolCallResult:
    """A tool call has completed with a result."""

    call_id: str
    result: str
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


# Reconnect
@dataclass(frozen=True)
class ReconnectAttempt:
    """A provider reconnection attempt is being made."""

    provider: str
    attempt: int
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class ReconnectSuccess:
    """A provider reconnection succeeded."""

    provider: str
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class ReconnectFailure:
    """A provider reconnection failed."""

    provider: str
    error: str
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


# Telephony
@dataclass(frozen=True)
class DTMF:
    """Single DTMF digit detected."""

    digit: str
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class DTMFAggregated:
    """Aggregated DTMF digit sequence."""

    sequence: str
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class VoicemailDetected:
    """Voicemail / answering machine detection result."""

    result: str  # "human" | "machine" | "unknown"
    source: str = ""  # "" = raw AMD, "fusion" = fused AMD+STT
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


# Outbound call lifecycle
@dataclass(frozen=True)
class CallInitiated:
    """Bot placed an outbound call."""

    call_sid: str
    to: str
    from_: str
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class CallRinging:
    """Remote phone is ringing."""

    call_sid: str
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class CallAnswered:
    """Call was answered (by human, machine, or screener)."""

    call_sid: str
    answered_by: str | None = None
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class CallScreening:
    """Call screening detected."""

    call_sid: str
    platform: str  # "ios" | "android" | "carrier" | "unknown"
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class CallFailed:
    """Call failed (busy, no answer, rejected, error)."""

    call_sid: str
    reason: str
    sip_code: int | None = None
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class CallEnded:
    """Call terminated."""

    call_sid: str
    duration_s: float | None = None
    disposition: str | None = None
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


# Error
@dataclass(frozen=True)
class Error:
    """Error event wrapping an exception."""

    exception: BaseException
    context: str = ""
    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic)


# Union of all EasyCat-level event types
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
    | BotStartedSpeaking
    | BotStoppedSpeaking
    | TurnStarted
    | TurnEnded
    | Interruption
    | PlaybackMarkAck
    | ToolCallStarted
    | ToolCallDelta
    | ToolCallResult
    | ReconnectAttempt
    | ReconnectSuccess
    | ReconnectFailure
    | DTMF
    | DTMFAggregated
    | VoicemailDetected
    | CallInitiated
    | CallRinging
    | CallAnswered
    | CallScreening
    | CallFailed
    | CallEnded
    | Error
)


# ── Provider-scoped event types ────────────────────────────────────
# Internal to provider implementations. Session maps these to EasyCat events.


class STTEventType(enum.Enum):
    PARTIAL = "partial"
    FINAL = "final"


@dataclass(frozen=True)
class WordTimestamp:
    """Timestamp for a single word in an STT transcript."""

    word: str
    start: float
    end: float


@dataclass(frozen=True)
class STTEvent:
    """Provider-scoped STT event produced by STT provider async iterators."""

    type: STTEventType
    text: str
    confidence: float | None = None
    language: str | None = None
    word_timestamps: list[WordTimestamp] | None = None
    timestamp: float = field(default_factory=time.monotonic)


class TTSEventType(enum.Enum):
    AUDIO = "audio"
    MARKERS = "markers"


@dataclass(frozen=True)
class TTSEvent:
    """Provider-scoped TTS event produced by TTS provider async iterators."""

    type: TTSEventType
    audio: AudioChunk | None = None
    markers: list[dict[str, Any]] | None = None
    timestamp: float = field(default_factory=time.monotonic)


# ── EventBus ───────────────────────────────────────────────────────


class EventBus:
    """Publish/subscribe event dispatcher supporting sync and async handlers."""

    def __init__(self) -> None:
        self._handlers: defaultdict[type, list[EventHandler]] = defaultdict(list)
        self._all_handlers: list[EventHandler] = []

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

    def subscribe_all(self, handler: EventHandler) -> None:
        """Register a handler that receives every emitted event."""
        self._all_handlers.append(handler)

    def unsubscribe_all(self, handler: EventHandler) -> None:
        """Remove a global handler registered by ``subscribe_all``."""
        try:
            self._all_handlers.remove(handler)
        except ValueError:
            pass

    async def emit(self, event: Event) -> None:
        """Emit an event to matching and global handlers.

        Sync handlers are called directly; async handlers are awaited.
        Exceptions in handlers are logged but do not prevent other handlers from running.
        """
        event_type = type(event)
        handlers = list(self._all_handlers)
        handlers.extend(self._handlers[event_type])
        for handler in handlers:
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
