"""Session-owned journal sink.

Translates session events and explicit observability calls into execution
journal records.  The sink is intentionally small: Session still owns runtime
lifecycle and public debug surfaces, while this component owns journal writes
and event-bus subscription handlers.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from easycat import _observability as observability
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AgentRequestStarted,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    EventBus,
    EventHandler,
    Interruption,
    PlaybackMarkAck,
    ReconnectAttempt,
    ReconnectFailure,
    ReconnectSuccess,
    SessionActionCompleted,
    SessionActionFailed,
    SessionActionRequested,
    SessionActionStarted,
    STTFinal,
    STTPartial,
    SupervisorListenerAttached,
    SupervisorListenerDetached,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TransportDegraded,
    TTSAudio,
    TTSMarkers,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.runtime.artifacts import ArtifactClass, ArtifactStore
from easycat.runtime.journal import ExecutionJournal
from easycat.runtime.records import ErrorInfo, JournalRecordKind
from easycat.validation.redaction import redact_value

logger = logging.getLogger(__name__)
_JOURNAL_ATTRS = (
    "text",
    "track",
    "result",
    "action",
    "executor",
    "tool_name",
    "call_id",
    "delta",
    "listener_id",
    "queue_size",
    "dropped_frames",
    "reason",
    "error",
    "structured_output",
)

# ``Any``-typed event fields that may carry Pydantic models / dataclasses /
# other non-JSON-native objects.  Persistent backends serialize ``data`` with
# ``json.dumps(..., default=str)``, which would silently repr-stringify these
# (discarding the model's fields), while the in-memory backend keeps them live
# — the same record would round-trip to a different shape per backend.  We
# normalize them once here so all backends store identical JSON-native shapes.
_JSONABLE_ATTRS = frozenset({"structured_output", "result", "action"})
_MAX_TRANSPORT_DEGRADED_DETAIL_CHARS = 512
_REDACTED_SESSION_ACTION_VALUE = "[REDACTED_SESSION_ACTION_VALUE]"
_REDACTED_SESSION_ACTION_PAYLOAD = "[REDACTED_SESSION_ACTION_PAYLOAD]"
_SESSION_ACTION_SENSITIVE_KEYS = frozenset(
    {
        "body",
        "caller_id",
        "digits",
        "from",
        "from_",
        "from_number",
        "number",
        "payload",
        "post_dial_digits",
        "target",
        "to",
    }
)


def _truncate_transport_degraded_detail(detail: str) -> str:
    """Bound persisted transport diagnostic strings from untrusted transports."""
    if len(detail) <= _MAX_TRANSPORT_DEGRADED_DETAIL_CHARS:
        return detail
    omitted = len(detail) - _MAX_TRANSPORT_DEGRADED_DETAIL_CHARS
    return f"{detail[:_MAX_TRANSPORT_DEGRADED_DETAIL_CHARS]}… (truncated {omitted} chars)"


def _coerce_json_native(value: Any) -> Any:
    """Recursively coerce *value* to a JSON-native structure.

    ``dataclasses.asdict`` / ``model_dump`` only rebuild the container tree;
    non-JSON-native leaves (a ``set`` / ``bytes`` / ``datetime`` inside a
    ``CustomAction.payload`` dict, say) survive as live Python objects.  The
    persistent backend then ``json.dumps(default=str)``-stringifies them while
    the in-memory backend keeps them live, so the same record would round-trip
    to a different shape per backend.  Round-tripping through ``json`` here
    forces an identical JSON-native shape for every backend.
    """
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return value


def _to_jsonable(value: Any) -> Any:
    """Best-effort conversion of *value* to a JSON-native structure.

    Pydantic models -> ``model_dump()``; dataclasses -> ``asdict``; both are
    then recursively coerced so non-JSON-native leaves can't diverge per
    backend.  Anything else is returned unchanged (the journal's ``json.dumps``
    default-handler still catches whatever is left non-serializable).
    """
    # Common case (a plain string/number/bool result) is already JSON-native;
    # skip the model_dump/dataclass probing entirely.
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            try:
                return _coerce_json_native(dump())
            except Exception:
                return value
        except Exception:
            return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            data = dataclasses.asdict(value)
        except Exception:
            return value
        action_type = getattr(value, "type", None)
        if action_type is not None:
            data["type"] = getattr(action_type, "value", action_type)
        return _coerce_json_native(data)
    return value


def _redact_session_action_data(value: Any, key: str | None = None) -> Any:
    """Redact sensitive session-action fields before journaling.

    Session actions can carry telephony secrets and customer content (DTMF
    PINs/account numbers, SMS bodies, transfer targets, and arbitrary custom
    payloads).  Generic validation redaction catches long phone numbers and
    token-like values, but short DTMF strings and provider-neutral field names
    need action-specific minimization at the journal boundary.
    """
    normalized_key = str(key).lower() if key is not None else None
    if normalized_key == "payload" and value not in ({}, None):
        return _REDACTED_SESSION_ACTION_PAYLOAD
    if normalized_key in _SESSION_ACTION_SENSITIVE_KEYS and value not in ("", None):
        return _REDACTED_SESSION_ACTION_VALUE
    if isinstance(value, dict):
        return {
            str(item_key): _redact_session_action_data(item_value, str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        return [_redact_session_action_data(item, key) for item in value]
    return redact_value(value, key)


def _journal_attr_value(attr: str, value: Any) -> Any:
    jsonable = _to_jsonable(value) if attr in _JSONABLE_ATTRS else value
    if attr in {"action", "result"}:
        return _redact_session_action_data(jsonable)
    return jsonable


class TurnIdResolver(Protocol):
    """Resolve the turn id to record, defaulting to the active turn.

    Matches the ``current_turn_id`` member of the runtime layer's
    structural ``JournalSink`` protocols (callable with an optional
    ``turn_id`` keyword), so a ``SessionJournalSink`` instance satisfies
    them without adapters.
    """

    def __call__(self, turn_id: str | None = None) -> str | None: ...


@dataclass(slots=True)
class SessionJournalSink:
    """Write session activity to an execution journal."""

    event_bus: EventBus
    journal: ExecutionJournal | None
    artifact_store: ArtifactStore | None
    session_id: str
    current_turn_id: TurnIdResolver
    _subscribed: bool = field(default=False, init=False)

    def subscribe(self) -> None:
        """Subscribe event bus handlers that write session events to the journal."""
        if self.journal is None or self._subscribed:
            return
        self._subscribed = True

        evt = JournalRecordKind.EVENT
        ctl = JournalRecordKind.CONTROL

        self._subscribe(TurnStarted, self._make_event_handler(evt, "turn_started"))
        self._subscribe(TurnEnded, self._make_event_handler(evt, "turn_ended"))
        self._subscribe(VADStartSpeaking, self._make_event_handler(evt, "vad_start_speaking"))
        self._subscribe(VADStopSpeaking, self._make_event_handler(evt, "vad_stop_speaking"))
        self._subscribe(STTPartial, self._make_event_handler(evt, "stt_partial"))
        self._subscribe(STTFinal, self._make_event_handler(evt, "stt_final"))
        self._subscribe(
            AgentRequestStarted,
            self._make_event_handler(evt, "agent_request_started"),
        )
        self._subscribe(AgentDelta, self._make_event_handler(evt, "agent_delta"))
        self._subscribe(AgentFinal, self._make_event_handler(evt, "agent_final"))
        self._subscribe(TTSAudio, self._handle_tts_audio)
        self._subscribe(TTSMarkers, self._handle_tts_markers)
        self._subscribe(
            BotStartedSpeaking,
            self._make_event_handler(evt, "bot_started_speaking"),
        )
        self._subscribe(
            BotStoppedSpeaking,
            self._make_event_handler(evt, "bot_stopped_speaking"),
        )
        self._subscribe(Interruption, self._handle_interruption(ctl))
        self._subscribe(Error, self._make_event_handler(evt, "error"))
        self._subscribe(ToolCallStarted, self._make_event_handler(evt, "tool_call_started"))
        self._subscribe(ToolCallDelta, self._make_event_handler(evt, "tool_call_delta"))
        self._subscribe(ToolCallResult, self._make_event_handler(evt, "tool_call_result"))
        self._subscribe(
            SessionActionRequested,
            self._make_event_handler(evt, "session_action_requested"),
        )
        self._subscribe(
            SessionActionStarted,
            self._make_event_handler(evt, "session_action_started"),
        )
        self._subscribe(
            SessionActionCompleted,
            self._make_event_handler(evt, "session_action_completed"),
        )
        self._subscribe(
            SessionActionFailed,
            self._make_event_handler(evt, "session_action_failed"),
        )
        # ReconnectingWebSocket emits these on the bus; journal records make
        # the retry timeline visible in exported bundles.
        self._subscribe(ReconnectAttempt, self._make_event_handler(evt, "ws_reconnect_attempt"))
        self._subscribe(ReconnectSuccess, self._make_event_handler(evt, "ws_reconnect_success"))
        self._subscribe(ReconnectFailure, self._make_event_handler(evt, "ws_reconnect_failure"))
        # Passive supervisor listener lifecycle is audit-relevant: these
        # records show who attached to a session's audio fan-out and whether
        # slow listeners dropped frames before detaching.
        self._subscribe(
            SupervisorListenerAttached,
            self._make_event_handler(evt, "supervisor_listener_attached"),
        )
        self._subscribe(
            SupervisorListenerDetached,
            self._make_event_handler(evt, "supervisor_listener_detached"),
        )
        # PlaybackMarkAck is also consumed by Session state tracking; keep a
        # separate journal timeline of what the client rendered when.
        self._subscribe(PlaybackMarkAck, self._make_event_handler(evt, "playback_mark_ack"))
        # Transports emit these for drop/poison/abort conditions that would
        # otherwise only reach the debug log; recording them keeps the
        # journal the single source of truth for observability.
        self._subscribe(TransportDegraded, self._handle_transport_degraded)

    def replace_backends(
        self,
        *,
        journal: ExecutionJournal | None,
        artifact_store: ArtifactStore | None,
    ) -> None:
        """Retarget writes after Session swaps live backends for snapshots."""
        self.journal = journal
        self.artifact_store = artifact_store

    def store_artifact(
        self,
        payload: bytes,
        *,
        artifact_class: ArtifactClass = "debug_verbose",
    ) -> str | None:
        if self.artifact_store is None or not payload:
            return None
        ref = self.artifact_store.put(payload, artifact_class=artifact_class)
        return ref or None

    def append_record(
        self,
        *,
        name: str,
        kind: JournalRecordKind = JournalRecordKind.EVENT,
        turn_id: str | None = None,
        data: dict[str, Any] | None = None,
        input_bytes: bytes | None = None,
        output_bytes: bytes | None = None,
        input_artifact_class: ArtifactClass = "debug_verbose",
        output_artifact_class: ArtifactClass = "debug_verbose",
    ) -> None:
        if self.journal is None:
            return
        input_ref = (
            self.store_artifact(input_bytes, artifact_class=input_artifact_class)
            if input_bytes is not None
            else None
        )
        output_ref = (
            self.store_artifact(output_bytes, artifact_class=output_artifact_class)
            if output_bytes is not None
            else None
        )
        resolved_turn_id = self.current_turn_id(turn_id)
        self.journal.append(
            kind=kind,
            name=name,
            session_id=self.session_id,
            turn_id=resolved_turn_id,
            data=data,
            input_ref=input_ref,
            output_ref=output_ref,
        )

    def _subscribe(self, event_type: type, handler: EventHandler) -> None:
        self.event_bus.subscribe(event_type, handler)

    def _make_event_handler(self, kind: JournalRecordKind, name: str) -> EventHandler:
        def _handler(event: Any) -> None:
            if self.journal is None:
                return
            data: dict[str, Any] = {}
            for attr in _JOURNAL_ATTRS:
                val = getattr(event, attr, None)
                if val is not None:
                    data[attr] = _journal_attr_value(attr, val)
            error = None
            exc = getattr(event, "exception", None)
            if exc is not None:
                stage = getattr(event, "stage", None)
                if stage is not None and hasattr(stage, "value"):
                    data["stage"] = stage.value
                provider = getattr(event, "provider", None)
                if provider:
                    data["provider"] = provider
                code = getattr(event, "code", None)
                if code:
                    # Stable EASYCAT_Exxx code from the Error event; keep it in
                    # ``data`` so exported journals stay machine-correlatable
                    # (ErrorInfo has no dedicated field for it).
                    data["code"] = code
                elapsed_ms = getattr(event, "elapsed_ms", None)
                if elapsed_ms is not None:
                    data["elapsed_ms"] = elapsed_ms
                sequence = getattr(event, "sequence", None)
                if sequence is not None:
                    data["sequence"] = sequence
                record_key = getattr(event, "record_key", None)
                if record_key:
                    data["record_ref"] = record_key
                error = ErrorInfo.from_exception(exc)
            self.journal.append(
                kind=kind,
                name=name,
                session_id=getattr(event, "session_id", None) or self.session_id,
                turn_id=getattr(event, "turn_id", None),
                data=data or None,
                error=error,
            )

        return _handler

    def _handle_interruption(self, kind: JournalRecordKind) -> EventHandler:
        """Journal the interruption and bump the ``easycat.interruption`` counter.

        The counter carries only the low-cardinality ``easycat.surface`` (``vad``)
        attribute — never ``turn_id`` / ``transcript``, which the observability
        sanitizer rejects as forbidden keys.  The cutoff-latency histogram is
        defined for OTel consumers but not emitted here: the sink sees the
        barge-in event before the bot has stopped, so the cutoff delta is
        computed offline by the issues engine.
        """
        journal_handler = self._make_event_handler(kind, "interruption")

        def _handler(event: Any) -> None:
            journal_handler(event)
            observability.increment_counter(
                "easycat.interruption.total",
                attributes={"easycat.surface": "vad"},
            )

        return _handler

    def _handle_tts_audio(self, event: TTSAudio) -> None:
        # TTSStage captures replay-critical audio bytes via ``tts_frame``.
        # The session-level ``tts_audio`` record stays metadata-only for
        # legacy observers.
        self.append_record(
            name="tts_audio",
            turn_id=event.turn_id,
            data={
                "audio_bytes": len(event.chunk.data),
                "duration_ms": event.chunk.duration_ms,
                "sample_rate": event.chunk.format.sample_rate,
                "channels": event.chunk.format.channels,
                "sample_width": event.chunk.format.sample_width,
                "encoding": event.chunk.format.encoding,
                "bypass_gate": event.bypass_gate,
            },
        )

    def _handle_tts_markers(self, event: TTSMarkers) -> None:
        self.append_record(
            name="tts_markers",
            turn_id=event.turn_id,
            data={"markers": event.markers},
        )

    def _handle_transport_degraded(self, event: TransportDegraded) -> None:
        # Fatal teardowns are control-plane events (mirrors ``interruption``);
        # recoverable single-frame drops stay on the EVENT timeline.
        self.append_record(
            name="transport_degraded",
            kind=JournalRecordKind.CONTROL if event.fatal else JournalRecordKind.EVENT,
            turn_id=event.turn_id,
            data={
                "provider": event.provider,
                "reason": event.reason,
                "detail": _truncate_transport_degraded_detail(event.detail),
                "fatal": event.fatal,
            },
        )
