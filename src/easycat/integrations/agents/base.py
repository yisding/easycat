"""ExternalAgentBridge protocol, shared types, and error classes.

This module defines the contract that all agent framework integrations
implement.  Session speaks this protocol directly — there is no
intermediate adapter layer.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from easycat.cancel import CancelToken
from easycat.runtime.records import ErrorInfo

logger = logging.getLogger(__name__)

# ── Enums ────────────────────────────────────────────────────────


class UnitKind(enum.Enum):
    """Discriminator for execution cursor entries."""

    AGENT = "agent"
    SPECIALIST = "specialist"
    WORKFLOW_NODE = "workflow_node"
    MODEL_NODE = "model_node"
    TOOL_CALL = "tool_call"
    USER_PROMPT = "user_prompt"


class CancellationMode(enum.Enum):
    """How the runtime should cancel a running bridge turn."""

    IMMEDIATE_STOP = "immediate_stop"
    DRAIN_CURRENT_UNIT = "drain_current_unit"
    DRAIN_TO_COMMIT_POINT = "drain_to_commit_point"


class CommitRule(enum.Enum):
    """When a unit kind is committable."""

    BETWEEN_PHASES = "between_phases"
    BETWEEN_TURNS = "between_turns"
    BETWEEN_NODES = "between_nodes"
    NON_COMMITTABLE = "non_committable"
    USER_DETERMINED = "user_determined"


# ── Data classes ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ExecutionCursor:
    """Tracks where execution is within a bridge turn."""

    unit_id: str
    unit_kind: UnitKind | str
    display_name: str = ""
    parent_unit_id: str | None = None
    sequence: int = 0
    entered_at: int = 0  # monotonic_ns
    committable: bool = False

    def with_committable(self, committable: bool) -> ExecutionCursor:
        """Return a copy with a different ``committable`` flag."""
        return ExecutionCursor(
            unit_id=self.unit_id,
            unit_kind=self.unit_kind,
            display_name=self.display_name,
            parent_unit_id=self.parent_unit_id,
            sequence=self.sequence,
            entered_at=self.entered_at,
            committable=committable,
        )


@dataclass(frozen=True)
class FrameworkStateSnapshot:
    """JSON-safe snapshot of framework state at a point in time.

    Small fields go in ``fields`` (target: < 4 KB inline).  Anything
    larger goes via ``state_ref`` into the artifact store.
    """

    fields: dict[str, Any] = field(default_factory=dict)
    state_ref: str | None = None
    kind: str = ""


@dataclass(frozen=True)
class AgentTurnInput:
    """Input to a bridge ``invoke()`` call."""

    text: str
    context: list[dict[str, str]] = field(default_factory=list)
    turn_id: str | None = None

    @staticmethod
    def from_text(
        text: str,
        context: list[dict[str, str]] | None = None,
        *,
        turn_id: str | None = None,
    ) -> AgentTurnInput:
        """Construct from raw text, independent of voice pipeline."""
        return AgentTurnInput(
            text=text,
            context=context or [],
            turn_id=turn_id,
        )


AgentEventKind = Literal[
    "text_delta",
    "tool_started",
    "tool_delta",
    "tool_result",
    "done",
]


@dataclass(frozen=True)
class AgentBridgeEvent:
    """Event yielded by ``ExternalAgentBridge.invoke()``."""

    kind: AgentEventKind
    text: str = ""
    tool_name: str = ""
    call_id: str = ""
    result: str = ""
    structured_output: Any = None
    cursor: ExecutionCursor | None = None
    from_unit: str | None = None
    to_unit: str | None = None
    reason: str | None = None
    snapshot: FrameworkStateSnapshot | None = None


@dataclass(frozen=True)
class InterruptionPlan:
    """Describes an intended framework-state mutation without applying it.

    Returned by ``_plan_interruption`` and consumed by
    ``_apply_planned_mutation``.  The public ``apply_interruption``
    composes them with journal writes between (four-step atomic ordering).
    """

    mutation_kind: str  # e.g. "interrupt_truncate", "interrupt_drain"
    pre_state_ref: str  # artifact ref to current framework state
    post_state_ref: str  # artifact ref to post-mutation state
    framework_instructions: dict[str, Any] = field(default_factory=dict)


def run_interruption_journal_protocol(
    plan: InterruptionPlan,
    mode: CancellationMode,
    recorder: AgentRecorder | None,
    caused_by_signal_id: str | None,
    *,
    serialize_state: Callable[[], bytes],
    apply_mutation: Callable[[InterruptionPlan], None],
) -> None:
    """Run the four-step atomic interruption-journal protocol once.

    Bridges build an :class:`InterruptionPlan` and provide ``serialize_state``
    / ``apply_mutation`` callbacks; this helper owns the journal write
    ordering (plan → ``FrameworkStateCommitted`` → apply → paired
    success/failure) so the degraded-journal guards live in one place
    instead of being copy-pasted across every bridge.
    """
    # Step 1b: persist pre-mutation state snapshot.
    actual_pre_ref = plan.pre_state_ref
    if recorder is not None:
        actual_pre_ref = recorder.record_state_snapshot(
            plan.pre_state_ref,
            payload=serialize_state(),
        )

    # Step 2: write FrameworkStateCommitted to the journal.
    if recorder is not None:
        try:
            recorder.record_state_committed(
                mutation_kind=plan.mutation_kind,
                pre_state_ref=actual_pre_ref,
                post_state_ref=plan.post_state_ref,
            )
        except Exception:
            # Journal in degraded mode — skip mutation, runtime falls back.
            return

    # Step 3: apply the planned mutation.
    try:
        apply_mutation(plan)
    except Exception as exc:
        # Step 4a: mutation failed — write InterruptionApplyFailed.
        if recorder is not None:
            recorder.record_interruption_apply_failed(
                mutation_kind=plan.mutation_kind,
                pre_state_ref=actual_pre_ref,
                post_state_ref=plan.post_state_ref,
                failure_error=ErrorInfo.from_exception(exc),
            )
        raise

    # Step 4b: success — persist post-mutation state and write boundary.
    # The mutation already applied, so a degraded journal here must not
    # re-raise; we log and continue, keeping the success path symmetric
    # with the step-2 degraded-journal guard above.
    if recorder is not None:
        try:
            recorder.record_state_snapshot(
                plan.post_state_ref,
                payload=serialize_state(),
            )
            recorder.record_cancellation_boundary(
                mode=mode,
                reason=plan.mutation_kind,
                caused_by_signal_id=caused_by_signal_id,
            )
        except Exception:
            logger.debug(
                "interruption post-snapshot/boundary journal write failed; "
                "mutation already applied",
                exc_info=True,
            )


# ── Recorder types ───────────────────────────────────────────────


@dataclass(frozen=True)
class RecorderContext:
    """Bridge-readable side-channel exposed by the recorder."""

    run_id: str
    session_id: str
    turn_id: str | None = None
    mcp_servers: tuple[str, ...] = ()


@runtime_checkable
class AgentRecorder(Protocol):
    """Write-side shim bridges use to journal execution state."""

    @property
    def context(self) -> RecorderContext: ...

    def record_unit_entered(self, cursor: ExecutionCursor) -> None: ...

    def record_unit_exited(self, cursor: ExecutionCursor, reason: str | None = None) -> None: ...

    def safe_exit_cursor(self, cursor: ExecutionCursor, reason: str | None = "error") -> None:
        """Close ``cursor`` defensively, swallowing recorder errors.

        Used by bridges in their error / cancellation cleanup arms (where
        ``AgentRunner``-injected ``CancelledError`` / ``GeneratorExit``
        skips the normal exit) so the recorder's strict enter/exit stack
        invariant is preserved without a recorder fault masking the
        original exception.  Logs and continues on failure; never raises.
        """
        ...

    @contextmanager
    def unit(
        self, cursor: ExecutionCursor, *, commit_on_exit: bool = True
    ) -> Iterator[ExecutionCursor]:
        """Context manager wrapping enter/exit with guaranteed cleanup."""
        ...

    def record_tool_call(
        self,
        phase: Literal["start", "delta", "result", "error"],
        name: str,
        args_ref: str | None = None,
        result_ref: str | None = None,
        call_id: str | None = None,
    ) -> None: ...

    def record_state_snapshot(self, ref: str, *, payload: bytes | None = None) -> str: ...

    def record_framework_handoff(
        self,
        from_unit: str | None,
        to_unit: str,
        reason: str | None = None,
    ) -> None: ...

    def record_cancellation_boundary(
        self,
        mode: CancellationMode,
        reason: str | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None: ...

    def record_framework_error(self, error: ErrorInfo) -> None: ...

    def record_state_committed(
        self,
        mutation_kind: str,
        pre_state_ref: str | None = None,
        post_state_ref: str | None = None,
    ) -> None:
        """Write a ``FrameworkStateCommitted`` record before applying a mutation."""
        ...

    def record_interruption_apply_failed(
        self,
        mutation_kind: str,
        pre_state_ref: str | None = None,
        post_state_ref: str | None = None,
        failure_error: ErrorInfo | None = None,
    ) -> None:
        """Write an ``InterruptionApplyFailed`` record on mutation failure."""
        ...


class NullAgentRecorder:
    """No-op :class:`AgentRecorder` for driving bridges outside a Session.

    Used by :meth:`AgentRunner.run` and similar standalone-invocation helpers
    so callers don't have to construct a full journal just to satisfy
    :meth:`ExternalAgentBridge.invoke`.
    """

    context: RecorderContext = RecorderContext(run_id="null", session_id="")

    def record_unit_entered(self, cursor: ExecutionCursor) -> None:
        pass

    def record_unit_exited(self, cursor: ExecutionCursor, reason: str | None = None) -> None:
        pass

    def safe_exit_cursor(self, cursor: ExecutionCursor, reason: str | None = "error") -> None:
        pass

    @contextmanager
    def unit(
        self, cursor: ExecutionCursor, *, commit_on_exit: bool = True
    ) -> Iterator[ExecutionCursor]:
        yield cursor

    def record_tool_call(
        self,
        phase: Literal["start", "delta", "result", "error"],
        name: str,
        args_ref: str | None = None,
        result_ref: str | None = None,
        call_id: str | None = None,
    ) -> None:
        pass

    def record_state_snapshot(self, ref: str, *, payload: bytes | None = None) -> str:
        return ref

    def record_framework_handoff(
        self,
        from_unit: str | None,
        to_unit: str,
        reason: str | None = None,
    ) -> None:
        pass

    def record_cancellation_boundary(
        self,
        mode: CancellationMode,
        reason: str | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        pass

    def record_framework_error(self, error: ErrorInfo) -> None:
        pass

    def record_state_committed(
        self,
        mutation_kind: str,
        pre_state_ref: str | None = None,
        post_state_ref: str | None = None,
    ) -> None:
        pass

    def record_interruption_apply_failed(
        self,
        mutation_kind: str,
        pre_state_ref: str | None = None,
        post_state_ref: str | None = None,
        failure_error: ErrorInfo | None = None,
    ) -> None:
        pass


NULL_RECORDER: AgentRecorder = NullAgentRecorder()


# ── Bridge protocol ──────────────────────────────────────────────


@runtime_checkable
class ExternalAgentBridge(Protocol):
    """Protocol for agent framework bridges.

    Bridges expose framework execution state to the journal while
    producing text/tool events the voice pipeline consumes.
    """

    COMMITTABLE_BOUNDARIES: dict[UnitKind | str, CommitRule]

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        """Run one turn, yielding events as they occur."""
        ...

    def snapshot_state(self) -> FrameworkStateSnapshot:
        """Return a JSON-safe snapshot of the current framework state."""
        ...

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        """Mutate framework state to reflect an interruption.

        Uses four-step atomic write ordering when ``recorder`` is provided:
        plan → ``FrameworkStateCommitted`` → apply → paired success/failure.
        """
        ...

    def replace_last_assistant_text(self, text: str) -> None:
        """Rewrite the last assistant entry in the bridge's history.

        Called by Session after post-processing (e.g. Markdown stripping)
        so that subsequent turns condition on the cleaned text rather
        than the raw LLM output.  Bridges that do not maintain their
        own message history may no-op.
        """
        ...

    def append_interruption_note(self, note: str) -> None:
        """Append an interruption note so the next turn sees it.

        Called by Session when the configured ``interruption_mode`` is
        ``"message"`` (append a system/developer message rather than
        truncate the assistant response).
        """
        ...

    def reset(self) -> None:
        """Clear all framework state for a fresh session."""
        ...

    # NOTE: ``configure_runtime`` is an *optional* extension surface, not a
    # required member of this protocol.  It is intentionally **not** declared
    # in the protocol body because ``ExternalAgentBridge`` is
    # ``@runtime_checkable`` — adding it here would make ``isinstance(obj,
    # ExternalAgentBridge)`` return ``False`` for every bridge / ``AgentRunner``
    # that legitimately no-ops it.  The session factory probes for it with
    # ``getattr(bridge, "configure_runtime", None)`` (see
    # ``easycat.config._inject_agent_runtime``) and falls back to the
    # historical private-attribute path when absent.  Bridges that consume
    # session-level ``mcp_servers`` / ``model`` / ``api_key`` settings
    # implement the following signature::
    #
    #     def configure_runtime(
    #         self,
    #         *,
    #         mcp_servers: list[str] | None = None,
    #         model: str | None = None,
    #         api_key: str | None = None,
    #     ) -> None: ...
    #
    # ``mcp_servers`` is a list of EasyCat MCP URI strings; ``None`` means
    # "leave unchanged".  Passing an empty list explicitly clears any
    # previously-configured servers so a bridge reused across sessions does
    # not leak the prior list.


# ── Errors ───────────────────────────────────────────────────────


class BridgeInputError(ValueError):
    """Raised when bridge construction receives invalid arguments."""


class BridgeConfigurationError(RuntimeError):
    """Raised when a bridge detects a configuration problem at construction."""


class ConventionViolationError(RuntimeError):
    """Raised when a framework convention is not honored at runtime."""


class ShallowModeInterruptionError(RuntimeError):
    """Raised when ``apply_interruption`` is called on a shallow-mode bridge."""


class RecorderInvariantError(RuntimeError):
    """Raised when ``AgentRecorder`` invariants are violated."""


class MutationInjectedError(RuntimeError):
    """Test-only: injected into ``_apply_planned_mutation`` for atomicity tests."""
