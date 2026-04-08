"""Workflow-level adapter for stateful PydanticAI orchestration.

Deprecated: use easycat.integrations.agents.generic_workflow.GenericWorkflowBridge instead.

Wraps a workflow object that owns the application-level control flow across
multiple user turns. This is a better fit for PydanticAI's programmatic
hand-off model than adapting only an individual ``pydantic_ai.Agent``.

The wrapped workflow is expected to expose ``on_user_turn(text)`` and may
optionally expose ``on_user_turn_streaming(text, cancel_token=...)``.
"""
# ruff: noqa: E402

from __future__ import annotations

import warnings

warnings.warn(
    "easycat.agents.pydantic_ai_workflow is deprecated. "
    "Use easycat.integrations.agents.generic_workflow.GenericWorkflowBridge instead. "
    "See docs/migration-debug-first-runtime.md for migration details.",
    DeprecationWarning,
    stacklevel=2,
)

import inspect
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from easycat.agent_runner import INTERRUPTION_NOTE, AgentStreamEvent, AgentStreamEventType
from easycat.agents.base import BaseAgentAdapter
from easycat.cancel import CancelToken

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowTurnResult:
    """Result returned by a workflow turn.

    Parameters
    ----------
    text:
        The spoken response that EasyCat should synthesize.
    structured_output:
        Optional machine-readable result for application code and
        ``AgentFinal.structured_output``.
    active_agent_id:
        Optional identifier for the currently active specialist/step after
        this turn completes.
    """

    text: str
    structured_output: Any = None
    active_agent_id: str | None = None


@runtime_checkable
class PydanticAIWorkflow(Protocol):
    """Minimal workflow protocol for programmatic multi-agent control."""

    def on_user_turn(self, text: str) -> Any: ...


@runtime_checkable
class StreamingPydanticAIWorkflow(PydanticAIWorkflow, Protocol):
    """Workflow protocol with streaming support."""

    def on_user_turn_streaming(
        self,
        text: str,
        *,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]: ...


