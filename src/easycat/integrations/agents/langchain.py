"""LangChain bridge — wraps a ``Runnable`` via ``astream_events(version="v2")``.

Shallow integration suitable for LCEL chains, LangChain agents, or any
other composition that is *not* built with LangGraph (LangGraph graphs
get the deeper :class:`LangGraphBridge`).  The bridge surfaces text
deltas, tool calls, and unit transitions into the EasyCat journal so
voice-side debugging and barge-in work uniformly across agent
frameworks.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.integrations.agents._base_adapter import split_replacement_by_original_parts
from easycat.integrations.agents._langchain_events import translate_stream_event
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
    UnitKind,
)
from easycat.runtime.records import ErrorInfo

logger = logging.getLogger(__name__)


_LC_UNIT_KINDS: dict[str, UnitKind] = {
    "on_chain_start": UnitKind.SPECIALIST,
    "on_chat_model_start": UnitKind.MODEL_NODE,
    "on_llm_start": UnitKind.MODEL_NODE,
    "on_prompt_start": UnitKind.SPECIALIST,
    "on_parser_start": UnitKind.SPECIALIST,
    "on_retriever_start": UnitKind.SPECIALIST,
}
_LC_EXIT_EVENTS = {
    "on_chain_end",
    "on_chat_model_end",
    "on_llm_end",
    "on_prompt_end",
    "on_parser_end",
    "on_retriever_end",
}


class LangChainBridge:
    """Wraps a LangChain ``Runnable`` via ``astream_events(version="v2")``.

    Parameters
    ----------
    runnable:
        Any object implementing the LangChain ``Runnable`` protocol — an
        LCEL chain, a ``RunnableLambda``, a LangChain ``AgentExecutor``,
        etc.  Objects that are actually LangGraph ``CompiledStateGraph``
        instances should go through :class:`LangGraphBridge` instead;
        ``auto_adapt_agent()`` dispatches on the concrete type.
    display_name:
        Optional override for the top-level ``agent`` cursor display
        name (defaults to ``type(runnable).__name__``).
    input_key:
        Key under which ``turn_input.text`` is placed in the runnable's
        input dict.  Defaults to ``"input"`` (matching the LangChain
        Hub convention).  Pass ``None`` to pass the text as a bare
        string (useful for single-prompt runnables).
    history_key:
        Key under which the prior turn messages are placed.  Defaults
        to ``"history"``.  Set to ``None`` to disable history passing.
    """

    COMMITTABLE_BOUNDARIES = {
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
        UnitKind.SPECIALIST: CommitRule.BETWEEN_NODES,
        UnitKind.MODEL_NODE: CommitRule.NON_COMMITTABLE,
        UnitKind.TOOL_CALL: CommitRule.BETWEEN_PHASES,
    }

    def __init__(
        self,
        runnable: Any,
        *,
        display_name: str | None = None,
        input_key: str | None = "input",
        history_key: str | None = "history",
    ) -> None:
        if runnable is None:
            raise BridgeInputError("LangChainBridge requires a non-None runnable=")
        if not (
            hasattr(runnable, "astream_events")
            or hasattr(runnable, "ainvoke")
            or hasattr(runnable, "invoke")
        ):
            raise BridgeInputError(
                "LangChainBridge requires a LangChain Runnable (astream_events / "
                "ainvoke). Got: " + type(runnable).__name__
            )
        self._runnable = runnable
        self._display_name = display_name or type(runnable).__name__
        self._input_key = input_key
        self._history_key = history_key
        self._message_history: list[Any] = []
        self._last_output: Any = None

    # ── ExternalAgentBridge interface ─────────────────────────────

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        agent_cursor = ExecutionCursor(
            unit_id=f"agent-{uuid4().hex[:8]}",
            unit_kind=UnitKind.AGENT,
            display_name=self._display_name,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(agent_cursor)

        accumulated = ""
        # Track open nested cursors keyed by LangChain ``run_id`` so we
        # can pair ``on_*_start`` / ``on_*_end`` reliably without relying
        # on event ordering.
        open_cursors: dict[str, ExecutionCursor] = {}

        input_payload = self._build_input(turn_input.text)

        try:
            stream = self._runnable.astream_events(input_payload, version="v2")
            async for event in stream:
                if cancel_token and cancel_token.is_cancelled:
                    recorder.record_cancellation_boundary(
                        mode=CancellationMode.IMMEDIATE_STOP,
                        reason="cancel_token_set",
                    )
                    break

                self._maybe_enter_cursor(event, recorder, open_cursors, agent_cursor)
                for bridge_event in translate_stream_event(event, recorder):
                    if bridge_event.kind == "text_delta":
                        accumulated += bridge_event.text
                    yield bridge_event
                self._maybe_exit_cursor(event, recorder, open_cursors)

                # Capture final output from ``on_chain_end`` on the
                # top-level runnable.
                if (
                    event.get("event") == "on_chain_end"
                    and not event.get("parent_ids")
                    and isinstance(event.get("data"), dict)
                ):
                    self._last_output = event["data"].get("output")
        except Exception as exc:
            # Close any still-open nested cursors before bubbling.
            for cursor in reversed(list(open_cursors.values())):
                try:
                    recorder.record_unit_exited(cursor, reason="error")
                except Exception:
                    logger.debug("Failed to close cursor during error cleanup", exc_info=True)
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(agent_cursor, reason="error")
            raise

        # Close any cursors still open after the stream drains.
        for cursor in reversed(list(open_cursors.values())):
            recorder.record_unit_exited(cursor.with_committable(True), reason=None)
        open_cursors.clear()

        self._append_to_history(turn_input.text, accumulated)
        recorder.record_unit_exited(agent_cursor.with_committable(True), reason=None)
        yield AgentBridgeEvent(
            kind="done",
            text=accumulated,
            structured_output=self._last_output,
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(
            fields={
                "framework": "langchain",
                "runnable": self._display_name,
                "history_length": len(self._message_history),
            },
            kind="langchain",
        )

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        plan = self._plan_interruption(delivered_text, mode)

        actual_pre_ref = plan.pre_state_ref
        if recorder is not None:
            actual_pre_ref = recorder.record_state_snapshot(
                plan.pre_state_ref,
                payload=self._serialize_framework_state(),
            )

        if recorder is not None:
            try:
                recorder.record_state_committed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=actual_pre_ref,
                    post_state_ref=plan.post_state_ref,
                )
            except Exception:
                return

        try:
            self._apply_planned_mutation(plan)
        except Exception as exc:
            if recorder is not None:
                recorder.record_interruption_apply_failed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=actual_pre_ref,
                    post_state_ref=plan.post_state_ref,
                    failure_error=ErrorInfo.from_exception(exc),
                )
            raise

        if recorder is not None:
            recorder.record_state_snapshot(
                plan.post_state_ref,
                payload=self._serialize_framework_state(),
            )
            recorder.record_cancellation_boundary(
                mode=mode,
                reason=plan.mutation_kind,
                caused_by_signal_id=caused_by_signal_id,
            )

    def reset(self) -> None:
        self._message_history.clear()
        self._last_output = None

    # ── History post-processing ───────────────────────────────────

    def replace_last_assistant_text(self, text: str) -> None:
        """Rewrite the last assistant message in history.

        Called by the adapter shim after post-processing (e.g. Markdown
        stripping) so the next turn conditions on cleaned text rather
        than raw LLM output.
        """
        self._rewrite_last_ai_content(text)

    def append_interruption_note(self, note: str) -> None:
        """Append an interruption note so the next turn sees it."""
        try:
            from langchain_core.messages import SystemMessage

            self._message_history.append(SystemMessage(content=note))
        except ImportError:
            self._message_history.append({"role": "system", "content": note})
        except Exception:
            logger.debug("Failed to append interruption note to LangChain history", exc_info=True)

    # ── Internal ─────────────────────────────────────────────────

    def _build_input(self, text: str) -> Any:
        if self._input_key is None:
            return text
        payload: dict[str, Any] = {self._input_key: text}
        if self._history_key is not None:
            payload[self._history_key] = list(self._message_history)
        return payload

    def _append_to_history(self, user_text: str, assistant_text: str) -> None:
        """Extend message history after a successful turn.

        Uses LangChain's typed message classes when available, falling
        back to plain dicts otherwise.  The fallback path lets this
        bridge function against duck-typed test doubles that don't
        depend on ``langchain_core``.
        """
        try:
            from langchain_core.messages import AIMessage, HumanMessage

            self._message_history.append(HumanMessage(content=user_text))
            if assistant_text:
                self._message_history.append(AIMessage(content=assistant_text))
        except ImportError:
            self._message_history.append({"role": "user", "content": user_text})
            if assistant_text:
                self._message_history.append({"role": "assistant", "content": assistant_text})

    def _rewrite_last_ai_content(self, replacement: str) -> None:
        for i in range(len(self._message_history) - 1, -1, -1):
            msg = self._message_history[i]
            role = _role_of(msg)
            if role != "assistant":
                continue
            content = _content_of(msg)
            if isinstance(content, list):
                text_parts = [
                    p for p in content if isinstance(p, dict) and p.get("type") == "text"
                ]
                if text_parts:
                    originals = [str(p.get("text", "")) for p in text_parts]
                    splits = split_replacement_by_original_parts(originals, replacement)
                    for part, repl in zip(text_parts, splits):
                        part["text"] = repl
                    return
            # Plain string or empty content — overwrite.
            _set_content(msg, replacement)
            return

    def _serialize_framework_state(self) -> bytes:
        try:
            payload = [
                {"role": _role_of(m), "content": _content_of(m)} for m in self._message_history
            ]
            return json.dumps(payload, default=str).encode()
        except (TypeError, ValueError):
            return b"[]"

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        replacement = delivered_text + "..." if delivered_text else ""
        pre_ref = f"langchain-pre-{id(self._message_history):x}"
        post_ref = f"langchain-post-{id(self._message_history):x}"
        return InterruptionPlan(
            mutation_kind="interrupt_truncate",
            pre_state_ref=pre_ref,
            post_state_ref=post_ref,
            framework_instructions={
                "replacement": replacement,
                "delivered_text": delivered_text,
                "mode": mode.value,
            },
        )

    def _apply_planned_mutation(self, plan: InterruptionPlan) -> None:
        replacement = plan.framework_instructions["replacement"]
        self._rewrite_last_ai_content(replacement)

    def _maybe_enter_cursor(
        self,
        event: Mapping[str, Any],
        recorder: AgentRecorder,
        open_cursors: dict[str, ExecutionCursor],
        agent_cursor: ExecutionCursor,
    ) -> None:
        event_type = event.get("event")
        if not isinstance(event_type, str):
            return
        unit_kind = _LC_UNIT_KINDS.get(event_type)
        if unit_kind is None:
            return
        run_id = str(event.get("run_id") or uuid4().hex[:8])
        if run_id in open_cursors:
            return
        parent_ids = event.get("parent_ids") or []
        parent_unit_id: str | None = agent_cursor.unit_id
        for pid in reversed(parent_ids):
            if pid in open_cursors:
                parent_unit_id = open_cursors[pid].unit_id
                break
        display_name = event.get("name") or unit_kind.value
        cursor = ExecutionCursor(
            unit_id=f"{unit_kind.value}-{run_id[:8]}",
            unit_kind=unit_kind,
            display_name=str(display_name),
            parent_unit_id=parent_unit_id,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(cursor)
        open_cursors[run_id] = cursor

    def _maybe_exit_cursor(
        self,
        event: Mapping[str, Any],
        recorder: AgentRecorder,
        open_cursors: dict[str, ExecutionCursor],
    ) -> None:
        event_type = event.get("event")
        if event_type not in _LC_EXIT_EVENTS:
            return
        run_id = str(event.get("run_id") or "")
        cursor = open_cursors.pop(run_id, None)
        if cursor is None:
            return
        recorder.record_unit_exited(cursor.with_committable(True), reason=None)


# ── Helpers ──────────────────────────────────────────────────────


def _role_of(msg: Any) -> str:
    """Best-effort role extraction for both dict and typed messages."""
    if isinstance(msg, dict):
        return str(msg.get("role") or msg.get("type") or "")
    msg_type = getattr(msg, "type", None)
    if msg_type == "ai":
        return "assistant"
    if msg_type == "human":
        return "user"
    if msg_type == "system":
        return "system"
    if msg_type == "tool":
        return "tool"
    return getattr(msg, "role", "") or ""


def _content_of(msg: Any) -> Any:
    if isinstance(msg, dict):
        return msg.get("content", "")
    return getattr(msg, "content", "")


def _set_content(msg: Any, value: Any) -> None:
    if isinstance(msg, dict):
        msg["content"] = value
        return
    try:
        msg.content = value
    except (AttributeError, TypeError):
        object.__setattr__(msg, "content", value)
