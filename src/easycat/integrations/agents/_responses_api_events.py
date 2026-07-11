"""SSE event translator for the OpenAI Responses API.

Parses Server-Sent Events from a streaming ``/v1/responses`` call and
maps them to :class:`AgentBridgeEvent` instances the voice pipeline
consumes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from easycat.integrations.agents.base import AgentBridgeEvent, AgentRecorder

logger = logging.getLogger(__name__)

_PendingTools = dict[str, str] | None
_SSETranslator = Callable[
    [dict[str, Any], AgentRecorder, _PendingTools],
    AgentBridgeEvent | None,
]
_CALLER_HANDLED_EVENTS = frozenset(("response.completed", "response.failed"))


def _scalar_identifier(value: Any) -> str:
    """Return a safe string identifier for untrusted SSE IDs.

    Remote Responses API-compatible endpoints provide these values as JSON.
    Only JSON scalars can be safely normalized for use as dictionary keys and
    bridge event IDs; arrays/objects are malformed and are treated as missing.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, int | float | bool):
        return str(value)
    return ""


def _response_identifier(data: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = _scalar_identifier(data.get(key))
        if value:
            return value
    return ""


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
    if not isinstance(data, dict):
        return None

    event_type = data.get("type", "")
    if not event_type:
        return None

    return event_type, data


def _event_item(data: dict[str, Any]) -> Mapping[str, Any]:
    item = data.get("item")
    return item if isinstance(item, Mapping) else {}


def _translate_text_delta(
    data: dict[str, Any],
    _recorder: AgentRecorder,
    _pending: _PendingTools,
) -> AgentBridgeEvent | None:
    delta = data.get("delta")
    return (
        AgentBridgeEvent(kind="text_delta", text=delta)
        if isinstance(delta, str) and delta
        else None
    )


def _translate_output_item_added(
    data: dict[str, Any],
    recorder: AgentRecorder,
    pending: _PendingTools,
) -> AgentBridgeEvent | None:
    item = _event_item(data)
    if item.get("type") != "function_call":
        return None
    name = _scalar_identifier(item.get("name"))
    call_id = _response_identifier(item, "call_id", "id")
    if pending is not None and call_id:
        pending[call_id] = name
    recorder.record_tool_call(phase="start", name=name, call_id=call_id)
    return AgentBridgeEvent(kind="tool_started", tool_name=name, call_id=call_id)


def _translate_tool_delta(
    data: dict[str, Any],
    recorder: AgentRecorder,
    pending: _PendingTools,
) -> AgentBridgeEvent | None:
    delta = data.get("delta")
    if not isinstance(delta, str) or not delta:
        return None
    call_id = _response_identifier(data, "call_id", "item_id")
    name = pending.get(call_id, "") if pending is not None else ""
    recorder.record_tool_call(phase="delta", name=name, call_id=call_id)
    return AgentBridgeEvent(kind="tool_delta", text=delta, call_id=call_id)


def _translate_output_item_done(
    data: dict[str, Any],
    recorder: AgentRecorder,
    pending: _PendingTools,
) -> AgentBridgeEvent | None:
    item = _event_item(data)
    item_type = item.get("type")
    if item_type != "function_call_output":
        return None

    call_id = _response_identifier(item, "call_id", "id")
    result_str = str(item.get("output", ""))
    name = pending.pop(call_id, "") if pending is not None else ""
    recorder.record_tool_call(phase="result", name=name, call_id=call_id)
    return AgentBridgeEvent(kind="tool_result", call_id=call_id, result=result_str)


_SSE_TRANSLATORS: dict[str, _SSETranslator] = {
    "response.output_text.delta": _translate_text_delta,
    "response.output_item.added": _translate_output_item_added,
    "response.function_call_arguments.delta": _translate_tool_delta,
    "response.output_item.done": _translate_output_item_done,
}


def translate_sse_event(
    event_type: str,
    data: dict[str, Any],
    recorder: AgentRecorder,
    pending: dict[str, str] | None = None,
) -> AgentBridgeEvent | None:
    """Map a Responses API SSE event to an :class:`AgentBridgeEvent`.

    Returns ``None`` for events handled by the caller (``response.completed``,
    ``response.failed``) or events that have no bridge-level equivalent.

    *pending* is an optional ``call_id -> tool_name`` map supplied by the
    caller so the tool name captured on ``output_item.added`` can be reused
    when recording ``delta`` and ``result`` phases.  When ``None``, tool
    name is omitted from those records.
    """
    translator = _SSE_TRANSLATORS.get(event_type)
    if translator is not None:
        return translator(data, recorder, pending)
    if event_type in _CALLER_HANDLED_EVENTS:
        return None

    logger.debug("Unhandled Responses API SSE event: %s", event_type)
    return None
