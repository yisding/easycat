"""Shared LangChain/LangGraph event translator.

Maps LangChain ``astream_events(version="v2")`` dicts and LangGraph
``astream(stream_mode="messages")`` message chunks to
``AgentBridgeEvent`` instances, and records tool phases on the
``AgentRecorder``.  Used by both ``LangChainBridge`` and
``LangGraphBridge`` so the two bridges share one event mapping.

Uses duck typing — the ``langchain_core`` package is not imported here
so tests can run without it installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from easycat.integrations.agents.base import AgentBridgeEvent, AgentRecorder


def _chunk_text(chunk: Any) -> str:
    """Extract a string text delta from an ``AIMessageChunk``-like object.

    LangChain message chunks carry either a plain string ``content`` or a
    list of typed content blocks (``{"type": "text", "text": "..."}``,
    thinking blocks, tool_use blocks, etc.).  Only the plain ``"text"``
    blocks should be fed to TTS; everything else is either audio-unsafe
    (JSON tool args) or internal reasoning that upstream code might want
    to surface separately.
    """
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


def translate_stream_event(
    event: dict[str, Any],
    recorder: AgentRecorder | None = None,
) -> Iterator[AgentBridgeEvent]:
    """Translate one ``astream_events(version="v2")`` event.

    ``event`` is a dict with at least ``event``, ``data`` and ``name``
    keys.  Text deltas yield ``text_delta`` events; tool lifecycle
    transitions are recorded via ``recorder.record_tool_call`` and also
    yielded as ``tool_started`` / ``tool_delta`` / ``tool_result``
    events so the runtime can drive TTS and UI updates.
    """
    if not isinstance(event, dict):
        return
    event_type = event.get("event")
    if not isinstance(event_type, str):
        return

    data = event.get("data") or {}
    name = event.get("name") or ""
    run_id = event.get("run_id") or ""

    if event_type == "on_chat_model_stream":
        chunk = data.get("chunk") if isinstance(data, dict) else None
        if chunk is None:
            return
        text = _chunk_text(chunk)
        if text:
            yield AgentBridgeEvent(kind="text_delta", text=text)

        tool_call_chunks = getattr(chunk, "tool_call_chunks", None) or []
        for tc_chunk in tool_call_chunks:
            if not isinstance(tc_chunk, dict):
                continue
            tc_name = tc_chunk.get("name") or ""
            tc_args = tc_chunk.get("args") or ""
            tc_id = tc_chunk.get("id") or ""
            if tc_name:
                if recorder is not None:
                    recorder.record_tool_call(
                        phase="start",
                        name=tc_name,
                        call_id=tc_id or None,
                    )
                yield AgentBridgeEvent(
                    kind="tool_started",
                    tool_name=tc_name,
                    call_id=tc_id,
                )
            if tc_args:
                if recorder is not None:
                    recorder.record_tool_call(
                        phase="delta",
                        name=tc_name or "",
                        call_id=tc_id or None,
                    )
                yield AgentBridgeEvent(
                    kind="tool_delta",
                    tool_name=tc_name,
                    call_id=tc_id,
                    text=tc_args,
                )

    elif event_type == "on_tool_start":
        tool_name = name
        call_id = run_id
        args_input = data.get("input") if isinstance(data, dict) else None
        args_text = ""
        if isinstance(args_input, dict):
            # Best-effort JSON-ish preview for tool_started payload.
            try:
                import json

                args_text = json.dumps(args_input, default=str)
            except Exception:
                args_text = str(args_input)
        elif args_input is not None:
            args_text = str(args_input)
        if recorder is not None:
            recorder.record_tool_call(
                phase="start",
                name=tool_name,
                call_id=call_id or None,
            )
        yield AgentBridgeEvent(
            kind="tool_started",
            tool_name=tool_name,
            call_id=call_id,
            text=args_text,
        )

    elif event_type == "on_tool_end":
        tool_name = name
        call_id = run_id
        output = data.get("output") if isinstance(data, dict) else None
        result_text = ""
        if output is not None:
            content = getattr(output, "content", None)
            if isinstance(content, str):
                result_text = content
            elif isinstance(content, list):
                result_text = "".join(
                    str(b.get("text", ""))
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                result_text = str(output)
        if recorder is not None:
            recorder.record_tool_call(
                phase="result",
                name=tool_name,
                call_id=call_id or None,
            )
        yield AgentBridgeEvent(
            kind="tool_result",
            tool_name=tool_name,
            call_id=call_id,
            result=result_text,
        )

    elif event_type == "on_tool_error":
        tool_name = name
        call_id = run_id
        if recorder is not None:
            recorder.record_tool_call(
                phase="error",
                name=tool_name,
                call_id=call_id or None,
            )
        # No dedicated event kind for tool errors in the public bridge
        # surface; surface it as a tool_result with empty result and a
        # reason carried on the event.
        yield AgentBridgeEvent(
            kind="tool_result",
            tool_name=tool_name,
            call_id=call_id,
            reason="tool_error",
        )


def translate_message_chunk(
    chunk: Any,
    recorder: AgentRecorder | None = None,
) -> Iterator[AgentBridgeEvent]:
    """Translate a LangGraph ``stream_mode="messages"`` chunk.

    The tuple from LangGraph is ``(message_chunk, metadata)``; pass the
    message_chunk here.  Emits the same shape as ``on_chat_model_stream``
    from ``astream_events`` for uniformity.
    """
    if chunk is None:
        return
    # Reuse the chat-model-stream path.
    wrapped = {
        "event": "on_chat_model_stream",
        "data": {"chunk": chunk},
        "name": getattr(chunk, "name", "") or "",
        "run_id": getattr(chunk, "id", "") or "",
    }
    yield from translate_stream_event(wrapped, recorder)
