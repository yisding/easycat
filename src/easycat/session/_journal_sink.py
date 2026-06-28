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
from collections.abc import Callable
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
from easycat.runtime.costs import cost_budget_status, finite_number
from easycat.runtime.journal import ExecutionJournal
from easycat.runtime.records import ErrorInfo, JournalRecordKind

_COST_RECORD_NAMES = frozenset({"cost", "cost_record"})
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
    max_session_cost_usd: float | None = None
    on_cost_budget_exceeded: Callable[[dict[str, Any], str | None], bool | None] | None = None
    _subscribed: bool = field(default=False, init=False)
    _cost_total_usd: float = field(default=0.0, init=False)
    _cost_budget_warning_emitted: bool = field(default=False, init=False)
    _cost_budget_exceeded_emitted: bool = field(default=False, init=False)
    _cost_budget_enforcement_pending: bool = field(default=False, init=False)

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
            self._maybe_append_cost_budget_record(
                name=name,
                turn_id=self.current_turn_id(turn_id),
                data=data,
            )
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
        self._maybe_append_cost_budget_record(
            name=name,
            turn_id=resolved_turn_id,
            data=data,
        )

    def _maybe_append_cost_budget_record(
        self,
        *,
        name: str,
        turn_id: str | None,
        data: dict[str, Any] | None,
    ) -> None:
        if name not in _COST_RECORD_NAMES or not isinstance(data, dict):
            return
        limit_usd = finite_number(self.max_session_cost_usd)
        if limit_usd is None or limit_usd <= 0:
            return
        record_usd = finite_number(data.get("usd"))
        if record_usd is None:
            return

        self._cost_total_usd += record_usd
        budget = cost_budget_status(self._cost_total_usd, limit_usd)
        if budget["warning"] and not self._cost_budget_warning_emitted:
            self._append_cost_budget_alert(
                alert="warning",
                budget=budget,
                trigger_record_name=name,
                turn_id=turn_id,
            )
            self._cost_budget_warning_emitted = True
        if budget["exceeded"]:
            should_notify = (
                not self._cost_budget_exceeded_emitted or self._cost_budget_enforcement_pending
            )
            if not self._cost_budget_exceeded_emitted:
                alert_data = self._append_cost_budget_alert(
                    alert="exceeded",
                    budget=budget,
                    trigger_record_name=name,
                    turn_id=turn_id,
                )
                self._cost_budget_exceeded_emitted = True
            else:
                alert_data = self._cost_budget_alert_data(
                    alert="exceeded",
                    budget=budget,
                    trigger_record_name=name,
                )
            if should_notify and self.on_cost_budget_exceeded is not None:
                try:
                    accepted = self.on_cost_budget_exceeded(alert_data, turn_id)
                except Exception:
                    self._cost_budget_enforcement_pending = True
                    logger.exception("Cost budget enforcement callback failed")
                else:
                    self._cost_budget_enforcement_pending = accepted is False

    def _cost_budget_alert_data(
        self,
        *,
        alert: str,
        budget: dict[str, Any],
        trigger_record_name: str,
    ) -> dict[str, Any]:
        return {
            "alert": alert,
            "budget_status": budget["status"],
            "total_usd": self._cost_total_usd,
            "max_session_cost_usd": budget["max_session_cost_usd"],
            "warning_threshold_usd": budget["warning_threshold_usd"],
            "usage_fraction": budget["usage_fraction"],
            "remaining_usd": budget["remaining_usd"],
            "overage_usd": budget["overage_usd"],
            "trigger_record_name": trigger_record_name,
        }

    def _append_cost_budget_alert(
        self,
        *,
        alert: str,
        budget: dict[str, Any],
        trigger_record_name: str,
        turn_id: str | None,
    ) -> dict[str, Any]:
        data = self._cost_budget_alert_data(
            alert=alert,
            budget=budget,
            trigger_record_name=trigger_record_name,
        )
        if self.journal is None:
            return data
        self.journal.append(
            kind=JournalRecordKind.METRIC,
            name=f"cost_budget_{alert}",
            session_id=self.session_id,
            turn_id=turn_id,
            data=data,
            tags=frozenset({"cost_budget", alert}),
        )
        return data

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
                    data[attr] = _to_jsonable(val) if attr in _JSONABLE_ATTRS else val
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
