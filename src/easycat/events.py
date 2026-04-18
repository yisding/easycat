"""Event types and EventBus dispatch system for EasyCat."""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from easycat.audio_format import AudioChunk

if TYPE_CHECKING:
    from easycat.session.actions import SessionAction, SessionActionResult


def _default_session_action_result() -> SessionActionResult:
    """Late-bound default factory — breaks the events ↔ session cycle."""
    from easycat.session.actions import SessionActionResult

    return SessionActionResult()


logger = logging.getLogger(__name__)

# Type alias for event handlers
EventHandler = Callable[..., None] | Callable[..., Coroutine[Any, Any, None]]


def _handler_name(handler: EventHandler) -> str:
    """Return a log-friendly name for a handler callable."""
    name = getattr(handler, "__name__", None)
    if name:
        return str(name)
    func = getattr(handler, "func", None)
    name = getattr(func, "__name__", None)
    if name:
        return str(name)
    return type(handler).__name__


# ── Base event class ─────────────────────────────────────────────


@dataclass(frozen=True)
class Event:
    """Base class for all EasyCat session events.

    Every event carries optional ``session_id`` / ``turn_id`` correlation
    fields (injected by :class:`Session`) and a monotonic ``timestamp``.
    """

    session_id: str | None = field(default=None, kw_only=True)
    turn_id: str | None = field(default=None, kw_only=True)
    timestamp: float = field(default_factory=time.monotonic, kw_only=True)


# ── EasyCat-level event dataclasses ──────────────────────────────


# Audio
@dataclass(frozen=True)
class AudioIn(Event):
    """Raw audio chunk received from transport."""

    chunk: AudioChunk


# VAD
@dataclass(frozen=True)
class VADStartSpeaking(Event):
    """VAD detected start of user speech."""


@dataclass(frozen=True)
class VADStopSpeaking(Event):
    """VAD detected end of user speech."""


# STT
@dataclass(frozen=True)
class STTPartial(Event):
    """Partial transcript from STT provider."""

    text: str
    track: str | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class STTFinal(Event):
    """Final transcript from STT provider for a completed turn."""

    text: str
    track: str | None = field(default=None, kw_only=True)


# Agent
@dataclass(frozen=True)
class AgentDelta(Event):
    """Streaming text delta from the agent."""

    text: str


@dataclass(frozen=True)
class AgentRequestStarted(Event):
    """The runtime has started the agent/LLM request for this turn."""


@dataclass(frozen=True)
class AgentFinal(Event):
    """Final complete response from the agent.

    When the agent uses a structured ``output_type``, ``structured_output``
    carries the raw typed value (e.g. a Pydantic model instance) while
    ``text`` contains its serialized string form.
    """

    text: str
    structured_output: Any = None


# TTS
@dataclass(frozen=True)
class TTSAudio(Event):
    """Audio chunk produced by TTS provider."""

    chunk: AudioChunk
    bypass_gate: bool = field(default=False, kw_only=True)


@dataclass(frozen=True)
class TTSMarkers(Event):
    """Word/viseme alignment markers from TTS."""

    markers: list[dict[str, Any]]


# Lifecycle
@dataclass(frozen=True)
class BotStartedSpeaking(Event):
    """Bot began playing TTS audio."""


@dataclass(frozen=True)
class BotStoppedSpeaking(Event):
    """Bot finished playing TTS audio."""


@dataclass(frozen=True)
class TurnStarted(Event):
    """A new user turn has begun (VAD triggered)."""


@dataclass(frozen=True)
class TurnEnded(Event):
    """User turn has ended (speech capture complete)."""


# Interruption
@dataclass(frozen=True)
class Interruption(Event):
    """User barged in while bot was speaking."""


@dataclass(frozen=True)
class PlaybackMarkAck(Event):
    """Transport acknowledged playback reaching a previously queued mark."""

    mark_name: str


# Tools
@dataclass(frozen=True)
class ToolCallStarted(Event):
    """An agent tool call has started."""

    tool_name: str
    call_id: str


@dataclass(frozen=True)
class ToolCallDelta(Event):
    """Streaming delta from an in-progress tool call."""

    call_id: str
    delta: str


@dataclass(frozen=True)
class ToolCallResult(Event):
    """A tool call has completed with a result."""

    call_id: str
    result: str


# Reconnect
@dataclass(frozen=True)
class ReconnectAttempt(Event):
    """A provider reconnection attempt is being made."""

    provider: str
    attempt: int


@dataclass(frozen=True)
class ReconnectSuccess(Event):
    """A provider reconnection succeeded."""

    provider: str


@dataclass(frozen=True)
class ReconnectFailure(Event):
    """A provider reconnection failed."""

    provider: str
    error: str


# Telephony
@dataclass(frozen=True)
class DTMF(Event):
    """Single DTMF digit detected."""

    digit: str


@dataclass(frozen=True)
class DTMFAggregated(Event):
    """Aggregated DTMF digit sequence."""

    sequence: str


@dataclass(frozen=True)
class VoicemailDetected(Event):
    """Voicemail / answering machine detection result."""

    result: Literal["human", "machine", "unknown"]
    source: Literal["", "fusion", "detector"] = ""


# Outbound call lifecycle
@dataclass(frozen=True)
class CallInitiated(Event):
    """Bot placed an outbound call."""

    call_sid: str
    to: str
    from_: str


