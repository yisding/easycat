"""GenericWorkflowBridge — shallow and deep modes.

Supports user-defined orchestration code that does not use
``pydantic_ai.Agent`` or ``pydantic_graph.Graph``.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    FrameworkStateSnapshot,
    InterruptionPlan,
    ShallowModeInterruptionError,
    UnitKind,
)
from easycat.runtime.records import ErrorInfo

logger = logging.getLogger(__name__)


@runtime_checkable
class WorkflowProtocol(Protocol):
    """Shallow-mode protocol: ``on_user_turn(text) -> str``."""

    async def on_user_turn(self, text: str) -> Any: ...


@runtime_checkable
class StreamingWorkflowProtocol(Protocol):
    """Shallow-mode streaming: ``on_user_turn_streaming(text) -> AsyncIterator[str]``."""

    def on_user_turn_streaming(self, text: str) -> AsyncIterator[str]: ...


class GenericWorkflowBridge:
    """Bridge for user-defined orchestration code.

    Two modes:

    - **Shallow** (default): ``on_user_turn(text) -> str|AsyncIterator[str]``.
      One opaque cursor per turn.
    - **Deep** (opt-in): ``on_user_turn(text, *, recorder, cancel_token)``
      with ``recorder: AgentRecorder`` parameter.  User calls
      ``recorder.record_*`` methods from inside their orchestration.

    Mode is detected at construction via signature inspection.

    Interruption / barge-in behaviour
    ----------------------------------
    **Deep mode** supports mid-turn barge-in out of the box.  The
    ``InterruptionController`` calls ``apply_interruption`` and the bridge
    runs the four-step atomic write ordering (plan -> commit -> mutate ->
    paired record) as described in WS2B T2B.1.

    **Shallow mode** does **not** support mid-turn interruption by default
    because the bridge has no visibility into the workflow's internal state.
    When the ``InterruptionController`` attempts barge-in on a shallow
    workflow, ``apply_interruption`` raises
    ``ShallowModeInterruptionError``.  The controller catches the exception
    and **downgrades the turn to end-of-turn interruption**:

    1. The controller records a ``ControlSignalRecord`` with
       ``cause="shallow_mode_downgrade"`` in the journal.
    2. The current turn completes normally (no mid-turn cancellation).
    3. The next turn starts immediately after, without attempting barge-in.

    To opt in to mid-turn interruption in shallow mode, implement
    ``apply_interruption(delivered_text, mode)`` directly on the workflow
    object.  The bridge delegates to it via the same four-step atomic
    write ordering used by deep mode.

    The downgrade is visible via:

    - Journal records (``cause="shallow_mode_downgrade"``).
    - A ``logger.warning`` emitted by the ``InterruptionController``.
    - ``easycat doctor`` output when a shallow workflow is paired with a
      voice transport.
    """

    COMMITTABLE_BOUNDARIES = {
        UnitKind.WORKFLOW_NODE: CommitRule.BETWEEN_TURNS,
    }

    def __init__(
        self,
        workflow: Any,
        *,
        display_name: str | None = None,
    ) -> None:
        self._workflow = workflow
        self._display_name = display_name or type(workflow).__name__

        fn = getattr(workflow, "on_user_turn", None)
        if not callable(fn):
            raise BridgeInputError(
                "Workflow must implement on_user_turn(). "
                "See GenericWorkflowBridge docs for the supported signatures."
            )

        sig = inspect.signature(fn)
        self._deep_mode = "recorder" in sig.parameters
        self._accepts_cancel_token = "cancel_token" in sig.parameters
        self._last_output: Any = None
        self._mcp_warning_emitted = False

    @property
    def deep_mode(self) -> bool:
        return self._deep_mode

    # ── ExternalAgentBridge interface ─────────────────────────────

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        cursor = ExecutionCursor(
            unit_id=f"workflow-{uuid4().hex[:8]}",
            unit_kind=UnitKind.WORKFLOW_NODE,
            display_name=self._display_name,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(cursor)

        # Emit one-time warning if MCP servers configured on shallow workflow.
        if not self._mcp_warning_emitted and not self._deep_mode and recorder.context.mcp_servers:
            self._mcp_warning_emitted = True
            recorder.record_framework_error(
                ErrorInfo(
                    type="MCPShallowModeWarning",
                    message=(
                        f"MCP servers {list(recorder.context.mcp_servers)!r} "
                        "configured but GenericWorkflowBridge is in shallow mode. "
                        "MCP wiring is not supported in shallow mode — convert "
                        "to deep mode to use MCP servers."
                    ),
                )
            )

        self._last_output = None
        accumulated = ""
        try:
            if self._deep_mode:
                async for ev in self._invoke_deep(turn_input, recorder, cancel_token):
                    if ev.kind == "text_delta":
                        accumulated += ev.text
                    yield ev
            else:
                async for ev in self._invoke_shallow(turn_input, cancel_token):
                    if ev.kind == "text_delta":
                        accumulated += ev.text
                    yield ev
        except Exception as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(cursor, reason="error")
            raise

        recorder.record_unit_exited(cursor.with_committable(True), reason=None)
        yield AgentBridgeEvent(
            kind="done",
            text=accumulated,
            structured_output=self._last_output,
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        fields: dict[str, Any] = {
            "display_name": self._display_name,
            "mode": "deep" if self._deep_mode else "shallow",
        }
        if hasattr(self._workflow, "snapshot_state"):
            try:
                ws = self._workflow.snapshot_state()
                if isinstance(ws, dict):
                    fields["workflow_state"] = ws
            except Exception:
                pass
        return FrameworkStateSnapshot(
            fields=fields,
            kind="generic_workflow",
        )

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        # Shallow mode without explicit override → raise immediately.
        if not self._deep_mode and not hasattr(self._workflow, "apply_interruption"):
            raise ShallowModeInterruptionError(
                "Interruption is not supported in GenericWorkflowBridge "
                "shallow mode. Convert the workflow to deep mode by adding "
                "a `recorder: AgentRecorder` parameter to `on_user_turn`, "
                "or implement `workflow.apply_interruption(delivered_text, "
                "mode)` on the workflow object itself."
            )

        # Step 1: plan the mutation.
        plan = self._plan_interruption(delivered_text, mode)

        # Step 2: write FrameworkStateCommitted to the journal.
        if recorder is not None:
            try:
                recorder.record_state_committed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=plan.pre_state_ref,
                    post_state_ref=plan.post_state_ref,
                )
            except Exception:
                return

        # Step 3: apply the planned mutation.
        try:
            self._apply_planned_mutation(plan)
        except Exception as exc:
            # Step 4a: mutation failed.
            if recorder is not None:
                recorder.record_interruption_apply_failed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=plan.pre_state_ref,
                    post_state_ref=plan.post_state_ref,
                    failure_error=ErrorInfo.from_exception(exc),
                )
            raise

        # Step 4b: success.
        if recorder is not None:
            recorder.record_cancellation_boundary(
                mode=mode,
                reason=plan.mutation_kind,
                caused_by_signal_id=caused_by_signal_id,
            )

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        has_override = hasattr(self._workflow, "apply_interruption")
        mutation_kind = "interrupt_workflow_override" if has_override else "interrupt_cancel_token"
        pre_ref = f"workflow-pre-{id(self._workflow):x}"
        post_ref = f"workflow-post-{id(self._workflow):x}"
        return InterruptionPlan(
            mutation_kind=mutation_kind,
            pre_state_ref=pre_ref,
            post_state_ref=post_ref,
            framework_instructions={
                "has_override": has_override,
                "delivered_text": delivered_text,
                "mode": mode.value,
            },
        )

    def _apply_planned_mutation(self, plan: InterruptionPlan) -> None:
        instructions = plan.framework_instructions
        if instructions.get("has_override"):
            mode = CancellationMode(instructions["mode"])
            self._workflow.apply_interruption(instructions["delivered_text"], mode)
        else:
            # Deep mode without explicit apply_interruption — best-effort.
            logger.debug(
                "Deep-mode workflow %s has no apply_interruption; relying on cancel_token",
                self._display_name,
            )

    def reset(self) -> None:
        if hasattr(self._workflow, "reset"):
            self._workflow.reset()
        elif hasattr(self._workflow, "clear_history"):
            self._workflow.clear_history()
        self._last_output = None

    # ── Shallow mode ─────────────────────────────────────────────

    async def _invoke_shallow(
        self,
        turn_input: AgentTurnInput,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        # Check for streaming variant first.
        if hasattr(self._workflow, "on_user_turn_streaming"):
            async for chunk in self._workflow.on_user_turn_streaming(turn_input.text):
                if cancel_token and cancel_token.is_cancelled:
                    break
                if chunk:
                    yield AgentBridgeEvent(kind="text_delta", text=chunk)
            return

        result = self._workflow.on_user_turn(turn_input.text)
        if inspect.isasyncgen(result):
            async for chunk in result:
                if cancel_token and cancel_token.is_cancelled:
                    break
                if chunk:
                    yield AgentBridgeEvent(kind="text_delta", text=str(chunk))
        elif inspect.isawaitable(result):
            output = await result
            self._last_output = output
            text = self._extract_text(output)
            if text:
                yield AgentBridgeEvent(kind="text_delta", text=text)
        else:
            self._last_output = result
            text = self._extract_text(result)
            if text:
                yield AgentBridgeEvent(kind="text_delta", text=text)

    # ── Deep mode ────────────────────────────────────────────────

    async def _invoke_deep(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        kwargs: dict[str, Any] = {"recorder": recorder}
        if self._accepts_cancel_token:
            kwargs["cancel_token"] = cancel_token
        result = self._workflow.on_user_turn(turn_input.text, **kwargs)
        # Deep mode may return str, AsyncIterator[str], or a coroutine.
        if inspect.isasyncgen(result):
            async for chunk in result:
                if cancel_token and cancel_token.is_cancelled:
                    break
                if chunk:
                    yield AgentBridgeEvent(kind="text_delta", text=chunk)
        elif inspect.isawaitable(result):
            output = await result
            self._last_output = output
            text = self._extract_text(output)
            if text:
                yield AgentBridgeEvent(kind="text_delta", text=text)
        else:
            self._last_output = result
            text = self._extract_text(result)
            if text:
                yield AgentBridgeEvent(kind="text_delta", text=text)

    # ── Helpers ──────────────────────────────────────────────────

    def _extract_text(self, result: Any) -> str:
        if isinstance(result, str):
            return result
        if hasattr(result, "text"):
            return str(result.text)
        return str(result) if result is not None else ""
