"""Shared PydanticAI event translator.

Maps ``pydantic_ai`` streaming events to ``AgentBridgeEvent`` and records
tool phases to the ``AgentRecorder``.  Used by both Agent mode and Graph
mode in ``PydanticAIBridge``.
"""

from __future__ import annotations

import json
from typing import Any

from easycat.integrations.agents.base import AgentBridgeEvent, AgentRecorder


def translate_event(
    event: Any,
    recorder: AgentRecorder | None = None,
) -> AgentBridgeEvent | None:
    """Map a PydanticAI streaming event to an ``AgentBridgeEvent``.

    Also records tool phases to the recorder when provided.  Uses duck
    typing so this works without importing PydanticAI types directly.
    """
    event_cls = type(event).__name__

    # PartDeltaEvent → text_delta or tool_delta
    delta = getattr(event, "delta", None)
    if delta is not None:
        delta_cls = type(delta).__name__
        if delta_cls == "TextPartDelta":
            content = getattr(delta, "content_delta", "") or ""
            if content:
                return AgentBridgeEvent(kind="text_delta", text=content)
        elif delta_cls == "ToolCallPartDelta":
            args = getattr(delta, "args_delta", "") or ""
            if args:
                text = args if isinstance(args, str) else json.dumps(args, default=str)
                if recorder is not None:
                    recorder.record_tool_call(phase="delta", name="")
                return AgentBridgeEvent(kind="tool_delta", text=text)

    # FunctionToolCallEvent / OutputToolCallEvent → tool_started
    if event_cls in {"FunctionToolCallEvent", "OutputToolCallEvent"}:
        part = getattr(event, "part", None)
        name = getattr(part, "tool_name", "") or ""
        call_id = getattr(part, "tool_call_id", "") or ""
        if recorder is not None:
            recorder.record_tool_call(phase="start", name=name, call_id=call_id)
        return AgentBridgeEvent(kind="tool_started", tool_name=name, call_id=call_id)

    # FunctionToolResultEvent / OutputToolResultEvent → tool_result
    if event_cls in {"FunctionToolResultEvent", "OutputToolResultEvent"}:
        part = getattr(event, "part", None)
        call_id = getattr(event, "tool_call_id", None) or getattr(part, "tool_call_id", "") or ""
        result = getattr(event, "result", None)
        if result is None:
            result = getattr(event, "content", None)
        if result is None:
            result = getattr(part, "content", "")
        result_str = str(result)
        if recorder is not None:
            recorder.record_tool_call(phase="result", name="", call_id=call_id)
        return AgentBridgeEvent(kind="tool_result", call_id=call_id, result=result_str)

    # FinalResultEvent → done when the event carries output. PydanticAI v2's
    # FinalResultEvent only identifies the output tool, so the bridge emits
    # final done after agent_run.result is available.
    if event_cls == "FinalResultEvent":
        output = getattr(event, "result", None)
        if output is None:
            output = getattr(event, "output", None)
        if output is None:
            return None
        text = str(output) if output is not None else ""
        return AgentBridgeEvent(kind="done", text=text, structured_output=output)

    return None
