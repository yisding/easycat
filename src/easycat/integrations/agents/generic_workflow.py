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
        self._last_output: Any = None

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

    def apply_interruption(self, delivered_text: str, mode: CancellationMode) -> None:
        # Check for explicit opt-in on the workflow object.
        if hasattr(self._workflow, "apply_interruption"):
            self._workflow.apply_interruption(delivered_text, mode)
            return

        if not self._deep_mode:
            raise ShallowModeInterruptionError(
                "Interruption is not supported in GenericWorkflowBridge "
                "shallow mode. Convert the workflow to deep mode by adding "
                "a `recorder: AgentRecorder` parameter to `on_user_turn`, "
                "or implement `workflow.apply_interruption(delivered_text, "
                "mode)` on the workflow object itself."
            )

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

        result = await self._workflow.on_user_turn(turn_input.text)
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
        result = self._workflow.on_user_turn(
            turn_input.text,
            recorder=recorder,
            cancel_token=cancel_token,
        )
        # Deep mode may return str, AsyncIterator[str], or a coroutine.
        if inspect.isasyncgen(result):
            async for chunk in result:
                if cancel_token and cancel_token.is_cancelled:
                    break
                if chunk:
                    yield AgentBridgeEvent(kind="text_delta", text=chunk)
        elif inspect.isawaitable(result):
            output = await result
            text = self._extract_text(output)
            if text:
                yield AgentBridgeEvent(kind="text_delta", text=text)
        else:
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
