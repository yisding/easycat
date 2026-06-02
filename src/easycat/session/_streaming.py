"""Streaming agent consumption: translates an agent stream into TTS payloads.

Separates the "understanding agent stream events" concern from Session's
orchestration role.  Session wires this to a TTS queue and handles
concurrency; this module handles text buffering, sentence splitting,
markdown handling, and event emission.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from easycat._turn_context import TurnContext
from easycat.events import (
    AgentDelta,
    Error,
    ErrorStage,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
)
from easycat.session.text import (
    has_unclosed_markdown_delimiters,
    split_at_sentence_boundaries,
)
from easycat.strip_markdown import strip_markdown
from easycat.tts.input import TTSInput

logger = logging.getLogger(__name__)

# Characters that can make buffered text newly eligible for TTS sentence emission.
# When markdown stripping is enabled, avoid re-running delimiter checks, markdown
# regexes, and sentence segmentation on every tiny streamed delta; most deltas
# cannot complete a sentence and are therefore safe to buffer until one of these
# characters (or final stream flush) arrives.
_STREAMING_SENTENCE_TRIGGER_CHARS = frozenset(".!?。！？\n\r")

# Once a markdown construct is known to be open, sentence punctuation inside it
# is not enough to safely emit text.  Re-check the rolling markdown window only
# when a later delta contains a character that can plausibly close or otherwise
# disambiguate a markdown span.
_MARKDOWN_RECHECK_CHARS = frozenset("`*_~])")


@dataclass
class AgentStreamResult:
    """Result returned by :func:`consume_agent_stream`."""

    text: str = ""
    structured_output: Any = None
    error: BaseException | None = None
    interrupted: bool = False


async def emit_tool_event(
    event: Any,
    kind: str | None,
    *,
    emit: Callable[[Any], Awaitable[None]],
    session_id: str | None = None,
    turn_id: str | None = None,
    tool_span: Callable[[], AbstractContextManager[Any]] | None = None,
) -> bool:
    """Translate a tool-related bridge event into an EasyCat tool event.

    This is the single source of truth for the
    ``tool_started`` / ``tool_delta`` / ``tool_result`` →
    ``ToolCallStarted`` / ``ToolCallDelta`` / ``ToolCallResult`` mapping,
    shared by the streaming voice path (:func:`consume_agent_stream`) and
    the text path (``TurnRunner._execute_text_turn``) so the two cannot
    drift.

    ``session_id`` / ``turn_id`` are stamped onto the emitted event when
    provided (the text path needs this because it runs outside the
    TurnManager's active-turn window, where ``Session._emit`` would
    otherwise stamp a ``None`` turn id).  ``tool_span`` is an optional
    zero-arg factory returning a context manager wrapped around the
    ``ToolCallStarted`` emit for per-tool observability.

    Returns ``True`` when the event was a tool kind (and was emitted),
    ``False`` otherwise.  Callers remain responsible for any pending
    tool-call bookkeeping around the emit.
    """
    if kind == "tool_started":
        span = tool_span() if tool_span is not None else contextlib.nullcontext()
        with span:
            await emit(
                ToolCallStarted(
                    tool_name=event.tool_name,
                    call_id=event.call_id,
                    session_id=session_id,
                    turn_id=turn_id,
                )
            )
        return True
    if kind == "tool_delta":
        await emit(
            ToolCallDelta(
                call_id=event.call_id,
                delta=event.text,
                session_id=session_id,
                turn_id=turn_id,
            )
        )
        return True
    if kind == "tool_result":
        await emit(
            ToolCallResult(
                call_id=event.call_id,
                result=event.result,
                session_id=session_id,
                turn_id=turn_id,
            )
        )
        return True
    return False


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
    markdown_window_open = False

    async def _flush_buffer() -> None:
        nonlocal text_buffer
        if text_buffer.strip():
            if strip_md:
                text_buffer = strip_markdown(text_buffer, normalize_code_spans=True)
            payload = prepare_tts_payload(text_buffer, is_streaming=True, is_final=True)
            if payload.text.strip():
                await tts_queue.put(payload)
        text_buffer = ""

    stream: AsyncIterator[Any] | None = None
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
                        await emit_tool_event(event, kind, emit=emit)
                        if pending_tool_calls <= 0:
                            break
                    elif kind == "tool_started":
                        pending_tool_calls += 1
                        await emit_tool_event(event, kind, emit=emit)
                    elif kind == "tool_delta":
                        await emit_tool_event(event, kind, emit=emit)
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

                    # Markdown stripping is regex-heavy and sentence splitting scans the
                    # whole pending window.  Do not repeat that work for tiny deltas
                    # that cannot complete a sentence.  If markdown is already known
                    # to be open, punctuation inside the open span is also not enough;
                    # wait until a plausible markdown closer arrives before rechecking.
                    if markdown_window_open:
                        if not any(ch in event.text for ch in _MARKDOWN_RECHECK_CHARS):
                            continue
                    elif not any(ch in event.text for ch in _STREAMING_SENTENCE_TRIGGER_CHARS):
                        continue

                    markdown_window_open = has_unclosed_markdown_delimiters(text_buffer)
                    if markdown_window_open:
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
                await emit_tool_event(event, kind, emit=emit)
            elif kind == "tool_delta":
                await emit_tool_event(event, kind, emit=emit)
            elif kind == "tool_result":
                pending_tool_calls = max(0, pending_tool_calls - 1)
                await emit_tool_event(event, kind, emit=emit)
            elif kind == "done":
                if event.text:
                    if not result.text:
                        text_buffer = event.text
                    result.text = event.text
                if getattr(event, "structured_output", None) is not None:
                    result.structured_output = event.structured_output
                await _flush_buffer()
                done_received = True

    except Exception as exc:
        result.error = exc
        logger.exception("Agent streaming error")
        await emit(Error(exception=exc, stage=ErrorStage.AGENT))
    finally:
        # Defensively close the agent stream so a generator abandoned mid-
        # iteration (e.g. on barge-in/cancellation) is finalized promptly
        # rather than waiting for GC. Bridges already close their own upstream
        # connections via async with/finally on cancel; this is hygiene that
        # tightens the race window where the bridge frame is left suspended.
        if stream is not None:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
        stream_succeeded = result.error is None and (
            not cancel_token or not cancel_token.is_cancelled
        )
        if stream_succeeded:
            await _flush_buffer()
        # Sentinel to stop the TTS task.
        #
        # On a clean completion the consumer is still actively draining the
        # bounded queue, so we must guarantee delivery of the sentinel: a
        # non-blocking put could drop it while the consumer's final ``get()``
        # blocks forever waiting for the stop signal.  Use a blocking ``put``
        # here — backpressure resolves as the consumer drains.
        #
        # On cancellation / error the TTS task may have been cancelled
        # alongside this producer (barge-in), leaving no consumer to make
        # room.  A blocking put on a full queue would then hang in this
        # finally block, so fall back to a non-blocking put and swallow
        # ``QueueFull`` (the consumer is gone, so the sentinel is moot).
        if stream_succeeded:
            await tts_queue.put(None)
        else:
            try:
                tts_queue.put_nowait(None)
            except asyncio.QueueFull:
                logger.debug("tts_queue full; skipping stop sentinel (consumer already stopped)")

    return result
