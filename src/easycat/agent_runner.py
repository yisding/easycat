"""Agents SDK integration: AgentRunner with streaming, context, timeout, and tracing.

Provides AgentRunner — a wrapper around any agent that adds:
- Conversation context management (history tracking)
- Configurable timeout handling
- Streaming support with text deltas and tool events
- Cooperative cancellation via CancelToken
- Tracing spans for pipeline observability
"""

from __future__ import annotations

import asyncio
import enum
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from easycat.cancel import CancelToken
from easycat.timeouts import AgentTimeoutError
from easycat.tracing import Span, SpanStatus, TraceContext

logger = logging.getLogger(__name__)


# ── Stream event types ──────────────────────────────────────────────


class AgentStreamEventType(enum.Enum):
    TEXT_DELTA = "text_delta"
    TOOL_STARTED = "tool_started"
    TOOL_DELTA = "tool_delta"
    TOOL_RESULT = "tool_result"
    DONE = "done"


@dataclass(frozen=True)
class AgentStreamEvent:
    """Event produced by a streaming agent run.

    Fields are overloaded per event type:
    - TEXT_DELTA: ``text`` contains the delta string
    - TOOL_STARTED: ``tool_name`` and ``call_id``
    - TOOL_DELTA: ``call_id`` and ``text`` (delta content)
    - TOOL_RESULT: ``call_id`` and ``result``
    - DONE: ``text`` contains the full accumulated response (optional),
      ``structured_output`` carries the raw typed output when the agent
      uses a structured ``output_type`` (e.g. a Pydantic model).
    """

    type: AgentStreamEventType
    text: str = ""
    tool_name: str = ""
    call_id: str = ""
    result: str = ""
    structured_output: Any = None


# ── Protocols ───────────────────────────────────────────────────────


@runtime_checkable
class StreamingAgent(Protocol):
    """Agent that supports streaming text deltas and tool events.

    Implementations yield ``AgentStreamEvent`` objects as the agent produces
    output. The optional ``context`` parameter carries conversation history.
    ``cancel_token`` supports cooperative cancellation.
    """

    def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]: ...


# ── Configuration ───────────────────────────────────────────────────


@dataclass
class AgentRunnerConfig:
    """Configuration for AgentRunner."""

    timeout: float | None = 30.0
    enable_tracing: bool = True


# ── AgentRunner ─────────────────────────────────────────────────────


