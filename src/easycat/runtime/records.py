"""Structured record types for the ExecutionJournal.

All record dataclasses are frozen.  Fields added after the initial release
must have defaults so older bundles remain loadable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Literal


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
    queue_ns: int = 0  # time waiting in a stage queue (filled by WS3 stages)


@dataclass(frozen=True)
class ErrorInfo:
    """Structured error snapshot attached to a journal record."""

    type: str = ""
    message: str = ""
    traceback: str | None = None
    notes: str | None = None  # additional context (e.g. retry count, affected stage)

    @staticmethod
    def from_exception(exc: BaseException, *, notes: str | None = None) -> ErrorInfo:
        """Capture an ``ErrorInfo`` from a live exception.

        Third-party frames (site-packages) are collapsed to a single
        ``...N frames...`` line to keep journal records readable.
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

        return ErrorInfo(
            type=type(exc).__qualname__,
            message=str(exc),
            traceback="".join(collapsed),
            notes=notes,
        )


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
    op_id: str = ""
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

    Emitted by agent bridges (WS2A) when control enters or leaves the
    framework (e.g., OpenAI Agents SDK, PydanticAI).
    """

    kind: JournalRecordKind = JournalRecordKind.FRAMEWORK_TRANSITION
    framework: str = ""  # e.g. "openai_agents", "pydantic_ai"
    direction: Literal["enter", "exit"] = "enter"
    bridge_latency_ms: float | None = None


@dataclass(frozen=True)
class ControlSignalRecord(JournalRecord):
    """Records a control signal propagating through the pipeline.

    These five signal_kind values are the complete set WS3 stages emit;
    additions require a WS1 RFC amendment.
    """

    kind: JournalRecordKind = JournalRecordKind.CONTROL
    signal_kind: Literal["interrupt", "cancel", "pause", "resume", "backpressure"] = "cancel"
    observed_stage: str = ""  # e.g. "stt", "tts", "agent"
    direction: Literal["upstream", "downstream"] = "downstream"
    signal_id: str = ""
    cause: str | None = None  # e.g. "barge_in", "timeout", "user_cancel"


# ── WS2A: Framework transition record subtypes ──────────────────


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

    WS2B wraps this in the four-step atomic write ordering.
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

    ``caused_by_signal_id`` links back to the WS1 ``ControlSignalRecord``
    that triggered this boundary (see WS1 T1.1 composition note).
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


# ── WS1: Recovery and health markers ────────────────────────────


@dataclass(frozen=True)
class RecoveredSessionMarker(JournalRecord):
    """Emitted at sequence=0 when a journal is opened from a prior unclean shutdown.

    The post-open journal still starts at sequence=1, so AC1.4's strict
    monotonicity holds for real records.
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
