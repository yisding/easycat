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
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from easycat.cancel import CancelToken

logger = logging.getLogger(__name__)


# ── Exceptions ──────────────────────────────────────────────────────


class AgentTimeoutError(Exception):
    """Raised when an agent invocation exceeds the configured timeout."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(f"Agent did not respond within {timeout}s")


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
    - DONE: ``text`` contains the full accumulated response (optional)
    """

    type: AgentStreamEventType
    text: str = ""
    tool_name: str = ""
    call_id: str = ""
    result: str = ""


# ── Protocols ───────────────────────────────────────────────────────


@runtime_checkable
class StreamingAgent(Protocol):
    """Agent that supports streaming text deltas and tool events.

    Implementations yield ``AgentStreamEvent`` objects as the agent produces
    output. The optional ``context`` parameter carries conversation history.
    """

    def run_streaming(
        self, text: str, *, context: list[dict[str, str]] | None = None
    ) -> AsyncIterator[AgentStreamEvent]: ...


# ── Tracing ─────────────────────────────────────────────────────────


@dataclass
class TracingSpan:
    """A timing span for pipeline stage tracing.

    Records start/end timestamps and optional metadata. Integrates with
    WS8's observability layer when available.
    """

    name: str
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float | None:
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time) * 1000

    def finish(self) -> None:
        self.end_time = time.monotonic()


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
        self._spans: list[TracingSpan] = []
        self._is_streaming = isinstance(agent, StreamingAgent)

    # ── Properties ─────────────────────────────────────────────

    @property
    def history(self) -> list[dict[str, str]]:
        """Current conversation history (copies)."""
        return list(self._history)

    @property
    def spans(self) -> list[TracingSpan]:
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

    def _record_span(self, name: str, **metadata: Any) -> TracingSpan:
        span = TracingSpan(name=name, metadata=metadata)
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
            if span:
                span.metadata["error"] = "timeout"
                span.finish()
            # Remove the user message we just added since the turn failed
            self._history.pop()
            raise AgentTimeoutError(self._config.timeout or 0)
        except Exception:
            if span:
                span.metadata["error"] = "exception"
                span.finish()
            self._history.pop()
            raise

        if span:
            span.finish()

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
            stt_to_agent.finish()

        exec_span = self._record_span("agent_execution") if self._config.enable_tracing else None

        accumulated = ""
        errored = False

        try:
            if self._is_streaming:
                context = list(self._history[:-1])
                stream = self._agent.run_streaming(text, context=context)
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
            errored = True
            if exec_span:
                exec_span.metadata["error"] = "timeout"
                exec_span.finish()
            self._history.pop()
            raise AgentTimeoutError(self._config.timeout or 0)
        except GeneratorExit:
            # Generator was closed by caller (e.g., barge-in) — not an error
            if exec_span:
                exec_span.finish()
            self._history.append({"role": "assistant", "content": accumulated})
            return
        except Exception:
            errored = True
            if exec_span:
                exec_span.metadata["error"] = "exception"
                exec_span.finish()
            self._history.pop()
            raise

        if not errored:
            if exec_span:
                exec_span.finish()
            if self._config.enable_tracing:
                agent_to_tts = self._record_span("agent_to_tts")
                agent_to_tts.finish()
            self._history.append({"role": "assistant", "content": accumulated})
