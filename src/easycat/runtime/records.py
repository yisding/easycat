"""Structured record types for the ExecutionJournal.

All record dataclasses are frozen.  Fields added after the initial release
must have defaults so older bundles remain loadable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Literal

# ── Record-name constants ────────────────────────────────────────
# These names are consumed by replay, debugger, and bundle rollups as well as
# their live-path producers. Keep shared names here so those readers cannot
# silently drift from the persisted journal vocabulary.
STAGE_START_RECORD_NAME = "stage_start"
STAGE_COMPLETE_RECORD_NAME = "stage_complete"
STT_FINAL_RECORD_NAME = "stt_final"
AGENT_REQUEST_STARTED_RECORD_NAME = "agent_request_started"
AGENT_DELTA_RECORD_NAME = "agent_delta"
AGENT_FINAL_RECORD_NAME = "agent_final"
TTS_FRAME_RECORD_NAME = "tts_frame"
BOT_STARTED_SPEAKING_RECORD_NAME = "bot_started_speaking"
VAD_START_SPEAKING_RECORD_NAME = "vad_start_speaking"
BOT_STOPPED_SPEAKING_RECORD_NAME = "bot_stopped_speaking"
PLAYBACK_MARK_ACK_RECORD_NAME = "playback_mark_ack"
INTERRUPTION_RECORD_NAME = "interruption"
CONTROL_SIGNAL_RECORD_NAME = "control_signal"
CALL_ENDED_RECORD_NAME = "call_ended"

# Pinned built-in vocabulary. Public application records must use the
# ``app.`` namespace and may not collide with these runtime-owned names.
BUILTIN_JOURNAL_RECORD_NAMES = frozenset(
    {
        "aec_reference_frame",
        "agent_delta",
        "agent_final",
        "agent_request_started",
        "assistant_interruption_notified",
        "audio_queue_drop",
        "bot_started_speaking",
        "bot_stopped_speaking",
        "buffer_overflow",
        "call_answered",
        "call_ended",
        "call_failed",
        "call_screening",
        "cancellation_boundary",
        "control_signal",
        "control_signal_cause",
        "error",
        "framework_error",
        "framework_handoff",
        "interruption",
        "interruption_apply_failed",
        "interruption_note",
        "journal_degraded",
        "markdown_stripped",
        "pipeline_heartbeat",
        "playback_mark_ack",
        "provider_versions",
        "recovered_session",
        "replace_last_assistant_text",
        "session_action_completed",
        "session_action_failed",
        "session_action_requested",
        "session_action_started",
        "stage_complete",
        "stage_error",
        "stage_start",
        "state_committed",
        "state_snapshot",
        "stt_final",
        "stt_partial",
        "stt_segment_commit_requested",
        "stt_segment_commit_result",
        "stt_segment_final",
        "supervisor_listener_attached",
        "supervisor_listener_detached",
        "task_cancelled",
        "task_completed",
        "task_raised",
        "task_scheduled",
        "text_turn_latency_ms",
        "tool_call_delta",
        "tool_call_result",
        "tool_call_started",
        "tool_phase_changed",
        "transport_degraded",
        "tts_audio",
        "tts_frame",
        "tts_markers",
        "tts_payload_prepared",
        "turn_ended",
        "turn_started",
        "turn_state_changed",
        "turn_total_latency_ms",
        "unit_entered",
        "unit_exited",
        "vad_start_speaking",
        "vad_stop_speaking",
        "warmup_completed",
        "warmup_failed",
        "ws_reconnect_attempt",
        "ws_reconnect_failure",
        "ws_reconnect_success",
    }
)

# The AEC far-end reference frame is the bot playback fed into the echo
# canceller. Capturing it lets the debugger align mic-in / reference /
# post-AEC into one view and compute ERLE.
AEC_REFERENCE_FRAME_NAME = "aec_reference_frame"


class JournalRecordKind(enum.Enum):
    """Discriminator for journal record filtering."""

    EVENT = "event"
    SPAN_START = "span_start"
    SPAN_END = "span_end"
    METRIC = "metric"
    CONTROL = "control"  # control signals (interrupt, cancel, etc.)
    FRAMEWORK_TRANSITION = "framework_transition"  # agent bridge boundaries
    DEGRADED = "degraded"  # journal health
    RECOVERY = "recovery"  # crash recovery markers


@dataclass(frozen=True)
class TimingInfo:
    """Dual-clock timestamp captured at record creation."""

    wall_ns: int = 0  # time.time_ns()
    mono_ns: int = 0  # time.monotonic_ns()
    cpu_ns: int = 0  # time.process_time_ns() — CPU time spent in process


@dataclass(frozen=True)
class ErrorInfo:
    """Structured error snapshot attached to a journal record."""

    type: str = ""
    message: str = ""
    traceback: str | None = None
    notes: str | None = None  # additional context (e.g. retry count, affected stage)
    children: tuple[ErrorInfo, ...] = ()

    @staticmethod
    def from_exception(exc: BaseException, *, notes: str | None = None) -> ErrorInfo:
        """Capture an ``ErrorInfo`` from a live exception.

        Third-party frames (site-packages) are collapsed to a single
        ``...N frames...`` line to keep journal records readable.
        PEP 678 notes attached to the exception are preserved in the
        structured ``notes`` field for journal filters and bundle export.
        """
        import traceback as tb_mod

        raw_lines = tb_mod.format_exception(type(exc), exc, exc.__traceback__)
        collapsed: list[str] = []
        skip_run = 0
        for line in "".join(raw_lines).splitlines(keepends=True):
            if "site-packages" in line:
                skip_run += 1
            else:
                if skip_run:
                    collapsed.append(f"  ...{skip_run} third-party frame(s)...\n")
                    skip_run = 0
                collapsed.append(line)
        if skip_run:
            collapsed.append(f"  ...{skip_run} third-party frame(s)...\n")

        combined_notes = _combine_error_notes(notes, exc)

        return ErrorInfo(
            type=type(exc).__qualname__,
            message=str(exc),
            traceback="".join(collapsed),
            notes=combined_notes,
            children=_exception_children(exc),
        )


def _combine_error_notes(notes: str | None, exc: BaseException) -> str | None:
    combined: list[str] = []
    if notes:
        combined.append(notes)
    combined.extend(_exception_notes(exc))
    return "\n".join(combined) or None


def _exception_notes(exc: BaseException) -> list[str]:
    notes: list[str] = []
    exception_notes = getattr(exc, "__notes__", None)
    if isinstance(exception_notes, list):
        notes.extend(str(note) for note in exception_notes if note)
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            notes.extend(_exception_notes(child))
    return notes


def _exception_children(exc: BaseException) -> tuple[ErrorInfo, ...]:
    if not isinstance(exc, BaseExceptionGroup):
        return ()
    return tuple(ErrorInfo.from_exception(child) for child in exc.exceptions)


@dataclass(frozen=True)
class JournalRecord:
    """Base record appended to the ExecutionJournal.

    ``sequence`` and ``session_id`` are always present and have no defaults.
    Every other field carries a default so record subclasses remain
    forward-compatible when new fields are introduced.
    """

    sequence: int
    session_id: str
    kind: JournalRecordKind = JournalRecordKind.EVENT
    name: str = ""
    timing: TimingInfo = field(default_factory=TimingInfo)
    turn_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error: ErrorInfo | None = None
    input_ref: str | None = None
    output_ref: str | None = None
    tags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FrameworkTransitionRecord(JournalRecord):
    """Records a boundary crossing between EasyCat and an agent framework.

    Emitted by agent bridges when control enters or leaves the framework
    (e.g., OpenAI Agents SDK, PydanticAI).
    """

    kind: JournalRecordKind = JournalRecordKind.FRAMEWORK_TRANSITION
    framework: str = ""  # e.g. "openai_agents", "pydantic_ai"
    direction: Literal["enter", "exit"] = "enter"
    bridge_latency_ms: float | None = None


@dataclass(frozen=True)
class ControlSignalRecord(JournalRecord):
    """Records a control signal propagating through the pipeline.

    These five ``signal_kind`` values are the complete set emitted by
    pipeline stages. Additions are journal schema changes and must remain
    backward-compatible with older bundles.
    """

    kind: JournalRecordKind = JournalRecordKind.CONTROL
    signal_kind: Literal["interrupt", "cancel", "pause", "resume", "backpressure"] = "cancel"
    observed_stage: str = ""  # e.g. "stt", "tts", "agent"
    direction: Literal["upstream", "downstream"] = "downstream"
    signal_id: str = ""
    cause: str | None = None  # e.g. "barge_in", "timeout", "user_cancel"


# ── Framework transition record subtypes ────────────────────────


@dataclass(frozen=True)
class FrameworkUnitEntered(FrameworkTransitionRecord):
    """A bridge execution unit (agent, tool, node) was entered."""

    direction: Literal["enter", "exit"] = "enter"
    unit_id: str = ""
    unit_kind: str = ""  # matches UnitKind values
    display_name: str = ""
    parent_unit_id: str | None = None
    committable: bool = False


@dataclass(frozen=True)
class FrameworkUnitExited(FrameworkTransitionRecord):
    """A bridge execution unit was exited."""

    direction: Literal["enter", "exit"] = "exit"
    unit_id: str = ""
    unit_kind: str = ""
    display_name: str = ""
    parent_unit_id: str | None = None
    committable: bool = False
    exit_reason: str | None = None


@dataclass(frozen=True)
class FrameworkStateCommitted(FrameworkTransitionRecord):
    """Emitted *before* mutating framework state in ``apply_interruption``.

    Part of the bridge interruption protocol's four-step atomic write ordering.
    """

    mutation_kind: str = ""  # e.g. "interrupt_truncate", "interrupt_drain"
    pre_state_ref: str | None = None
    post_state_ref: str | None = None


@dataclass(frozen=True)
class FrameworkHandoff(FrameworkTransitionRecord):
    """Records a handoff between two execution units.

    Always part of an atomic triple: ``FrameworkUnitExited`` →
    ``FrameworkHandoff`` → ``FrameworkUnitEntered``.
    """

    from_unit: str = ""
    to_unit: str = ""
    transition_kind: str = ""  # e.g. "agent_handoff", "graph_transition"
    handoff_reason: str | None = None


@dataclass(frozen=True)
class FrameworkToolPhaseChanged(FrameworkTransitionRecord):
    """Records a tool call phase change (start, delta, result, error)."""

    phase: str = ""  # "start", "delta", "result", "error"
    tool_name: str = ""
    tool_call_id: str = ""
    args_ref: str | None = None
    result_ref: str | None = None


@dataclass(frozen=True)
class FrameworkCancellationBoundaryReached(FrameworkTransitionRecord):
    """Records that a cancellation boundary was reached.

    ``caused_by_signal_id`` links back to the ``ControlSignalRecord`` that
    triggered this boundary.
    """

    cancellation_mode: str = ""
    boundary_reason: str | None = None
    caused_by_signal_id: str | None = None


@dataclass(frozen=True)
class InterruptionApplyFailed(FrameworkTransitionRecord):
    """Emitted when ``apply_interruption`` fails after state was committed.

    Paired with a preceding ``FrameworkStateCommitted`` record.
    """

    mutation_kind: str = ""
    pre_state_ref: str | None = None
    post_state_ref: str | None = None
    failure_error: ErrorInfo | None = None


# ── Recovery and health markers ─────────────────────────────────


@dataclass(frozen=True)
class RecoveredSessionMarker(JournalRecord):
    """Emitted at sequence=0 when a journal is opened from a prior unclean shutdown.

    The post-open journal still starts at sequence=1, so strict monotonicity
    holds for real records.

    On SQLite persistence the typed fields below are mirrored into the base
    ``data`` dict (the journal table has no dedicated columns for them) and
    rehydrated by ``_SqlJournalBase._row_to_record`` on read.
    """

    kind: JournalRecordKind = JournalRecordKind.RECOVERY
    name: str = "recovered_session"
    recovered_record_count: int = 0
    original_session_id: str = ""


@dataclass(frozen=True)
class BufferOverflow(JournalRecord):
    """Sentinel emitted when the in-memory ring buffer drops records."""

    kind: JournalRecordKind = JournalRecordKind.CONTROL
    name: str = "buffer_overflow"


@dataclass(frozen=True)
class JournalDegraded(JournalRecord):
    """Sentinel emitted (once per session) when a backend write fails."""

    kind: JournalRecordKind = JournalRecordKind.DEGRADED
    name: str = "journal_degraded"
