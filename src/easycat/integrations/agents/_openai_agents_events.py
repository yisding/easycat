"""OpenAI Agents SDK event translator.

Maps raw streaming events from the OpenAI Agents SDK ``Runner.run_streamed``
to :class:`AgentBridgeEvent` instances and records tool phases to the
:class:`AgentRecorder`.  Used by :class:`OpenAIAgentsBridge`.
"""

from __future__ import annotations

from typing import Any

from easycat.integrations.agents.base import AgentBridgeEvent, AgentRecorder


def extract_text_delta(data: Any) -> str:
    """Extract a text delta string from a raw response event.

    Returns an empty string when the event is not a text output delta.
    """
    event_type = getattr(data, "type", "")
    if event_type == "response.output_text.delta":
        return getattr(data, "delta", "") or ""
    return ""


def extract_tool_delta(data: Any) -> AgentBridgeEvent | None:
    """Extract a tool-call argument delta from a raw response event.

    Returns ``None`` when the event is not a function-call arguments delta.
    """
    event_type = getattr(data, "type", "")
    if event_type == "response.function_call_arguments.delta":
        delta = getattr(data, "delta", "") or ""
        if delta:
            call_id = getattr(data, "call_id", "") or getattr(data, "item_id", "") or ""
            return AgentBridgeEvent(kind="tool_delta", text=delta, call_id=call_id)
    return None


def map_run_item(
    item: Any,
    recorder: AgentRecorder,
    pending: dict[str, str],
) -> AgentBridgeEvent | None:
    """Map a ``run_item_stream_event`` item to an :class:`AgentBridgeEvent`.

    Tracks pending tool calls via *pending* (``call_id -> tool_name``) and
    records tool phases to the *recorder*.  The tool name captured on the
    start phase is reused when recording the matching result so journal
    entries stay interpretable when several tools are in flight.  Returns
    ``None`` for unrecognised item types.
    """
    item_type = getattr(item, "type", "")
    if item_type == "tool_call_item":
        raw = getattr(item, "raw_item", None)
        name = getattr(raw, "name", "") or ""
        call_id = getattr(raw, "call_id", "") or ""
        pending[call_id] = name
        recorder.record_tool_call(phase="start", name=name, call_id=call_id)
        return AgentBridgeEvent(kind="tool_started", tool_name=name, call_id=call_id)
    if item_type == "tool_call_output_item":
        raw = getattr(item, "raw_item", None)
        call_id = getattr(raw, "call_id", "") or ""
        result_str = str(getattr(item, "output", "")) if hasattr(item, "output") else ""
        name = pending.pop(call_id, "")
        recorder.record_tool_call(phase="result", name=name, call_id=call_id)
        return AgentBridgeEvent(kind="tool_result", call_id=call_id, result=result_str)
    return None