@dataclass(frozen=True)
class CallRinging(Event):
    """Remote phone is ringing."""

    call_sid: str


@dataclass(frozen=True)
class CallAnswered(Event):
    """Call was answered (by human, machine, or screener)."""

    call_sid: str
    answered_by: str | None = None


@dataclass(frozen=True)
class CallScreening(Event):
    """Call screening detected."""

    call_sid: str
    platform: Literal["ios", "android", "carrier", "third_party", "unknown"]


@dataclass(frozen=True)
class ScreeningTimedOut(Event):
    """Screening exhausted max turns without resolution."""

    call_sid: str = ""


@dataclass(frozen=True)
class CallFailed(Event):
    """Call failed (busy, no answer, rejected, error)."""

    call_sid: str
    reason: str
    sip_code: int | None = None
    number: str | None = None


@dataclass(frozen=True)
class CallEnded(Event):
    """Call terminated."""

    call_sid: str
    duration_s: float | None = None
    disposition: str | None = None
    number: str | None = None


# Error


class ErrorStage(enum.StrEnum):
    """Pipeline stage where an error occurred."""

    STT = "stt"
    AGENT = "agent"
    TTS = "tts"
    PIPELINE = "pipeline"


@dataclass(frozen=True)
class Error(Event):
    """Error event wrapping an exception.

    ``stage`` identifies the pipeline stage (STT, agent, TTS, or general
    pipeline).  ``provider`` optionally names the provider implementation
    that raised the error.
    """

    exception: BaseException
    stage: ErrorStage = ErrorStage.PIPELINE
    provider: str | None = None


# Session actions (agent-requested)


@dataclass(frozen=True)
class SessionActionRequested(Event):
    """A session action has been dequeued and is about to run."""

    action: SessionAction


@dataclass(frozen=True)
class SessionActionStarted(Event):
    """A session action has started executing."""

    action: SessionAction
    executor: str


@dataclass(frozen=True)
class SessionActionCompleted(Event):
    """A session action completed successfully."""

    action: SessionAction
    executor: str
    result: SessionActionResult = field(default_factory=_default_session_action_result)


@dataclass(frozen=True)
class SessionActionFailed(Event):
    """A session action failed or had no supporting executor."""

    action: SessionAction
    error: str
    executor: str | None = None


# ── Event groups ─────────────────────────────────────────────────────
# Semantic groupings of EasyCat-level events for bulk subscription.

AUDIO_EVENTS: tuple[type[Event], ...] = (AudioIn,)
VAD_EVENTS: tuple[type[Event], ...] = (VADStartSpeaking, VADStopSpeaking)
STT_EVENTS: tuple[type[Event], ...] = (STTPartial, STTFinal)
AGENT_EVENTS: tuple[type[Event], ...] = (AgentRequestStarted, AgentDelta, AgentFinal)
TTS_EVENTS: tuple[type[Event], ...] = (TTSAudio, TTSMarkers)
TOOL_EVENTS: tuple[type[Event], ...] = (ToolCallStarted, ToolCallDelta, ToolCallResult)
LIFECYCLE_EVENTS: tuple[type[Event], ...] = (
    TurnStarted,
    TurnEnded,
    BotStartedSpeaking,
    BotStoppedSpeaking,
)
INTERRUPTION_EVENTS: tuple[type[Event], ...] = (Interruption, PlaybackMarkAck)
RECONNECT_EVENTS: tuple[type[Event], ...] = (ReconnectAttempt, ReconnectSuccess, ReconnectFailure)
TELEPHONY_EVENTS: tuple[type[Event], ...] = (
    DTMF,
    DTMFAggregated,
    VoicemailDetected,
    CallInitiated,
    CallRinging,
    CallAnswered,
    CallScreening,
    ScreeningTimedOut,
    CallFailed,
    CallEnded,
)
ERROR_EVENTS: tuple[type[Event], ...] = (Error,)
ACTION_EVENTS: tuple[type[Event], ...] = (
    SessionActionRequested,
    SessionActionStarted,
    SessionActionCompleted,
    SessionActionFailed,
)

ALL_EVENTS: tuple[type[Event], ...] = (
    AUDIO_EVENTS
    + VAD_EVENTS
    + STT_EVENTS
    + AGENT_EVENTS
    + TTS_EVENTS
    + TOOL_EVENTS
    + LIFECYCLE_EVENTS
    + INTERRUPTION_EVENTS
    + RECONNECT_EVENTS
    + TELEPHONY_EVENTS
    + ERROR_EVENTS
    + ACTION_EVENTS
)


# ── Provider-scoped event types ──────────────────────────────────
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
    track: str | None = None
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


# ── EventBus ─────────────────────────────────────────────────────


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

        Handlers registered for the exact event type **and** any of its
        parent classes (up to and including :class:`Event`) are invoked.
        Sync handlers are called directly; async handlers are awaited.
        Exceptions in handlers are logged but do not prevent other handlers from running.
        """
        event_type = type(event)
        handlers = list(self._all_handlers)
        for cls in event_type.__mro__:
            handlers.extend(self._handlers[cls])
            if cls is Event:
                break
        for handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "Error in handler %s for event %s",
                    _handler_name(handler),
                    event_type.__name__,
                )
