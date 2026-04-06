"""OpenAI Agents SDK bridge for the debug-first runtime.

Ports the core logic from ``easycat.agents.openai_agents.OpenAIAgentsAdapter``
into the ``ExternalAgentBridge`` protocol, recording execution state to the
journal via ``AgentRecorder``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from easycat.agents.base import split_replacement_by_original_parts
from easycat.cancel import CancelToken
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    FrameworkStateSnapshot,
    UnitKind,
)
from easycat.runtime.records import ErrorInfo

logger = logging.getLogger(__name__)

try:
    from agents import Runner  # type: ignore[import-untyped]
except ImportError:
    Runner = None  # type: ignore[assignment,misc]

INTERRUPTION_NOTE = (
    "[The user interrupted the assistant's response and may not have heard all of it.]"
)


class OpenAIAgentsBridge:
    """Bridge wrapping an OpenAI Agents SDK ``Agent``.

    Implements ``ExternalAgentBridge`` while capturing agent transitions,
    tool calls, and handoffs to the journal via the ``AgentRecorder``.
    """

    COMMITTABLE_BOUNDARIES = {
        UnitKind.TOOL_CALL: CommitRule.BETWEEN_PHASES,
        UnitKind.MODEL_NODE: CommitRule.NON_COMMITTABLE,
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
    }

    def __init__(
        self,
        agent: Any,
        *,
        run_config: Any = None,
        context: Any = None,
        use_previous_response_id: bool = True,
        max_turns: int | None = None,
        hooks: Any = None,
    ) -> None:
        self._agent = agent
        self._original_agent = agent
        self._run_config = run_config
        self._context = context
        self._use_previous_response_id = use_previous_response_id
        self._max_turns = max_turns
        self._hooks = hooks
        self._previous_response_id: str | None = None
        self._pending_interruption: str | None = None
        self._message_history: list[Any] = []
        self._last_output: Any = None

    # ── ExternalAgentBridge interface ─────────────────────────────

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        if Runner is None:
            raise ImportError(
                "openai-agents package is required: pip install 'easycat[openai-agents]'"
            )

        agent_cursor = ExecutionCursor(
            unit_id=f"agent-{uuid4().hex[:8]}",
            unit_kind=UnitKind.AGENT,
            display_name=getattr(self._agent, "name", "OpenAIAgent"),
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(agent_cursor)
        yield AgentBridgeEvent(kind="cursor_entered", cursor=agent_cursor)

        input_data = self._build_input(turn_input.text)
        kwargs = self._build_kwargs()
        result = Runner.run_streamed(self._agent, input_data, **kwargs)

        accumulated = ""
        pending_tool_calls: set[str] = set()
        interrupted = False

        try:
            async for event in result.stream_events():
                if cancel_token and cancel_token.is_cancelled:
                    if not interrupted:
                        interrupted = True
                    if pending_tool_calls:
                        if event.type == "run_item_stream_event":
                            bridge_ev = self._map_run_item(
                                event.item, recorder, pending_tool_calls
                            )
                            if bridge_ev is not None:
                                yield bridge_ev
                                if not pending_tool_calls:
                                    break
                        elif event.type == "raw_response_event":
                            bridge_ev = self._extract_tool_delta(event.data)
                            if bridge_ev is not None:
                                yield bridge_ev
                        continue
                    else:
                        break

                if event.type == "raw_response_event":
                    delta = self._extract_text_delta(event.data)
                    if delta:
                        accumulated += delta
                        yield AgentBridgeEvent(kind="text_delta", text=delta)
                    else:
                        bridge_ev = self._extract_tool_delta(event.data)
                        if bridge_ev is not None:
                            yield bridge_ev
                elif event.type == "run_item_stream_event":
                    bridge_ev = self._map_run_item(event.item, recorder, pending_tool_calls)
                    if bridge_ev is not None:
                        yield bridge_ev
        except Exception as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(agent_cursor, reason="error")
            raise
        finally:
            self._message_history = result.to_input_list()
            if self._use_previous_response_id:
                self._previous_response_id = getattr(result, "last_response_id", None)
            last_agent = getattr(result, "last_agent", None)
            if last_agent is not None and last_agent is not self._agent:
                # Record handoff.
                old_name = getattr(self._agent, "name", "unknown")
                new_name = getattr(last_agent, "name", "unknown")
                recorder.record_unit_exited(agent_cursor, reason="handoff")
                recorder.record_framework_handoff(
                    from_unit=old_name,
                    to_unit=new_name,
                    reason="agent_handoff",
                )
                self._agent = last_agent
                # Enter new agent cursor for the handoff target.
                new_cursor = ExecutionCursor(
                    unit_id=f"agent-{uuid4().hex[:8]}",
                    unit_kind=UnitKind.AGENT,
                    display_name=new_name,
                    entered_at=time.monotonic_ns(),
                    committable=True,
                )
                recorder.record_unit_entered(new_cursor)
                recorder.record_unit_exited(new_cursor.with_committable(True), reason=None)
            else:
                recorder.record_unit_exited(agent_cursor.with_committable(True), reason=None)

        self._last_output = getattr(result, "final_output", None)
        yield AgentBridgeEvent(
            kind="done",
            text=accumulated,
            structured_output=self._last_output,
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(
            fields={
                "agent": getattr(self._agent, "name", "unknown"),
                "previous_response_id": self._previous_response_id,
                "turn_count": len(self._message_history),
            },
            kind="openai_agents",
        )

    def apply_interruption(self, delivered_text: str, mode: CancellationMode) -> None:
        replacement = delivered_text + "..." if delivered_text else ""
        for i in range(len(self._message_history) - 1, -1, -1):
            item = self._message_history[i]
            role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
            if role == "assistant":
                if isinstance(item, dict) and "content" in item:
                    content = item["content"]
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "output_text":
                                parts_list = [
                                    p
                                    for p in content
                                    if isinstance(p, dict) and p.get("type") == "output_text"
                                ]
                                originals = [str(p.get("text", "")) for p in parts_list]
                                replacements = split_replacement_by_original_parts(
                                    originals, replacement
                                )
                                for p, r in zip(parts_list, replacements):
                                    p["text"] = r
                                break
                    elif isinstance(content, str):
                        item["content"] = replacement
                elif not isinstance(item, dict) and hasattr(item, "content"):
                    item.content = replacement
                break
        if self._use_previous_response_id and self._previous_response_id is not None:
            self._pending_interruption = (
                "[The user interrupted the assistant's response. "
                f'They approximately heard: "{replacement}"]'
            )

    def reset(self) -> None:
        self._agent = self._original_agent
        self._message_history.clear()
        self._previous_response_id = None
        self._pending_interruption = None
        self._last_output = None

    # ── Internal helpers ─────────────────────────────────────────

    def _build_input(self, text: str) -> Any:
        if self._use_previous_response_id and self._previous_response_id is not None:
            parts: list[dict[str, str]] = []
            if self._pending_interruption is not None:
                parts.append({"role": "developer", "content": self._pending_interruption})
                self._pending_interruption = None
            parts.append({"role": "user", "content": text})
            return parts
        if self._message_history:
            return self._message_history + [{"role": "user", "content": text}]
        return text

    def _build_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self._run_config is not None:
            kwargs["run_config"] = self._run_config
        if self._context is not None:
            kwargs["context"] = self._context
        if self._use_previous_response_id:
            if self._previous_response_id is not None:
                kwargs["previous_response_id"] = self._previous_response_id
            kwargs["auto_previous_response_id"] = True
        if self._max_turns is not None:
            kwargs["max_turns"] = self._max_turns
        if self._hooks is not None:
            kwargs["hooks"] = self._hooks
        return kwargs

    @staticmethod
    def _extract_text_delta(data: Any) -> str:
        event_type = getattr(data, "type", "")
        if event_type == "response.output_text.delta":
            return getattr(data, "delta", "") or ""
        return ""

    @staticmethod
    def _extract_tool_delta(data: Any) -> AgentBridgeEvent | None:
        event_type = getattr(data, "type", "")
        if event_type == "response.function_call_arguments.delta":
            delta = getattr(data, "delta", "") or ""
            if delta:
                call_id = getattr(data, "call_id", "") or getattr(data, "item_id", "") or ""
                return AgentBridgeEvent(kind="tool_delta", text=delta, call_id=call_id)
        return None

    @staticmethod
    def _map_run_item(
        item: Any,
        recorder: AgentRecorder,
        pending: set[str],
    ) -> AgentBridgeEvent | None:
        item_type = getattr(item, "type", "")
        if item_type == "tool_call_item":
            raw = getattr(item, "raw_item", None)
            name = getattr(raw, "name", "") or ""
            call_id = getattr(raw, "call_id", "") or ""
            pending.add(call_id)
            recorder.record_tool_call(phase="start", name=name)
            return AgentBridgeEvent(kind="tool_started", tool_name=name, call_id=call_id)
        if item_type == "tool_call_output_item":
            raw = getattr(item, "raw_item", None)
            call_id = getattr(raw, "call_id", "") or ""
            result_str = str(getattr(item, "output", "")) if hasattr(item, "output") else ""
            pending.discard(call_id)
            recorder.record_tool_call(phase="result", name="")
            return AgentBridgeEvent(kind="tool_result", call_id=call_id, result=result_str)
        return None