class PydanticAIWorkflowAdapter(BaseAgentAdapter):
    """Expose a stateful workflow as an EasyCat-compatible agent.

    The workflow owns routing, hand-offs, and per-agent private histories.
    This adapter presents a simple agent interface to ``Session`` while
    preserving workflow-level state such as the active specialist.
    """

    def __init__(
        self,
        workflow: Any,
        *,
        output_type: type | None = None,
    ) -> None:
        super().__init__()
        self._workflow = workflow
        self._output_type = output_type
        self._active_agent_id = self._workflow_active_agent_id()

    @property
    def workflow(self) -> Any:
        """The wrapped workflow object."""
        return self._workflow

    @property
    def active_agent_id(self) -> str | None:
        """Identifier for the specialist/step currently handling the session."""
        current = self._workflow_active_agent_id()
        return current if current is not None else self._active_agent_id

    @property
    def output_type(self) -> type | None:
        """Structured output type, if declared by the workflow or adapter."""
        if self._output_type is not None:
            return self._output_type
        otype = getattr(self._workflow, "output_type", None)
        if otype is str or otype is None:
            return None
        return otype

    def clear_history(self) -> None:
        """Clear local history and reset the workflow when supported."""
        super().clear_history()
        for method_name in ("clear_history", "reset"):
            fn = getattr(self._workflow, method_name, None)
            if not callable(fn):
                continue
            result = fn()
            if inspect.isawaitable(result):
                result.close()
                logger.warning(
                    "Ignoring awaitable returned by workflow.%s(); clear_history() must be sync",
                    method_name,
                )
            break
        self._active_agent_id = self._workflow_active_agent_id()

    def notify_interruption(
        self,
        text_spoken: str = "",
        *,
        mode: str = "truncate",
    ) -> None:
        """Propagate interruption handling to the workflow and local history."""
        fn = getattr(self._workflow, "notify_interruption", None)
        if callable(fn):
            try:
                fn(text_spoken, mode=mode)
            except Exception:
                logger.debug("Error in workflow.notify_interruption", exc_info=True)
        super().notify_interruption(text_spoken, mode=mode)

    def replace_last_assistant_text(self, text: str) -> None:
        """Patch the latest assistant turn locally and in the workflow when supported."""
        for entry in reversed(self._message_history):
            if isinstance(entry, dict) and entry.get("role") == "assistant":
                entry["content"] = text
                break

        fn = getattr(self._workflow, "replace_last_assistant_text", None)
        if callable(fn):
            fn(text)

    def _truncate_last_assistant_for_interruption(self, text_spoken: str) -> bool:
        replacement = self.interruption_replacement_text(text_spoken)
        for entry in reversed(self._message_history):
            if isinstance(entry, dict) and entry.get("role") == "assistant":
                entry["content"] = replacement
                return True
        return False

    def _append_interruption_note(self) -> None:
        for entry in reversed(self._message_history):
            if not isinstance(entry, dict):
                continue
            if entry.get("role") == "user":
                break
            if entry == {"role": "system", "content": INTERRUPTION_NOTE}:
                return
        self._message_history.append({"role": "system", "content": INTERRUPTION_NOTE})

    async def run(self, text: str) -> str:
        """Invoke the workflow for one user turn and return spoken text."""
        fn = getattr(self._workflow, "on_user_turn", None)
        if not callable(fn):
            raise TypeError("Workflow must define on_user_turn(text)")

        raw_result = fn(text)
        if inspect.isawaitable(raw_result):
            raw_result = await raw_result
        turn = self._normalize_turn_result(raw_result)
        return self._store_turn(text, turn)

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        """Run the workflow with streaming output when available.

        Falls back to a single full-text delta followed by ``DONE`` when the
        workflow only implements ``on_user_turn``.
        """
        del context

        fn = getattr(self._workflow, "on_user_turn_streaming", None)
        if not callable(fn):
            text_response = await self.run(text)
            if text_response:
                yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=text_response)
            yield self.done_event(text=text_response, raw_output=self.last_output)
            return

        stream = fn(text, cancel_token=cancel_token)
        if inspect.isawaitable(stream):
            stream = await stream

        accumulated = ""
        done_received = False
        async for event in stream:
            if event.type == AgentStreamEventType.TEXT_DELTA:
                accumulated += event.text
                yield event
                continue

            if event.type == AgentStreamEventType.DONE:
                done_received = True
                if event.text:
                    accumulated = event.text
                turn = WorkflowTurnResult(
                    text=accumulated,
                    structured_output=event.structured_output,
                    active_agent_id=self._workflow_active_agent_id(),
                )
                self._store_turn(text, turn)
                yield self.done_event(
                    text=turn.text,
                    raw_output=turn.structured_output
                    if turn.structured_output is not None
                    else turn.text,
                )
                break

            yield event

        if not done_received:
            turn = WorkflowTurnResult(
                text=accumulated,
                structured_output=None,
                active_agent_id=self._workflow_active_agent_id(),
            )
            self._store_turn(text, turn)
            yield self.done_event(text=turn.text, raw_output=turn.text)

    def _normalize_turn_result(self, result: Any) -> WorkflowTurnResult:
        if isinstance(result, WorkflowTurnResult):
            return result
        if isinstance(result, str):
            return WorkflowTurnResult(text=result)
        raise TypeError(
            f"Workflow turn must return str or WorkflowTurnResult, got {type(result).__name__}"
        )

    def _store_turn(self, user_text: str, turn: WorkflowTurnResult) -> str:
        self._message_history.append({"role": "user", "content": user_text})
        self._message_history.append({"role": "assistant", "content": turn.text})
        self._last_output = (
            turn.structured_output if turn.structured_output is not None else turn.text
        )
        if turn.active_agent_id is not None:
            self._active_agent_id = turn.active_agent_id
        else:
            self._active_agent_id = self._workflow_active_agent_id()
        return turn.text

    def _workflow_active_agent_id(self) -> str | None:
        active_agent_id = getattr(self._workflow, "active_agent_id", None)
        if active_agent_id is None:
            return None
        return str(active_agent_id)
