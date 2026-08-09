"""Event types and EventBus dispatch system for EasyCat.

The EventBus drives application behavior — it is NOT an observability sink.
For durable, replayable records use ``session.journal`` or
``export_debug_bundle()``; for production telemetry configure an OpenTelemetry
SDK; for human diagnostics use the ``easycat`` stdlib logger
(``EASYCAT_LOG_LEVEL``). See docs/observability.md for the four-layer model.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import math
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
EventHandlerErrorPolicy = Literal["continue", "raise"]


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


@dataclass(frozen=True)
class AudioOut(Event):
    """Audio chunk past EasyCat's last retractable transport buffer.

    For direct transports this is emitted once the transport accepts the
    chunk. Buffered transports may defer emission until the chunk has
    crossed their own clearable queue, so later barge-ins do not report
    audio EasyCat can still discard.
    """

    chunk: AudioChunk


@dataclass(frozen=True)
class TransportAudioDelivered(Event):
    """Internal transport callback for chunks that crossed a clearable buffer."""

    chunk: AudioChunk
    turn_ref: Any = field(default=None, kw_only=True, repr=False, compare=False)


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
    """Streaming text update from the agent.

    Most events append ``text``. When ``replacement`` is true, consumers
    replace the complete text part at ``part_index`` instead.
    """

    text: str
    part_index: int | None = field(default=None, kw_only=True)
    replacement: bool = field(default=False, kw_only=True)


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
    """Best-effort alignment markers from TTS.

    ``markers`` is a list of *provider-native* alignment payloads, NOT a
    normalized cross-provider schema: Cartesia emits word-level
    ``word_timestamps`` objects while ElevenLabs emits char-level
    ``alignment`` dicts, and some providers (e.g. Deepgram) emit none. The
    journal records them opaquely for forensic/debug use; consumers MUST NOT
    assume a uniform shape. A typed, normalized marker schema (mirroring the
    STT-side :class:`WordTimestamp`) is the migration path if a portable
    cross-provider consumer is ever needed.
    """

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


_TURN_STARTED_OBSERVATION_MARKER = object()


def _mark_turn_started_observation(event: TurnStarted) -> TurnStarted:
    """Mark an internally published TurnStarted as observation-only."""
    object.__setattr__(event, "_easycat_observation_marker", _TURN_STARTED_OBSERVATION_MARKER)
    return event


def _is_turn_started_observation(event: TurnStarted) -> bool:
    """Return whether private lifecycle publication preceded this public event."""
    return getattr(event, "_easycat_observation_marker", None) is _TURN_STARTED_OBSERVATION_MARKER


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


# Transport diagnostics
@dataclass(frozen=True)
class TransportDegraded(Event):
    """A transport hit a non-fatal degradation or an abnormal teardown.

    Emitted on the session :class:`EventBus` so :class:`SessionJournalSink`
    can record drop / poison / abort conditions that would otherwise only
    reach the debug log — keeping the journal the single source of truth for
    observability (see ``runtime/``).

    ``reason`` is a stable machine code owned by the emitting transport (see
    that transport's ``_DEGRADED_*`` constants for the vocabulary).
    ``fatal`` is True when the condition tore the underlying session down
    rather than just dropping a frame.
    """

    provider: str
    reason: str
    detail: str = ""
    fatal: bool = False


# Supervisor audit
@dataclass(frozen=True)
class SupervisorListenerAttached(Event):
    """A passive supervisor listener subscribed to session audio."""

    listener_id: int
    queue_size: int


@dataclass(frozen=True)
class SupervisorListenerDetached(Event):
    """A passive supervisor listener detached from session audio."""

    listener_id: int
    dropped_frames: int = 0
    reason: Literal["unsubscribe", "close"] = "unsubscribe"


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
    call_sid: str = ""


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
class ScreeningResponse(Event):
    """Call screening response requested by the detector."""

    text: str
    mode: Literal["static", "agent"]


class IVRActionType(enum.Enum):
    DTMF = "dtmf"
    SPEAK = "speak"
    WAIT = "wait"
    HANGUP = "hangup"
    HOLD = "hold"
    HUMAN_DETECTED = "human_detected"


@dataclass(frozen=True)
class IVRAction(Event):
    """IVR navigator action decided by the agent or timeout policy."""

    type: IVRActionType
    digits: str = ""
    text: str = ""
    menu_depth: int = 0


@dataclass(frozen=True)
class CallStateChanged(Event):
    """Outbound call state transition."""

    old: Any
    new: Any
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
    that raised the error.  ``code`` is a stable ``EASYCAT_Exxx`` code
    when available, making journal ``Error`` records machine-correlatable
    with ``easycat explain``; it defaults to the wrapped exception's
    ``code`` attribute (e.g. the runtime timeout errors expose one) and
    falls back to ``None`` for uncoded exceptions.
    """

    exception: BaseException
    stage: ErrorStage = ErrorStage.PIPELINE
    provider: str | None = None
    code: str | None = None
    elapsed_ms: float | None = None
    sequence: int | None = None
    record_key: str | None = None

    def __post_init__(self) -> None:
        if self.code is None:
            inferred = getattr(self.exception, "code", None)
            if isinstance(inferred, str) and inferred:
                # Frozen dataclass: bypass the immutability guard to
                # backfill the code derived from the wrapped exception.
                object.__setattr__(self, "code", inferred)
        _add_exception_notes(
            self.exception,
            stage=self.stage.value,
            provider=self.provider,
            code=self.code,
            session_id=self.session_id,
            turn_id=self.turn_id,
            elapsed_ms=self.elapsed_ms,
            sequence=self.sequence,
            record_key=self.record_key,
        )


def _add_exception_notes(exc: BaseException, **context: Any) -> None:
    """Attach deduplicated PEP 678 notes to journal-visible exceptions."""
    existing = getattr(exc, "__notes__", None)
    if not isinstance(existing, list):
        existing = []
    for key, value in context.items():
        rendered = _render_exception_note_value(value)
        if rendered is None:
            continue
        note = f"{key}={rendered}"
        if any(str(existing_note).startswith(f"{key}=") for existing_note in existing):
            continue
        try:
            exc.add_note(note)
        except Exception:  # noqa: BLE001  # pragma: no cover - defensive
            return


def _render_exception_note_value(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


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

AUDIO_EVENTS: tuple[type[Event], ...] = (AudioIn, AudioOut)
TRANSPORT_EVENTS: tuple[type[Event], ...] = (TransportAudioDelivered, TransportDegraded)
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
SUPERVISOR_EVENTS: tuple[type[Event], ...] = (
    SupervisorListenerAttached,
    SupervisorListenerDetached,
)
TELEPHONY_EVENTS: tuple[type[Event], ...] = (
    DTMF,
    DTMFAggregated,
    VoicemailDetected,
    CallInitiated,
    CallRinging,
    CallAnswered,
    CallScreening,
    ScreeningTimedOut,
    ScreeningResponse,
    IVRAction,
    CallStateChanged,
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
    + TRANSPORT_EVENTS
    + VAD_EVENTS
    + STT_EVENTS
    + AGENT_EVENTS
    + TTS_EVENTS
    + TOOL_EVENTS
    + LIFECYCLE_EVENTS
    + INTERRUPTION_EVENTS
    + RECONNECT_EVENTS
    + SUPERVISOR_EVENTS
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
    # ``confidence`` and ``word_timestamps`` are provider-captured metadata:
    # the Session pipeline drives turns off ``text``/``track`` only, but the
    # STT committer records both into the ``stt_segment_final`` journal entry
    # (when populated) for postmortem observability. Not every provider fills
    # them in; they default to ``None``.
    confidence: float | None = None
    language: str | None = None
    word_timestamps: list[WordTimestamp] | None = None
    track: str | None = None
    # Provider transport boundaries may finalize a transcript segment without
    # representing a semantic end of the user's turn. Native-endpoint sessions
    # only auto-end the turn for endpoint-bearing finals.
    ends_turn: bool = True
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
    """Publish/subscribe event dispatcher supporting sync and async handlers.

    Dispatch is inline by default: ``emit()`` invokes matching handlers in
    subscription order and awaits async handlers. The default
    ``handler_error_policy="continue"`` logs handler exceptions and keeps
    dispatching remaining public handlers. Reserved internal lifecycle handlers
    always fail closed before public observation. Use
    ``handler_error_policy="raise"`` in tests or strict app code when any public
    handler failure should abort dispatch and propagate to the emitter. Use the
    returned :class:`EventSubscription` when lifecycle ownership matters; the
    older ``unsubscribe(event_type, handler)`` form remains supported.
    """

    def __init__(
        self,
        *,
        slow_handler_threshold_s: float | None = None,
        handler_error_policy: EventHandlerErrorPolicy = "continue",
    ) -> None:
        if slow_handler_threshold_s is not None and (
            isinstance(slow_handler_threshold_s, bool)
            or not isinstance(slow_handler_threshold_s, int | float)
            or not math.isfinite(slow_handler_threshold_s)
            or slow_handler_threshold_s < 0
        ):
            raise ValueError("slow_handler_threshold_s must be non-negative and finite")
        if handler_error_policy not in {"continue", "raise"}:
            raise ValueError("handler_error_policy must be either 'continue' or 'raise'")
        self._handlers: defaultdict[type, list[EventHandler]] = defaultdict(list)
        self._reserved_handlers: defaultdict[type, list[EventHandler]] = defaultdict(list)
        self._all_handlers: list[EventHandler] = []
        self._handler_failures = 0
        self._last_handler_error: HandlerDispatchError | None = None
        self._slow_handler_threshold_s = slow_handler_threshold_s
        self._handler_error_policy = handler_error_policy

    @property
    def handler_failures(self) -> int:
        """Number of handler exceptions observed by this bus."""
        return self._handler_failures

    @property
    def slow_handler_threshold_s(self) -> float | None:
        """Elapsed time at which an inline handler produces a warning."""
        return self._slow_handler_threshold_s

    @property
    def handler_error_policy(self) -> EventHandlerErrorPolicy:
        """How ``emit`` handles exceptions raised by event handlers."""
        return self._handler_error_policy

    @property
    def last_handler_error(self) -> HandlerDispatchError | None:
        """Most recent handler exception metadata, if any."""
        return self._last_handler_error

    def subscribe(self, event_type: type, handler: EventHandler) -> EventSubscription:
        """Register a handler for a specific event type."""
        self._handlers[event_type].append(handler)
        return EventSubscription(self, event_type, handler)

    def _subscribe_reserved(
        self,
        event_type: type,
        handler: EventHandler,
    ) -> EventSubscription:
        """Register an internal handler that runs before every public observer."""
        self._reserved_handlers[event_type].append(handler)
        return EventSubscription(self, event_type, handler, reserved=True)

    def unsubscribe(self, event_type: type, handler: EventHandler) -> None:
        """Remove a handler for a specific event type."""
        handlers = self._handlers.get(event_type)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            pass

    def _unsubscribe_reserved(self, event_type: type, handler: EventHandler) -> None:
        handlers = self._reserved_handlers.get(event_type)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            pass

    def subscribers(self, event_type: type) -> list[EventHandler]:
        """Return a snapshot of handlers registered for the exact event type.

        Only handlers registered via :meth:`subscribe` for *this exact* type are
        included — parent-class handlers and :meth:`subscribe_all` handlers are
        excluded, matching the buckets :meth:`emit` walks per class. The result
        is a fresh list, so callers may iterate or filter it without observing
        later subscribe/unsubscribe mutations. Intended for collaborators that
        must reason about who else listens to an internal event (e.g. a router
        deciding whether it is the sole owner of a bus-scoped callback).
        """
        handlers = self._handlers.get(event_type)
        return list(handlers) if handlers else []

    def subscribe_all(self, handler: EventHandler) -> EventSubscription:
        """Register a handler that receives every emitted event."""
        self._all_handlers.append(handler)
        return EventSubscription(self, None, handler, all_events=True)

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
        Public handler exceptions follow ``handler_error_policy``. Reserved
        lifecycle handler exceptions always abort before public observation.
        """
        event_type = type(event)
        # Reserved exact-type handlers establish private lifecycle state before
        # the event becomes visible to subscribe_all(), exact-type, or parent
        # observers. They are intentionally excluded from ``subscribers()``.
        reserved = self._reserved_handlers.get(event_type)
        handlers: list[EventHandler] = list(reserved) if reserved else []
        reserved_count = len(handlers)
        # Build the handler list lazily.  This runs on the per-audio-chunk hot
        # path, so avoid the ``list(...)`` copy when there are no global
        # handlers, and read ``_handlers`` with ``.get`` so an emit with no
        # subscriber does not mutate the defaultdict with an empty bucket.
        if self._all_handlers:
            handlers.extend(self._all_handlers)
        for cls in event_type.__mro__:
            bucket = self._handlers.get(cls)
            if bucket:
                handlers.extend(bucket)
            if cls is Event:
                break
        for index, handler in enumerate(handlers):
            started = time.perf_counter()
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                self._handler_failures += 1
                self._last_handler_error = HandlerDispatchError(
                    handler_name=_handler_name(handler),
                    event_type=event_type.__name__,
                    exception=exc,
                )
                logger.exception(
                    "Error in handler %s for event %s",
                    _handler_name(handler),
                    event_type.__name__,
                )
                # A reserved handler establishes private lifecycle invariants;
                # exposing the event after it failed would invert the promised
                # ordering. Reserved failures therefore always fail closed.
                if index < reserved_count or self._handler_error_policy == "raise":
                    raise
            finally:
                elapsed = time.perf_counter() - started
                threshold = self._slow_handler_threshold_s
                if threshold is not None and elapsed >= threshold:
                    logger.warning(
                        "Slow handler %s for event %s took %.3fs",
                        _handler_name(handler),
                        event_type.__name__,
                        elapsed,
                    )


@dataclass(frozen=True)
class HandlerDispatchError:
    """Metadata for the most recent EventBus handler exception."""

    handler_name: str
    event_type: str
    exception: Exception


class EventSubscription:
    """Handle returned by :meth:`EventBus.subscribe`.

    Calling :meth:`unsubscribe` is idempotent, which makes it safe to keep the
    token on long-lived collaborators and release it during teardown without
    tracking whether teardown already ran.
    """

    def __init__(
        self,
        bus: EventBus,
        event_type: type | None,
        handler: EventHandler,
        *,
        all_events: bool = False,
        reserved: bool = False,
    ) -> None:
        self._bus = bus
        self.event_type = event_type
        self.handler = handler
        self.all_events = all_events
        self.reserved = reserved
        self._active = True

    @property
    def active(self) -> bool:
        """Whether this token is still subscribed."""
        return self._active

    def unsubscribe(self) -> None:
        """Remove this subscription if it is still active."""
        if not self._active:
            return
        if self.all_events:
            self._bus.unsubscribe_all(self.handler)
        elif self.reserved and self.event_type is not None:
            self._bus._unsubscribe_reserved(self.event_type, self.handler)
        elif self.event_type is not None:
            self._bus.unsubscribe(self.event_type, self.handler)
        self._active = False
