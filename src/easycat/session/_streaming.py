"""Streaming agent consumption: translates an agent stream into TTS payloads.

Separates the "understanding agent stream events" concern from Session's
orchestration role.  Session wires this to a TTS queue and handles
concurrency; this module handles text buffering, sentence splitting,
markdown handling, and event emission.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from easycat.agent_runner import AgentStreamEventType
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    Error,
    ErrorStage,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
)
from easycat.metrics import AGENT_LATENCY, MetricsCollector
from easycat.session._text_utils import (
    _has_unclosed_markdown_delimiters,
    _split_at_sentence_boundaries,
)
from easycat.session._turn_context import TurnContext
from easycat.strip_markdown import strip_markdown
from easycat.tts.input import TTSInput

logger = logging.getLogger(__name__)


@dataclass
class AgentStreamResult:
    """Result returned by :func:`consume_agent_stream`."""

    text: str = ""
    structured_output: Any = None
    error: BaseException | None = None
    interrupted: bool = False


async def consume_agent_stream(
    agent: Any,
    transcript: str,
    *,
    token: CancelToken | None,
    tts_queue: asyncio.Queue[TTSInput | None],
    emit: Callable[[Any], Awaitable[None]],
    prepare_tts_payload: Callable[..., TTSInput],
    strip_md: bool,
    turn: TurnContext,
    metrics: MetricsCollector | None,
) -> AgentStreamResult:
    """Consume a streaming agent and queue TTS payloads on sentence boundaries.

    This is the "translation layer" between agent stream events and TTS
    payloads.  It accumulates text deltas, splits at sentence boundaries,
    handles markdown buffering, emits EasyCat events, and drains in-flight
    tool calls during cancellation.

    Returns an :class:`AgentStreamResult` with the accumulated text,
    structured output, and any error that occurred.
    """
    result = AgentStreamResult()
    text_buffer = ""
    pending_tool_calls = 0
    done_received = False

    async def _flush_buffer() -> None:
        nonlocal text_buffer
        if text_buffer.strip():
            if strip_md:
                text_buffer = strip_markdown(text_buffer, normalize_code_spans=True)
            payload = prepare_tts_payload(text_buffer, is_streaming=True, is_final=True)
            if payload.text.strip():
                await tts_queue.put(payload)
        text_buffer = ""

    try:
        async for event in agent.run_streaming(transcript, cancel_token=token):
            if done_received:
                continue

            # ── Cancellation: drain tool calls then stop ──
            if token and token.is_cancelled:
                if not result.interrupted:
                    result.interrupted = True
                if pending_tool_calls > 0:
                    if event.type == AgentStreamEventType.TOOL_RESULT:
                        pending_tool_calls = max(0, pending_tool_calls - 1)
                        await emit(ToolCallResult(call_id=event.call_id, result=event.result))
                        if pending_tool_calls <= 0:
                            break
                    elif event.type == AgentStreamEventType.TOOL_STARTED:
                        pending_tool_calls += 1
                        await emit(
                            ToolCallStarted(tool_name=event.tool_name, call_id=event.call_id)
                        )
                    elif event.type == AgentStreamEventType.TOOL_DELTA:
                        await emit(ToolCallDelta(call_id=event.call_id, delta=event.text))
                    elif event.type == AgentStreamEventType.DONE:
                        if event.text:
                            result.text = event.text
                        if event.structured_output is not None:
                            result.structured_output = event.structured_output
                        break
                    continue
                else:
                    break

            # ── Normal event handling ──
            if event.type == AgentStreamEventType.TEXT_DELTA:
                result.text += event.text
                await emit(AgentDelta(text=event.text))

                # Record first-token latency
                if turn.first_agent_time is None:
                    turn.first_agent_time = time.monotonic()
                    if metrics and turn.stt_final_time is not None:
                        metrics.record_latency(
                            AGENT_LATENCY,
                            (turn.first_agent_time - turn.stt_final_time) * 1000,
                        )

                # Buffer text and queue complete sentences for TTS
                if strip_md:
                    text_buffer += event.text
                    if _has_unclosed_markdown_delimiters(text_buffer):
                        continue
                    stripped_window = strip_markdown(
                        text_buffer, trim=False, normalize_code_spans=True
                    )
                    ready, remaining = _split_at_sentence_boundaries(stripped_window)
                    if ready:
                        payload = prepare_tts_payload(ready, is_streaming=True, is_final=False)
                        if payload.text.strip():
                            await tts_queue.put(payload)
                    text_buffer = remaining
                else:
                    text_buffer += event.text
                    ready, text_buffer = _split_at_sentence_boundaries(text_buffer)
                    if ready:
                        payload = prepare_tts_payload(ready, is_streaming=True, is_final=False)
                        if payload.text.strip():
                            await tts_queue.put(payload)

            elif event.type == AgentStreamEventType.TOOL_STARTED:
                pending_tool_calls += 1
                await emit(ToolCallStarted(tool_name=event.tool_name, call_id=event.call_id))
            elif event.type == AgentStreamEventType.TOOL_DELTA:
                await emit(ToolCallDelta(call_id=event.call_id, delta=event.text))
            elif event.type == AgentStreamEventType.TOOL_RESULT:
                pending_tool_calls = max(0, pending_tool_calls - 1)
                await emit(ToolCallResult(call_id=event.call_id, result=event.result))
            elif event.type == AgentStreamEventType.DONE:
                if event.text:
                    result.text = event.text
                if event.structured_output is not None:
                    result.structured_output = event.structured_output
                await _flush_buffer()
                done_received = True

    except Exception as exc:
        result.error = exc
        logger.exception("Agent streaming error")
        await emit(Error(exception=exc, stage=ErrorStage.AGENT))
    finally:
        stream_succeeded = result.error is None and (not token or not token.is_cancelled)
        if stream_succeeded:
            await _flush_buffer()
        await tts_queue.put(None)  # sentinel to stop TTS task

    return result