class AgentRunner:
    """Wraps an agent with context management, timeout, cancellation, and tracing.

    Supports both basic agents (``Agent`` protocol with ``run()``) and streaming
    agents (``StreamingAgent`` protocol with ``run_streaming()``). Maintains
    conversation history across turns and records tracing spans.

    Usage::

        # Basic (non-streaming) agent
        runner = AgentRunner(my_agent)
        response = await runner.run("Hello")

        # Streaming agent
        runner = AgentRunner(my_streaming_agent)
        async for event in runner.run_streaming("Hello"):
            if event.type == AgentStreamEventType.TEXT_DELTA:
                print(event.text, end="")
    """

    def __init__(
        self,
        agent: Any,
        config: AgentRunnerConfig | None = None,
    ) -> None:
        self._agent = agent
        self._config = config or AgentRunnerConfig()
        self._history: list[dict[str, str]] = []
        self._spans: list[Span] = []
        self._trace_context = TraceContext()
        self._is_streaming = isinstance(agent, StreamingAgent)

    # ── Properties ─────────────────────────────────────────────

    @property
    def history(self) -> list[dict[str, str]]:
        """Current conversation history (copies)."""
        return list(self._history)

    @property
    def spans(self) -> list[Span]:
        """Recorded tracing spans (copies)."""
        return list(self._spans)

    @property
    def is_streaming(self) -> bool:
        """Whether the underlying agent supports streaming."""
        return self._is_streaming

    # ── History management ─────────────────────────────────────

    def clear_history(self) -> None:
        """Clear conversation context."""
        self._history.clear()

    def clear_spans(self) -> None:
        """Clear recorded tracing spans."""
        self._spans.clear()

    # ── Internal helpers ───────────────────────────────────────

    def _record_span(self, name: str, **metadata: Any) -> Span:
        span = self._trace_context.create_span(name=name, **metadata)
        self._spans.append(span)
        return span

    # ── Basic run (Agent protocol) ─────────────────────────────

    async def run(self, text: str) -> str:
        """Invoke the agent and return the full response text.

        Handles timeout, records conversation history, and creates tracing spans.
        Satisfies the basic ``Agent`` protocol so AgentRunner can be used as a
        drop-in replacement wherever an Agent is expected.
        """
        self._history.append({"role": "user", "content": text})

        span = self._record_span("agent_execution") if self._config.enable_tracing else None

        try:
            if self._config.timeout:
                response = await asyncio.wait_for(
                    self._agent.run(text),
                    timeout=self._config.timeout,
                )
            else:
                response = await self._agent.run(text)
        except TimeoutError:
            err = AgentTimeoutError(self._config.timeout or 0)
            if span:
                span.set_error(err)
                span.finish(SpanStatus.ERROR)
            # Remove the user message we just added since the turn failed
            self._history.pop()
            raise err
        except Exception as exc:
            if span:
                span.set_error(exc)
                span.finish(SpanStatus.ERROR)
            self._history.pop()
            raise

        if span:
            span.finish(SpanStatus.OK)

        self._history.append({"role": "assistant", "content": response})
        return response

    # ── Streaming run ──────────────────────────────────────────

    async def run_streaming(
        self,
        text: str,
        *,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        """Run the agent with streaming output.

        Yields ``AgentStreamEvent`` objects for text deltas, tool events, and
        a final done event. Handles cancellation via ``cancel_token``, timeout,
        and conversation context.

        If the underlying agent doesn't support streaming, falls back to
        wrapping the basic ``run()`` result as a single text delta + done event.
        """
        self._history.append({"role": "user", "content": text})

        if self._config.enable_tracing:
            stt_to_agent = self._record_span("stt_to_agent")
            stt_to_agent.finish(SpanStatus.OK)

        exec_span = self._record_span("agent_execution") if self._config.enable_tracing else None

        accumulated = ""
        stream: AsyncIterator[AgentStreamEvent] | None = None

        try:
            if self._is_streaming:
                context = list(self._history[:-1])
                stream = self._agent.run_streaming(
                    text,
                    context=context,
                    cancel_token=cancel_token,
                )

                async def _iter_stream() -> AsyncIterator[AgentStreamEvent]:
                    nonlocal accumulated
                    async for event in stream:
                        if cancel_token and cancel_token.is_cancelled:
                            # Try to close the stream gracefully
                            if hasattr(stream, "aclose"):
                                await stream.aclose()
                            break
                        if event.type == AgentStreamEventType.TEXT_DELTA:
                            accumulated += event.text
                        elif event.type == AgentStreamEventType.DONE and event.text:
                            accumulated = event.text
                        yield event

                if self._config.timeout:
                    async with asyncio.timeout(self._config.timeout):
                        async for event in _iter_stream():
                            yield event
                else:
                    async for event in _iter_stream():
                        yield event
            else:
                # Non-streaming fallback: wrap run() result
                if self._config.timeout:
                    response = await asyncio.wait_for(
                        self._agent.run(text),
                        timeout=self._config.timeout,
                    )
                else:
                    response = await self._agent.run(text)

                accumulated = response

                if not (cancel_token and cancel_token.is_cancelled):
                    yield AgentStreamEvent(
                        type=AgentStreamEventType.TEXT_DELTA,
                        text=response,
                    )
                    yield AgentStreamEvent(
                        type=AgentStreamEventType.DONE,
                        text=response,
                    )
        except TimeoutError:
            if stream and hasattr(stream, "aclose"):
                await stream.aclose()
            if exec_span:
                exec_span.set_error(AgentTimeoutError(self._config.timeout or 0))
                exec_span.finish(SpanStatus.ERROR)
            self._history.pop()
            raise AgentTimeoutError(self._config.timeout or 0)
        except GeneratorExit:
            # Generator was closed by caller (e.g., barge-in) — not an error
            if exec_span:
                exec_span.finish(SpanStatus.CANCELLED)
            self._history.append({"role": "assistant", "content": accumulated})
            return
        except Exception as exc:
            if exec_span:
                exec_span.set_error(exc)
                exec_span.finish(SpanStatus.ERROR)
            self._history.pop()
            raise

        if exec_span:
            exec_span.finish(SpanStatus.OK)
        if self._config.enable_tracing:
            agent_to_tts = self._record_span("agent_to_tts")
            agent_to_tts.finish(SpanStatus.OK)
        self._history.append({"role": "assistant", "content": accumulated})
