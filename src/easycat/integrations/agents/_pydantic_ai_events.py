"""Shared PydanticAI event translator.

Maps ``pydantic_ai`` streaming events to ``AgentBridgeEvent`` and records
tool phases to the ``AgentRecorder``.  Used by both Agent mode and Graph
mode in ``PydanticAIBridge``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from easycat.integrations.agents.base import AgentBridgeEvent, AgentRecorder

_EventTranslator = Callable[[Any, AgentRecorder | None], AgentBridgeEvent | None]


def _translate_part_start(
    event: Any,
    _recorder: AgentRecorder | None,
) -> AgentBridgeEvent | None:
    part = getattr(event, "part", None)
    if type(event).__name__ != "PartStartEvent" or type(part).__name__ != "TextPart":
        return None
    content = getattr(part, "content", "") or ""
    part_index = getattr(event, "index", None)
    if not isinstance(part_index, int) or isinstance(part_index, bool) or part_index < 0:
        return None
    return AgentBridgeEvent(kind="text_replace", text=content, part_index=part_index)


def _translate_delta(
    event: Any,
    recorder: AgentRecorder | None,
    tool_call_ids: dict[int, str] | None,
) -> AgentBridgeEvent | None:
    delta = getattr(event, "delta", None)
    delta_cls = type(delta).__name__
    if delta_cls == "TextPartDelta":
        content = getattr(delta, "content_delta", "") or ""
        part_index = getattr(event, "index", None)
        return (
            AgentBridgeEvent(kind="text_delta", text=content, part_index=part_index)
            if content
            and isinstance(part_index, int)
            and not isinstance(part_index, bool)
            and part_index >= 0
            else None
        )
    if delta_cls != "ToolCallPartDelta":
        return None

    call_id = getattr(delta, "tool_call_id", "") or ""
    part_index = getattr(event, "index", None)
    if tool_call_ids is not None and isinstance(part_index, int):
        if call_id:
            tool_call_ids[part_index] = call_id
        else:
            call_id = tool_call_ids.get(part_index, "")

    args = getattr(delta, "args_delta", "") or ""
    if not args:
        return None
    text = args if isinstance(args, str) else json.dumps(args, default=str)
    if recorder is not None:
        recorder.record_tool_call(phase="delta", name="", call_id=call_id)
    return AgentBridgeEvent(kind="tool_delta", text=text, call_id=call_id)


def _translate_tool_started(
    event: Any,
    recorder: AgentRecorder | None,
) -> AgentBridgeEvent:
    part = getattr(event, "part", None)
    name = getattr(part, "tool_name", "") or ""
    call_id = getattr(part, "tool_call_id", "") or ""
    if recorder is not None:
        recorder.record_tool_call(phase="start", name=name, call_id=call_id)
    return AgentBridgeEvent(kind="tool_started", tool_name=name, call_id=call_id)


def _translate_tool_result(
    event: Any,
    recorder: AgentRecorder | None,
) -> AgentBridgeEvent:
    part = getattr(event, "part", None)
    name = getattr(event, "tool_name", None) or getattr(part, "tool_name", "") or ""
    call_id = getattr(event, "tool_call_id", None) or getattr(part, "tool_call_id", "") or ""
    result = getattr(event, "result", None)
    if result is None:
        result = getattr(event, "content", None)
    if result is None:
        result = getattr(part, "content", "")
    result_str = "" if result is None else str(result)
    if recorder is not None:
        recorder.record_tool_call(phase="result", name=name, call_id=call_id)
    return AgentBridgeEvent(
        kind="tool_result",
        tool_name=name,
        call_id=call_id,
        result=result_str,
    )


def _translate_final_result(
    event: Any,
    _recorder: AgentRecorder | None,
) -> AgentBridgeEvent | None:
    # PydanticAI v2's FinalResultEvent only identifies the output tool, so
    # callers emit done later when the agent run exposes its result.
    output = getattr(event, "result", None)
    if output is None:
        output = getattr(event, "output", None)
    if output is None:
        return None
    return AgentBridgeEvent(kind="done", text=str(output), structured_output=output)


_EVENT_TRANSLATORS: dict[str, _EventTranslator] = {
    "PartStartEvent": _translate_part_start,
    "FunctionToolCallEvent": _translate_tool_started,
    "FunctionToolResultEvent": _translate_tool_result,
    "OutputToolCallEvent": _translate_tool_started,
    "OutputToolResultEvent": _translate_tool_result,
    "FinalResultEvent": _translate_final_result,
}


def translate_event(
    event: Any,
    recorder: AgentRecorder | None = None,
    *,
    tool_call_ids: dict[int, str] | None = None,
) -> AgentBridgeEvent | None:
    """Map a PydanticAI streaming event to an ``AgentBridgeEvent``.

    Also records tool phases to the recorder when provided.  Uses duck
    typing so this works without importing PydanticAI types directly.
    """
    translated_delta = _translate_delta(event, recorder, tool_call_ids)
    if translated_delta is not None:
        return translated_delta

    translator = _EVENT_TRANSLATORS.get(type(event).__name__)
    return translator(event, recorder) if translator is not None else None
