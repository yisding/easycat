"""SSE event translator for the OpenAI Responses API.

Parses Server-Sent Events from a streaming ``/v1/responses`` call and
maps them to :class:`AgentBridgeEvent` instances the voice pipeline
consumes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from easycat.integrations.agents.base import AgentBridgeEvent, AgentRecorder

logger = logging.getLogger(__name__)


def parse_sse_line(line: str) -> tuple[str, dict[str, Any]] | None:
    """Parse a single SSE ``data:`` line into ``(event_type, data_dict)``.

    Returns ``None`` for comment lines, blank lines, or ``event:`` /
    ``id:`` / ``retry:`` fields (which are consumed by the caller but
    don't produce bridge events on their own).
    """
    stripped = line.strip()
    if not stripped or stripped.startswith(":"):
        return None

    if not stripped.startswith("data:"):
        # event: / id: / retry: lines are not data payloads.
        return None

    payload = stripped[len("data:") :].strip()
    if not payload:
        return None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.debug("SSE line is not valid JSON: %s", payload[:120])
        return None

    event_type = data.get("type", "")
    if not event_type:
        return None

    return event_type, data


def translate_sse_event(
    event_type: str,
    data: dict[str, Any],
    recorder: AgentRecorder,
) -> AgentBridgeEvent | None:
    """Map a Responses API SSE event to an :class:`AgentBridgeEvent`.

    Returns ``None`` for events handled by the caller (``response.completed``,
    ``response.failed``) or events that have no bridge-level equivalent.
    """
    if event_type == "response.output_text.delta":
        delta = data.get("delta", "")
        if delta:
            return AgentBridgeEvent(kind="text_delta", text=delta)
        return None

    if event_type == "response.function_call_arguments.delta":
        delta = data.get("delta", "")
        call_id = data.get("call_id", "") or data.get("item_id", "") or ""
        if delta:
            return AgentBridgeEvent(kind="tool_delta", text=delta, call_id=call_id)
        return None

    if event_type == "response.output_item.done":
        item = data.get("item", {})
        item_type = item.get("type", "")

        if item_type == "function_call":
            name = item.get("name", "")
            call_id = item.get("call_id", "") or item.get("id", "") or ""
            recorder.record_tool_call(phase="start", name=name, call_id=call_id)
            return AgentBridgeEvent(kind="tool_started", tool_name=name, call_id=call_id)

        if item_type == "function_call_output":
            call_id = item.get("call_id", "") or item.get("id", "") or ""
            result_str = str(item.get("output", ""))
            recorder.record_tool_call(phase="result", name="", call_id=call_id)
            return AgentBridgeEvent(kind="tool_result", call_id=call_id, result=result_str)

        return None

    if event_type in ("response.completed", "response.failed"):
        # Handled by the streaming loop in RemoteResponsesAPIBridge.invoke().
        return None

    logger.debug("Unhandled Responses API SSE event: %s", event_type)
    return None
