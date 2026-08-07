"""Session-owned journal sink.

Translates session events and explicit observability calls into execution
journal records.  The sink is intentionally small: Session still owns runtime
lifecycle and public debug surfaces, while this component owns journal writes
and event-bus subscription handlers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from easycat import _observability as observability
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AgentRequestStarted,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    CallAnswered,
    CallEnded,
    CallFailed,
    CallScreening,
    Error,
    Event,
    EventBus,
    EventHandler,
    EventSubscription,
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
from easycat.runtime.artifacts import (
    ArtifactClass,
    ArtifactStore,
    ArtifactWriteReceipt,
    FilesystemArtifactStore,
)
from easycat.runtime.journal import ExecutionJournal, append_journal_record_async
from easycat.runtime.record_contracts import validate_builtin_record
from easycat.runtime.records import ErrorInfo, JournalRecordKind
from easycat.validation.redaction import RedactionPolicy, redact_value

logger = logging.getLogger(__name__)
_JOURNAL_ATTRS = (
    "text",
    "track",
    "result",
    "action",
    "executor",
    "provider",
    "tool_name",
    "call_id",
    "attempt",
    "call_sid",
    "answered_by",
    "platform",
    "sip_code",
    "duration_s",
    "disposition",
    "number",
    "delta",
    "listener_id",
    "queue_size",
    "dropped_frames",
    "mark_name",
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
_NONEMPTY_ATTRS = frozenset({"provider"})
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


@dataclass(frozen=True, slots=True)
class _EventRecordSpec:
    event_type: type[Event]
    kind: JournalRecordKind
    name: str


_SIMPLE_EVENT_RECORDS = (
    _EventRecordSpec(TurnStarted, JournalRecordKind.EVENT, "turn_started"),
    _EventRecordSpec(TurnEnded, JournalRecordKind.EVENT, "turn_ended"),
    _EventRecordSpec(VADStartSpeaking, JournalRecordKind.EVENT, "vad_start_speaking"),
    _EventRecordSpec(VADStopSpeaking, JournalRecordKind.EVENT, "vad_stop_speaking"),
    _EventRecordSpec(STTPartial, JournalRecordKind.EVENT, "stt_partial"),
    _EventRecordSpec(STTFinal, JournalRecordKind.EVENT, "stt_final"),
    _EventRecordSpec(AgentRequestStarted, JournalRecordKind.EVENT, "agent_request_started"),
    _EventRecordSpec(AgentDelta, JournalRecordKind.EVENT, "agent_delta"),
    _EventRecordSpec(AgentFinal, JournalRecordKind.EVENT, "agent_final"),
    _EventRecordSpec(CallAnswered, JournalRecordKind.EVENT, "call_answered"),
    _EventRecordSpec(CallEnded, JournalRecordKind.EVENT, "call_ended"),
    _EventRecordSpec(CallFailed, JournalRecordKind.EVENT, "call_failed"),
    _EventRecordSpec(CallScreening, JournalRecordKind.EVENT, "call_screening"),
    _EventRecordSpec(BotStartedSpeaking, JournalRecordKind.EVENT, "bot_started_speaking"),
    _EventRecordSpec(BotStoppedSpeaking, JournalRecordKind.EVENT, "bot_stopped_speaking"),
    _EventRecordSpec(Error, JournalRecordKind.EVENT, "error"),
    _EventRecordSpec(ToolCallStarted, JournalRecordKind.EVENT, "tool_call_started"),
    _EventRecordSpec(ToolCallDelta, JournalRecordKind.EVENT, "tool_call_delta"),
    _EventRecordSpec(ToolCallResult, JournalRecordKind.EVENT, "tool_call_result"),
    _EventRecordSpec(SessionActionRequested, JournalRecordKind.EVENT, "session_action_requested"),
    _EventRecordSpec(SessionActionStarted, JournalRecordKind.EVENT, "session_action_started"),
    _EventRecordSpec(SessionActionCompleted, JournalRecordKind.EVENT, "session_action_completed"),
    _EventRecordSpec(SessionActionFailed, JournalRecordKind.EVENT, "session_action_failed"),
    # Retry and listener lifecycle records make reconnect/audit timelines
    # visible in exported bundles without bespoke handlers.
    _EventRecordSpec(ReconnectAttempt, JournalRecordKind.EVENT, "ws_reconnect_attempt"),
    _EventRecordSpec(ReconnectSuccess, JournalRecordKind.EVENT, "ws_reconnect_success"),
    _EventRecordSpec(ReconnectFailure, JournalRecordKind.EVENT, "ws_reconnect_failure"),
    _EventRecordSpec(
        SupervisorListenerAttached,
        JournalRecordKind.EVENT,
        "supervisor_listener_attached",
    ),
    _EventRecordSpec(
        SupervisorListenerDetached,
        JournalRecordKind.EVENT,
        "supervisor_listener_detached",
    ),
    _EventRecordSpec(PlaybackMarkAck, JournalRecordKind.EVENT, "playback_mark_ack"),
)

_ERROR_NONEMPTY_ATTRS = (
    ("provider", "provider"),
    ("code", "code"),
    ("record_key", "record_ref"),
)
_ERROR_OPTIONAL_ATTRS = (
    ("elapsed_ms", "elapsed_ms"),
    ("sequence", "sequence"),
)


@dataclass(frozen=True, slots=True)
class _JournalEventProjection:
    data: dict[str, Any] | None
    error: ErrorInfo | None


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
            except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
                return value
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            data = dataclasses.asdict(value)
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            return value
        action_type = getattr(value, "type", None)
        if action_type is not None:
            data["type"] = getattr(action_type, "value", action_type)
        return _coerce_json_native(data)
    return value


def _redact_session_action_data(
    value: Any,
    key: str | None = None,
    *,
    policy: RedactionPolicy = "secrets",
) -> Any:
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
            str(item_key): _redact_session_action_data(
                item_value,
                str(item_key),
                policy=policy,
            )
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        return [_redact_session_action_data(item, key, policy=policy) for item in value]
    return redact_value(value, key, policy=policy)


def _journal_attr_value(attr: str, value: Any, *, policy: RedactionPolicy) -> Any:
    jsonable = _to_jsonable(value) if attr in _JSONABLE_ATTRS else value
    if attr in {"action", "result"}:
        return _redact_session_action_data(jsonable, policy=policy)
    return jsonable


def _event_attributes(event: Event, *, policy: RedactionPolicy) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for attr in _JOURNAL_ATTRS:
        value = getattr(event, attr, None)
        if value is not None and (attr not in _NONEMPTY_ATTRS or value):
            data[attr] = _journal_attr_value(attr, value, policy=policy)
    return data


def _exception_attributes(event: Event) -> dict[str, Any]:
    data: dict[str, Any] = {}
    stage = getattr(event, "stage", None)
    if stage is not None and hasattr(stage, "value"):
        data["stage"] = stage.value
    for source, target in _ERROR_NONEMPTY_ATTRS:
        value = getattr(event, source, None)
        if value:
            data[target] = value
    for source, target in _ERROR_OPTIONAL_ATTRS:
        value = getattr(event, source, None)
        if value is not None:
            data[target] = value
    return data


def _project_journal_event(
    event: Event,
    *,
    policy: RedactionPolicy = "secrets",
) -> _JournalEventProjection:
    data = _event_attributes(event, policy=policy)
    exception = getattr(event, "exception", None)
    if exception is None:
        return _JournalEventProjection(data=data or None, error=None)
    data.update(_exception_attributes(event))
    return _JournalEventProjection(
        data=data or None,
        error=ErrorInfo.from_exception(exception),
    )


class TurnIdResolver(Protocol):
    """Resolve the turn id to record, defaulting to the active turn.

    Matches the ``current_turn_id`` member of the runtime layer's
    structural ``JournalSink`` protocols (callable with an optional
    ``turn_id`` keyword), so a ``SessionJournalSink`` instance satisfies
    them without adapters.
    """

    def __call__(self, turn_id: str | None = None) -> str | None: ...


def _artifact_store_writes_block(store: ArtifactStore | None) -> bool:
    if store is None:
        return False
    declared = getattr(store, "writes_block", None)
    return bool(declared) or (declared is None and isinstance(store, FilesystemArtifactStore))


@dataclass(frozen=True, slots=True)
class _StoredArtifact:
    ref: str | None
    cleanup_token: str | None = None


def _store_artifact_for_record(
    store: ArtifactStore,
    payload: bytes,
    artifact_class: ArtifactClass,
) -> _StoredArtifact:
    put_with_token = getattr(store, "put_with_cleanup_token", None)
    delete_with_token = getattr(store, "delete_if_cleanup_token", None)
    if callable(put_with_token) and callable(delete_with_token):
        receipt = put_with_token(payload, artifact_class=artifact_class)
        if not isinstance(receipt, ArtifactWriteReceipt):
            raise TypeError("put_with_cleanup_token() must return ArtifactWriteReceipt")
        cleanup_token = receipt.cleanup_token if receipt.ref and receipt.created else None
        return _StoredArtifact(receipt.ref or None, cleanup_token)
    ref = store.put(payload, artifact_class=artifact_class)
    return _StoredArtifact(ref or None)


def _cleanup_rejected_artifacts(
    store: ArtifactStore,
    writes: tuple[_StoredArtifact, ...],
) -> None:
    delete_with_token = getattr(store, "delete_if_cleanup_token", None)
    if not callable(delete_with_token):
        return
    seen: set[tuple[str, str]] = set()
    for write in writes:
        if write.ref is None or write.cleanup_token is None:
            continue
        cleanup = (write.ref, write.cleanup_token)
        if cleanup in seen:
            continue
        seen.add(cleanup)
        try:
            delete_with_token(*cleanup)
        except Exception:
            logger.warning(
                "Artifact cleanup failed after journal append rejection for ref=%s",
                write.ref,
                exc_info=True,
            )


def _shared_artifact_class(
    input_class: ArtifactClass,
    output_class: ArtifactClass,
) -> ArtifactClass:
    """Keep the stronger retention class when one payload serves both refs."""
    if "replay_critical" in (input_class, output_class):
        return "replay_critical"
    return "debug_verbose"


async def _await_owned_write(operation: asyncio.Task[None]) -> None:
    try:
        await asyncio.shield(operation)
    except asyncio.CancelledError:
        # A synchronous artifact write cannot be stopped once its worker
        # starts. Keep ownership of the entire write-and-reference unit
        # until its journal row commits, so cancellation cannot leave a
        # detached write behind or expose an unreferenced artifact.
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
        try:
            operation.result()
        except BaseException:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
            # Cancellation remains caller-visible, but retrieving the result
            # prevents a detached operation exception warning.
            pass
        raise


@dataclass(slots=True)
class SessionJournalSink:
    """Write session activity to an execution journal."""

    event_bus: EventBus
    journal: ExecutionJournal | None
    artifact_store: ArtifactStore | None
    session_id: str
    current_turn_id: TurnIdResolver
    redaction: RedactionPolicy = "secrets"
    subscribe_event: Callable[[type[Event], EventHandler], EventSubscription] | None = None
    _subscribed: bool = field(default=False, init=False)

    def subscribe(self) -> None:
        """Subscribe event bus handlers that write session events to the journal."""
        if self.journal is None or self._subscribed:
            return
        self._subscribed = True

        for spec in _SIMPLE_EVENT_RECORDS:
            self._subscribe(spec.event_type, self._make_event_handler(spec.kind, spec.name))

        self._subscribe(TTSAudio, self._handle_tts_audio)
        self._subscribe(TTSMarkers, self._handle_tts_markers)
        self._subscribe(
            Interruption,
            self._handle_interruption(JournalRecordKind.CONTROL),
        )
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
        if (
            self.artifact_store is None
            or not payload
            or (self.journal is not None and self.journal.degraded)
        ):
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
        tags: frozenset[str] = frozenset(),
        inherit_turn_id: bool = True,
    ) -> int | None:
        if self.journal is None:
            return None
        validate_builtin_record(name=name, kind=kind, data=data)
        journal = self.journal
        artifact_store = self.artifact_store
        writes: list[_StoredArtifact] = []
        try:
            shared_payload = input_bytes is not None and output_bytes == input_bytes
            effective_input_class = (
                _shared_artifact_class(input_artifact_class, output_artifact_class)
                if shared_payload
                else input_artifact_class
            )
            input_write = (
                _store_artifact_for_record(
                    artifact_store,
                    input_bytes,
                    effective_input_class,
                )
                if artifact_store is not None and input_bytes and not journal.degraded
                else _StoredArtifact(None)
            )
            writes.append(input_write)
            output_write = (
                input_write
                if shared_payload
                else (
                    _store_artifact_for_record(
                        artifact_store,
                        output_bytes,
                        output_artifact_class,
                    )
                    if artifact_store is not None and output_bytes and not journal.degraded
                    else _StoredArtifact(None)
                )
            )
            writes.append(output_write)
            resolved_turn_id = self.current_turn_id(turn_id) if inherit_turn_id else turn_id
            sequence = journal.append(
                kind=kind,
                name=name,
                session_id=self.session_id,
                turn_id=resolved_turn_id,
                data=data,
                tags=tags,
                input_ref=input_write.ref,
                output_ref=output_write.ref,
            )
        except BaseException:
            if artifact_store is not None:
                _cleanup_rejected_artifacts(artifact_store, tuple(writes))
            raise
        if sequence < 0 and artifact_store is not None:
            _cleanup_rejected_artifacts(artifact_store, tuple(writes))
        return sequence

    async def append_record_async(  # noqa: C901 - explicit artifact/journal cleanup stages
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
        tags: frozenset[str] = frozenset(),
        inherit_turn_id: bool = True,
    ) -> None:
        """Async event-bus write path for persistent journal/store backends."""
        journal = self.journal
        if journal is None:
            return
        validate_builtin_record(name=name, kind=kind, data=data)
        artifact_store = self.artifact_store
        owns_artifact_write = (
            artifact_store is not None
            and not journal.degraded
            and (input_bytes is not None or output_bytes is not None)
        )
        blocking_artifact_write = owns_artifact_write and _artifact_store_writes_block(
            artifact_store
        )

        async def _write_artifacts_and_record() -> None:
            async def _store(
                payload: bytes | None,
                artifact_class: ArtifactClass,
            ) -> _StoredArtifact:
                if not payload or artifact_store is None or journal.degraded:
                    return _StoredArtifact(None)
                if blocking_artifact_write:
                    return await asyncio.to_thread(
                        _store_artifact_for_record,
                        artifact_store,
                        payload,
                        artifact_class,
                    )
                return _store_artifact_for_record(artifact_store, payload, artifact_class)

            writes: list[_StoredArtifact] = []
            try:
                shared_payload = input_bytes is not None and output_bytes == input_bytes
                effective_input_class = (
                    _shared_artifact_class(input_artifact_class, output_artifact_class)
                    if shared_payload
                    else input_artifact_class
                )
                input_write = await _store(input_bytes, effective_input_class)
                writes.append(input_write)
                output_write = (
                    input_write
                    if shared_payload
                    else await _store(output_bytes, output_artifact_class)
                )
                writes.append(output_write)
                resolved_turn_id = self.current_turn_id(turn_id) if inherit_turn_id else turn_id
                sequence = await append_journal_record_async(
                    journal,
                    kind=kind,
                    name=name,
                    session_id=self.session_id,
                    turn_id=resolved_turn_id,
                    data=data,
                    tags=tags,
                    input_ref=input_write.ref,
                    output_ref=output_write.ref,
                )
            except BaseException:
                if artifact_store is not None:
                    if blocking_artifact_write:
                        await asyncio.to_thread(
                            _cleanup_rejected_artifacts,
                            artifact_store,
                            tuple(writes),
                        )
                    else:
                        _cleanup_rejected_artifacts(artifact_store, tuple(writes))
                raise
            if sequence < 0 and artifact_store is not None:
                if blocking_artifact_write:
                    await asyncio.to_thread(
                        _cleanup_rejected_artifacts,
                        artifact_store,
                        tuple(writes),
                    )
                else:
                    _cleanup_rejected_artifacts(artifact_store, tuple(writes))

        if not owns_artifact_write:
            await _write_artifacts_and_record()
            return

        operation = asyncio.create_task(_write_artifacts_and_record())
        await _await_owned_write(operation)

    def _subscribe(self, event_type: type[Event], handler: EventHandler) -> None:
        subscribe = self.subscribe_event or self.event_bus.subscribe
        subscribe(event_type, handler)

    def _make_event_handler(
        self,
        kind: JournalRecordKind,
        name: str,
    ) -> Callable[[Event], Coroutine[Any, Any, None]]:
        async def _handler(event: Event) -> None:
            journal = self.journal
            if journal is None:
                return
            projection = _project_journal_event(event, policy=self.redaction)
            validate_builtin_record(name=name, kind=kind, data=projection.data)
            await append_journal_record_async(
                journal,
                kind=kind,
                name=name,
                session_id=getattr(event, "session_id", None) or self.session_id,
                turn_id=getattr(event, "turn_id", None),
                data=projection.data,
                error=projection.error,
            )

        return _handler

    def _handle_interruption(self, kind: JournalRecordKind) -> EventHandler:
        """Journal the interruption and bump the ``easycat.interruption`` counter.

        The counter carries only the low-cardinality ``easycat.surface`` (``vad``)
        attribute — never ``turn_id`` / ``transcript``, which the observability
        sanitizer rejects as forbidden keys.  The companion cutoff-latency
        histogram is not emitted here: the sink sees the barge-in event before
        the bot has stopped, so it cannot measure the cutoff delta. That
        histogram is emitted by ``Session.cancel_turn`` once playback has
        actually been cleared on the transport.
        """
        journal_handler = self._make_event_handler(kind, "interruption")

        async def _handler(event: Any) -> None:
            await journal_handler(event)
            observability.increment_counter(
                "easycat.interruption.total",
                attributes={"easycat.surface": "vad"},
            )

        return _handler

    async def _handle_tts_audio(self, event: TTSAudio) -> None:
        # TTSStage captures replay-critical audio bytes via ``tts_frame``.
        # The session-level ``tts_audio`` record stays metadata-only for
        # legacy observers.
        await self.append_record_async(
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

    async def _handle_tts_markers(self, event: TTSMarkers) -> None:
        await self.append_record_async(
            name="tts_markers",
            turn_id=event.turn_id,
            data={"markers": event.markers},
        )

    async def _handle_transport_degraded(self, event: TransportDegraded) -> None:
        # Fatal teardowns are control-plane events (mirrors ``interruption``);
        # recoverable single-frame drops stay on the EVENT timeline.
        await self.append_record_async(
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
