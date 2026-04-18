"""Stage protocol and shared types for the EasyCat pipeline.

Every pipeline stage (STT, TTS, VAD, Agent, etc.) implements the
``Stage`` protocol.  Stages are thin wrappers around existing providers
that add journal recording and a uniform ``execute`` / ``snapshot_state``
surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.session._turn_context import TurnContext

if TYPE_CHECKING:
    # Annotation-only import.  At runtime ``ReplaySpec`` resolves via
    # ``__getattr__`` below so we stay clear of the import cycle with
    # ``runtime.replay`` (which imports NONDETERMINISTIC_FIELDS from here).
    from easycat.runtime.replay import ReplaySpec

# ── Control signals ──────────────────────────────────────────────


@dataclass(frozen=True)
class ControlSignal:
    """Base for upstream control signals."""

    signal_id: str


@dataclass(frozen=True)
class InterruptSignal(ControlSignal):
    """User barged in — cancel downstream and truncate."""


@dataclass(frozen=True)
class CancelSignal(ControlSignal):
    """Cancel the current operation."""


@dataclass(frozen=True)
class PauseSignal(ControlSignal):
    """Pause the stage (e.g. during hold)."""


@dataclass(frozen=True)
class ResumeSignal(ControlSignal):
    """Resume a previously paused stage."""


@dataclass(frozen=True)
class BackpressureSignal(ControlSignal):
    """Downstream is overwhelmed — slow down."""


# ── Stage state ──────────────────────────────────────────────────


@dataclass(frozen=True)
class StageStateSnapshot:
    """Serialisable snapshot of a stage's internal state."""

    stage_name: str
    fields: dict[str, Any] = field(default_factory=dict)
    state_ref: str | None = None


# ── Stage protocol ───────────────────────────────────────────────


@runtime_checkable
class Stage(Protocol):
    """Uniform interface for every pipeline stage."""

    name: str

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        """Run the stage on *input* and return the result."""
        ...

    def snapshot_state(self) -> StageStateSnapshot:
        """Return a frozen snapshot of current internal state."""
        ...

    def replay(self, spec: ReplaySpec) -> Any:
        """Replay from journal/artifacts.  Stub until WS4."""
        ...

    async def handle_upstream(self, signal: ControlSignal, ctx: RunContext | None = None) -> None:
        """React to an upstream control signal.

        When *ctx* is supplied, the stage journals a ``ControlSignalRecord``
        so the signal path is visible alongside normal stage events.
        """
        ...


# ── Non-deterministic fields ─────────────────────────────────────


# Extended in WS4 runtime/replay.py as REPLAY_IGNORE_FIELDS


# ── Lazy re-export: ReplaySpec lives in runtime.replay ──────────
# ``runtime.replay`` imports :data:`NONDETERMINISTIC_FIELDS` from this
# module, so a top-level ``from easycat.runtime.replay import
# ReplaySpec`` would deadlock during initial module load.  We defer
# the lookup to attribute access time instead.


def __getattr__(name: str) -> Any:  # pragma: no cover - trivial forwarder
    if name == "ReplaySpec":
        from easycat.runtime.replay import ReplaySpec

        return ReplaySpec
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── Shared capture helpers ───────────────────────────────────────


def put_artifact(
    ctx: RunContext,
    payload: bytes | None,
    *,
    artifact_class: Literal["replay_critical", "debug_verbose"] = "replay_critical",
) -> str | None:
    """Store ``payload`` in ``ctx.artifact_store`` and return its ref.

    Returns ``None`` when there is no store, the payload is empty, or
    the store silently rejects the write (over size cap).  Callers
    should treat the ref as optional and fall back to inline ``data``.
    """
    if ctx.artifact_store is None or not payload:
        return None
    ref = ctx.artifact_store.put(payload, artifact_class=artifact_class)
    return ref or None


def journal_append_event(
    ctx: RunContext,
    *,
    stage: str,
    name: str,
    turn_id: str | None = None,
    kind: JournalRecordKind = JournalRecordKind.EVENT,
    state_before: StageStateSnapshot | None = None,
    state_after: StageStateSnapshot | None = None,
    error: str | None = None,
    input_ref: str | None = None,
    output_ref: str | None = None,
    data_extra: dict[str, Any] | None = None,
) -> None:
    """Append a stage-scoped journal record.

    Centralises the boilerplate every stage used to duplicate: stamping
    ``data["stage"]`` for replay-runner filtering, stringifying state
    snapshots for JSON serialization, and passing artifact refs through
    to the journal's first-class ``input_ref`` / ``output_ref`` fields
    instead of burying them in ``data`` as strings.
    """
    if ctx.journal is None:
        return
    payload: dict[str, Any] = {"stage": stage}
    if state_before is not None:
        payload["state_before"] = str(state_before)
    if state_after is not None:
        payload["state_after"] = str(state_after)
    if error is not None:
        payload["error"] = error
    if data_extra:
        payload.update(data_extra)
    ctx.journal.append(
        kind=kind,
        name=name,
        session_id=ctx.session_id,
        turn_id=turn_id,
        data=payload,
        input_ref=input_ref,
        output_ref=output_ref,
    )


def audio_format_fields(audio: Any) -> dict[str, Any]:
    """Best-effort extraction of PCM format fields from an AudioChunk-like.

    Returns an empty dict for inputs without a ``format`` attribute so
    callers can unconditionally splice it into ``data_extra``.
    """
    fmt = getattr(audio, "format", None)
    if fmt is None:
        return {}
    return {
        "sample_rate": getattr(fmt, "sample_rate", None),
        "channels": getattr(fmt, "channels", None),
        "sample_width": getattr(fmt, "sample_width", None),
        "encoding": getattr(fmt, "encoding", None),
    }


_SIGNAL_KIND_BY_CLASS: dict[str, str] = {
    "InterruptSignal": "interrupt",
    "CancelSignal": "cancel",
    "PauseSignal": "pause",
    "ResumeSignal": "resume",
    "BackpressureSignal": "backpressure",
}


def journal_append_control_signal(
    ctx: RunContext,
    *,
    stage: str,
    signal: ControlSignal,
    turn_id: str | None = None,
    direction: Literal["upstream", "downstream"] = "upstream",
    cause: str | None = None,
) -> None:
    """Append a ``ControlSignalRecord`` describing one stage observing a signal.

    Emits ``kind=JournalRecordKind.CONTROL`` with the signal's class
    mapped into the canonical ``signal_kind`` enum and the observing
    stage stamped into ``data["observed_stage"]`` so the replay runner's
    stage filter keeps working.  The ``signal_id`` is preserved so
    upstream fan-out across stages can be correlated.
    """
    if ctx.journal is None:
        return
    signal_cls = type(signal).__name__
    signal_kind = _SIGNAL_KIND_BY_CLASS.get(signal_cls, signal_cls.lower().replace("signal", ""))
    ctx.journal.append(
        kind=JournalRecordKind.CONTROL,
        name="control_signal",
        session_id=ctx.session_id,
        turn_id=turn_id,
        data={
            "stage": stage,
            "observed_stage": stage,
            "signal_kind": signal_kind,
            "signal_id": getattr(signal, "signal_id", ""),
            "direction": direction,
            "cause": cause,
        },
    )
