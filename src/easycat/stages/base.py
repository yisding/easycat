"""Stage protocol and shared types for the EasyCat pipeline.

Every pipeline stage (STT, TTS, VAD, Agent, etc.) implements the
``Stage`` protocol.  Stages are thin wrappers around existing providers
that add journal recording and a uniform ``execute`` / ``snapshot_state``
surface.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from easycat import _observability as observability
from easycat._turn_context import TurnContext
from easycat.runtime.context import RunContext
from easycat.runtime.nondeterministic import NONDETERMINISTIC_FIELDS  # noqa: F401  (re-export)
from easycat.runtime.records import JournalRecordKind

if TYPE_CHECKING:
    # Annotation-only imports.  ``ReplaySpec`` and ``ReplayCassette`` appear
    # only in ``Stage.replay``'s signature, which stays a string thanks to
    # ``from __future__ import annotations`` and is never evaluated at
    # runtime.  Importing them under ``TYPE_CHECKING`` keeps module load
    # order independent of ``runtime.replay`` (which imports from here).
    from easycat.runtime.replay import ReplayCassette, ReplaySpec


# ── Control signals ──────────────────────────────────────────────


@dataclass(frozen=True)
class ControlSignal:
    """Base for upstream control signals.

    A control signal is an *observation* that is fanned out through every
    stage's :meth:`Stage.handle_upstream` so the signal path shows up in
    the journal.  Receiving a signal does not, by itself, change pipeline
    behaviour: the actual cancel/truncate/pause work is owned by the
    ``CancelOrchestrator`` and the turn runner, not by the stages that
    observe the signal.  The verbs in the subclasses below describe the
    intent the orchestrator acts on, not work that ``handle_upstream``
    performs.
    """

    signal_id: str


@dataclass(frozen=True)
class InterruptSignal(ControlSignal):
    """User barged in.

    Intent: the orchestrator/turn runner cancels downstream work and
    truncates playback.  Stages only observe and journal this signal.
    """


@dataclass(frozen=True)
class CancelSignal(ControlSignal):
    """Cancel the current operation.

    Intent acted on by the orchestrator/turn runner; stages only observe
    and journal this signal.
    """


@dataclass(frozen=True)
class PauseSignal(ControlSignal):
    """Pause the pipeline (e.g. during hold).

    Intent acted on by the orchestrator; stages only observe and journal
    this signal.
    """


@dataclass(frozen=True)
class ResumeSignal(ControlSignal):
    """Resume a previously paused pipeline.

    Intent acted on by the orchestrator; stages only observe and journal
    this signal.
    """


@dataclass(frozen=True)
class BackpressureSignal(ControlSignal):
    """Downstream is overwhelmed — slow down.

    Intent acted on by the orchestrator; stages only observe and journal
    this signal.
    """


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

    def replay(self, spec: ReplaySpec, cassette: ReplayCassette | None = None) -> Any:
        """Replay this stage's output from journal/artifacts.

        ``spec`` selects the replay fidelity and carries any caller
        overrides; ``cassette`` (when supplied) is the per-stage view of
        the recorded journal/artifacts that the stage reads its captured
        output from.  Every concrete stage implements this two-argument
        form; the protocol mirrors it so callers programming to ``Stage``
        (e.g. the replay runner in ``debugger/server.py``) can pass a
        cassette.

        Note: endpoint-detector stages (``VADStage``, ``TurnStage``) also
        expose a ``replay_decision(snapshot)`` method.  That is a
        detector-specific extension and is intentionally *not* part of the
        ``Stage`` contract — callers must narrow to the concrete type
        before using it.
        """
        ...

    async def handle_upstream(self, signal: ControlSignal, ctx: RunContext | None = None) -> None:
        """Observe (and journal) an upstream control signal.

        This is observation/journaling only.  When *ctx* is supplied, the
        stage journals a ``ControlSignalRecord`` so the signal path is
        visible alongside normal stage events; it performs **no**
        cancellation or truncation.  The real cancel/truncate work is
        owned out-of-band by the ``CancelOrchestrator`` and the turn
        runner (which cancel the in-flight task and call
        ``provider.cancel()``).  Custom stages should follow the same
        contract: react to a signal here by recording/adjusting local
        observability state, not by trying to tear down downstream work.
        """
        ...


# ── Non-deterministic fields ─────────────────────────────────────
# Re-exported from ``runtime.nondeterministic`` (canonical home).
# Extended in ``runtime.replay`` as ``REPLAY_IGNORE_FIELDS``.


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


def journal_ctx(ctx: RunContext, fallback_journal: Any) -> RunContext:
    """Return *ctx*, substituting *fallback_journal* when ctx has no journal.

    Recording normally flows through ``ctx.journal``; when the RunContext
    was built without one but the stage was handed a journal directly
    (direct construction), we record into that fallback so recording is
    never silently dead.
    """
    if ctx.journal is None and fallback_journal is not None:
        return dataclasses.replace(ctx, journal=fallback_journal)
    return ctx


def annotate_stage_exception(
    exc: BaseException,
    *,
    stage: str,
    provider: str | None = None,
    elapsed_ms: float | None = None,
    sequence: int | None = None,
    record_key: str | None = None,
) -> None:
    """Attach common PEP 678 context for stage-raised exceptions."""
    from easycat.events import _add_exception_notes

    record_key = record_key or _checkpoint_ref(sequence)
    sequence = sequence if sequence is not None and sequence >= 0 else None
    _add_exception_notes(
        exc,
        stage=stage,
        provider=provider,
        elapsed_ms=elapsed_ms,
        sequence=sequence,
        record_key=record_key,
    )


def stage_error_context(
    *,
    elapsed_ms: float,
    input_sequence: int | None = None,
) -> dict[str, Any]:
    """Build shared ``stage_error`` data for provider failures."""
    payload: dict[str, Any] = {"elapsed_ms": elapsed_ms}
    record_ref = _checkpoint_ref(input_sequence)
    if input_sequence is not None and input_sequence >= 0 and record_ref is not None:
        payload["input_sequence"] = input_sequence
        payload["input_record_ref"] = record_ref
    return payload


def _checkpoint_ref(sequence: int | None) -> str | None:
    if sequence is None or sequence < 0:
        return None
    from easycat.debug.bundle import checkpoint_id

    return checkpoint_id(sequence)


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
    tags: frozenset[str] = frozenset(),
) -> int | None:
    """Append a stage-scoped journal record.

    Centralises the boilerplate every stage used to duplicate: stamping
    ``data["stage"]`` for replay-runner filtering, stringifying state
    snapshots for JSON serialization, and passing artifact refs through
    to the journal's first-class ``input_ref`` / ``output_ref`` fields
    instead of burying them in ``data`` as strings.
    """
    if ctx.journal is None:
        return None
    payload: dict[str, Any] = {"stage": stage}
    if state_before is not None:
        payload["state_before"] = str(state_before)
    if state_after is not None:
        payload["state_after"] = str(state_after)
    if error is not None:
        payload["error"] = error
    if data_extra:
        payload.update(data_extra)
    return ctx.journal.append(
        kind=kind,
        name=name,
        session_id=ctx.session_id,
        turn_id=turn_id,
        data=payload,
        input_ref=input_ref,
        output_ref=output_ref,
        tags=tags,
    )


def record_stage_failure(
    exc: BaseException,
    ctx: RunContext,
    *,
    stage: str,
    provider: str,
    surface: str,
    elapsed_ms: float,
    sequence: int | None,
    turn_id: str | None = None,
    state_before: StageStateSnapshot | None = None,
) -> None:
    """Record the shared provider-failure trio for a raising stage.

    Runs the three uniform side effects every stage repeats in its
    ``except Exception`` arm: PEP 678 exception annotation, the
    ``easycat.provider.errors.total`` counter, and a ``stage_error``
    journal event.  ``provider`` and ``surface`` are passed explicitly
    because they vary per stage and are not derivable from each other —
    ``AudioStage`` in particular threads the in-flight component
    (echo canceller vs. noise reducer) so a failure is attributed to the
    provider that actually raised.  The caller keeps its
    ``result_attr = "fail"`` bookkeeping and the ``raise`` at the call
    site.
    """
    annotate_stage_exception(
        exc,
        stage=stage,
        provider=provider,
        elapsed_ms=elapsed_ms,
        sequence=sequence,
    )
    observability.increment_counter(
        "easycat.provider.errors.total",
        attributes={
            "easycat.surface": surface,
            "easycat.provider": provider,
            "easycat.error_type": type(exc).__name__,
        },
    )
    journal_append_event(
        ctx,
        stage=stage,
        name="stage_error",
        turn_id=turn_id,
        state_before=state_before,
        error=str(exc),
        data_extra=stage_error_context(
            elapsed_ms=elapsed_ms,
            input_sequence=sequence,
        ),
    )


def live_replay_input(
    spec: ReplaySpec,
    cassette: ReplayCassette | None,
    *,
    record_name: str = "stage_start",
    source: Literal["input_ref", "output_ref", "data_input"] = "input_ref",
) -> Any:
    """Resolve a stage's LIVE-fidelity replay input.

    Shared prologue for every stage's ``replay`` LIVE branch: an explicit
    ``spec.overrides["input"]`` wins, otherwise the cassette's last
    ``record_name`` record (falling back to its last record) supplies the
    captured input.  ``source`` selects the extraction — the blob behind
    the record's ``input_ref`` / ``output_ref`` artifact ref, or the
    inline ``data["input"]`` string.
    """
    overrides = spec.overrides
    if "input" in overrides:
        return overrides["input"]
    if cassette is not None:
        record = cassette.last_record(record_name) or cassette.last_record()
        if record is not None:
            if source == "data_input":
                data = record.get("data") or {}
                if isinstance(data, dict) and "input" in data:
                    return data["input"]
            else:
                blob = cassette.blob(record.get(source))
                if blob is not None:
                    return blob
    return None


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
