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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from easycat.events import (
    AgentDelta,
    Error,
    ErrorStage,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
)
from easycat.session._turn_context import TurnContext
from easycat.session.text import (
    has_unclosed_markdown_delimiters,
    split_at_sentence_boundaries,
)
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
    stream_factory: Callable[[], AsyncIterator[Any]],
    *,
    cancel_token: Any | None,
    tts_queue: asyncio.Queue[TTSInput | None],
    emit: Callable[[Any], Awaitable[None]],
    prepare_tts_payload: Callable[..., TTSInput],
    strip_md: bool,
    turn: TurnContext,
) -> AgentStreamResult:
    """Consume an :class:`AgentBridgeEvent` stream and queue TTS payloads.

    This is the "translation layer" between bridge events and TTS
    payloads.  It accumulates text deltas, splits at sentence boundaries,
    handles markdown buffering, emits EasyCat-level events, and drains
    in-flight tool calls during cancellation.

    ``stream_factory`` is a zero-argument callable returning the async
    iterator to consume — typically
    ``lambda: agent_stage.execute_streaming(...)``.

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
        stream = stream_factory()
        async for event in stream:
            kind = getattr(event, "kind", None)
            if kind is None:
                continue

            if done_received:
                continue

            # ── Cancellation: drain tool calls then stop ──
            if cancel_token and cancel_token.is_cancelled:
                if not result.interrupted:
                    result.interrupted = True
                if pending_tool_calls > 0:
                    if kind == "tool_result":
                        pending_tool_calls = max(0, pending_tool_calls - 1)
                        await emit(ToolCallResult(call_id=event.call_id, result=event.result))
                        if pending_tool_calls <= 0:
                            break
                    elif kind == "tool_started":
                        pending_tool_calls += 1
                        await emit(
                            ToolCallStarted(tool_name=event.tool_name, call_id=event.call_id)
                        )
                    elif kind == "tool_delta":
                        await emit(ToolCallDelta(call_id=event.call_id, delta=event.text))
                    elif kind == "done":
                        if event.text:
                            result.text = event.text
                        if getattr(event, "structured_output", None) is not None:
                            result.structured_output = event.structured_output
                        break
                    continue
                else:
                    # No tool calls left to drain. Capture any trailing
                    # ``done`` payload (text / structured_output) before
                    # stopping, mirroring the pending>0 branch above, so an
                    # interrupted stream still surfaces its partial result.
                    if kind == "done":
                        if event.text:
                            result.text = event.text
                        if getattr(event, "structured_output", None) is not None:
                            result.structured_output = event.structured_output
                    break

            # ── Normal event handling ──
            if kind == "text_delta":
                result.text += event.text
                await emit(AgentDelta(text=event.text))

                # Record first-token latency
                if turn.first_agent_time is None:
                    turn.first_agent_time = time.monotonic()

                # Buffer text and queue complete sentences for TTS
                if strip_md:
                    text_buffer += event.text
                    if has_unclosed_markdown_delimiters(text_buffer):
                        continue
                    stripped_window = strip_markdown(
                        text_buffer, trim=False, normalize_code_spans=True
                    )
                    ready, remaining = split_at_sentence_boundaries(stripped_window)
                    if ready:
                        payload = prepare_tts_payload(ready, is_streaming=True, is_final=False)
                        if payload.text.strip():
                            await tts_queue.put(payload)
                    text_buffer = remaining
                else:
                    text_buffer += event.text
                    ready, text_buffer = split_at_sentence_boundaries(text_buffer)
                    if ready:
                        payload = prepare_tts_payload(ready, is_streaming=True, is_final=False)
                        if payload.text.strip():
                            await tts_queue.put(payload)

            elif kind == "tool_started":
                pending_tool_calls += 1
                await emit(ToolCallStarted(tool_name=event.tool_name, call_id=event.call_id))
            elif kind == "tool_delta":
                await emit(ToolCallDelta(call_id=event.call_id, delta=event.text))
            elif kind == "tool_result":
                pending_tool_calls = max(0, pending_tool_calls - 1)
                await emit(ToolCallResult(call_id=event.call_id, result=event.result))
            elif kind == "done":
                if event.text:
                    if not result.text:
                        text_buffer = event.text
                    result.text = event.text
                if getattr(event, "structured_output", None) is not None:
                    result.structured_output = event.structured_output
                await _flush_buffer()
                done_received = True
            elif kind in ("cursor_entered", "cursor_exited", "handoff", "state_snapshot"):
                # Observability-only kinds. Bridges already write the
                # authoritative execution-cursor / handoff / state records via
                # the AgentRecorder (the journal is the single source of truth
                # for these). They are surfaced on the stream so out-of-band
                # consumers can follow framework progress, but they never drive
                # TTS, so this translation layer intentionally ignores them.
                pass

    except Exception as exc:
        result.error = exc
        logger.exception("Agent streaming error")
        await emit(Error(exception=exc, stage=ErrorStage.AGENT))
    finally:
        stream_succeeded = result.error is None and (
            not cancel_token or not cancel_token.is_cancelled
        )
        if stream_succeeded:
            await _flush_buffer()
        await tts_queue.put(None)  # sentinel to stop TTS task

    return result
