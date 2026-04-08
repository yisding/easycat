"""Agents SDK integration: AgentRunner with streaming, context, timeout, and tracing.

Provides AgentRunner — a wrapper around any agent that adds:
- Conversation context management (history tracking)
- Configurable timeout handling
- Streaming support with text deltas and tool events
- Cooperative cancellation via CancelToken
- Tracing spans for pipeline observability
"""
# ruff: noqa: E402

from __future__ import annotations

import warnings

warnings.warn(
    "easycat.agent_runner is deprecated. Use easycat.integrations.agents bridges instead. "
    "See docs/migration-debug-first-runtime.md for migration details.",
    DeprecationWarning,
    stacklevel=2,
)

import asyncio
import enum
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from easycat.cancel import CancelToken
from easycat.timeouts import AgentTimeoutError
from easycat.tracing import Span, SpanStatus, TraceContext

logger = logging.getLogger(__name__)

# Shared constant used by AgentRunner and adapter subclasses when recording
# an interruption in message history.
INTERRUPTION_NOTE = (
    "[The user interrupted the assistant's response and may not have heard all of it.]"
)


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
        # When the inner agent manages its own history (e.g. BaseAgentAdapter
        # subclasses), defer to it instead of maintaining a shadow copy.
        self._delegates_history = hasattr(agent, "message_history") and hasattr(
            agent, "clear_history"
        )

    # ── Properties ─────────────────────────────────────────────

    @property
    def history(self) -> list[Any]:
        """Current conversation history (copies)."""
        if self._delegates_history:
            return self._agent.message_history
        return list(self._history)

    @property
    def spans(self) -> list[Span]:
        """Recorded tracing spans (copies)."""
        return list(self._spans)

    @property
    def is_streaming(self) -> bool:
        """Whether the underlying agent supports streaming."""
        return self._is_streaming

    @property
    def output_type(self) -> type | None:
        """Structured output type, delegated to the inner agent if available."""
        return getattr(self._agent, "output_type", None)

    @property
    def last_output(self) -> Any:
        """Last raw output value, delegated to the inner agent if available."""
        return getattr(self._agent, "last_output", None)

    # ── History management ─────────────────────────────────────

    def clear_history(self) -> None:
        """Clear conversation context."""
        if self._delegates_history:
            self._agent.clear_history()
        else:
            self._history.clear()

    def clear_spans(self) -> None:
        """Clear recorded tracing spans."""
        self._spans.clear()

    # ── Interruption handling ─────────────────────────────────

    def _apply_interruption(
        self, text_spoken: str = "", *, mode: Literal["truncate", "message"] = "truncate"
    ) -> None:
        """Record an interruption in the runner's own ``_history``.

        * ``mode="truncate"`` — replace the last assistant entry's content
          with *text_spoken* + ``"..."`` so the model sees only what was
          actually delivered to the user.
        * ``mode="message"`` — append an explicit ``system`` message.
        """
        if mode == "truncate":
            # Walk backwards to find the last assistant entry and truncate.
            for i in range(len(self._history) - 1, -1, -1):
                if self._history[i].get("role") == "assistant":
                    self._history[i] = {
                        "role": "assistant",
                        "content": text_spoken + "..." if text_spoken else "...",
                    }
                    break
        else:
            # Deduplicate: don't add a second note if one already follows
            # the last user message.
            for entry in reversed(self._history):
                if entry["role"] == "user":
                    break
                if entry == {"role": "system", "content": INTERRUPTION_NOTE}:
                    return
            self._history.append({"role": "system", "content": INTERRUPTION_NOTE})

    def notify_interruption(
        self,
        text_spoken: str = "",
        *,
        mode: Literal["truncate", "message"] = "truncate",
    ) -> None:
        """Record that the user interrupted the assistant's last response.

        Called by :class:`Session` after a barge-in.  Delegates to the
        underlying agent if it supports ``notify_interruption``, then
        applies the note to the runner's own history.
        """
        if hasattr(self._agent, "notify_interruption"):
            try:
                self._agent.notify_interruption(text_spoken, mode=mode)
            except Exception:
                logger.debug(
                    "Error in underlying agent.notify_interruption",
                    exc_info=True,
                )
        if not self._delegates_history:
            self._apply_interruption(text_spoken, mode=mode)

    def replace_last_assistant_text(self, text: str) -> None:
        """Replace the text content of the last assistant message in history.

        Used by the session layer to update history after post-processing
        (e.g. Markdown stripping) so that subsequent turns see the cleaned
        text rather than the raw LLM output.

        Also delegates to the wrapped agent when it exposes
        ``replace_last_assistant_text`` (e.g. :class:`BaseAgentAdapter`).
        """
        if not self._delegates_history:
            # Update AgentRunner's own history
            for entry in reversed(self._history):
                if entry.get("role") == "assistant":
                    entry["content"] = text
                    break

        # Delegate to the wrapped agent/adapter
        fn = getattr(self._agent, "replace_last_assistant_text", None)
        if callable(fn):
            fn(text)

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
        if not self._delegates_history:
            self._history.append({"role": "user", "content": text})

        span = self._record_span("agent_execution") if self._config.enable_tracing else None

        try:
            if self._config.timeout is not None:
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
            if not self._delegates_history:
                self._history.pop()
            raise err
        except Exception as exc:
            if span:
                span.set_error(exc)
                span.finish(SpanStatus.ERROR)
            if not self._delegates_history:
                self._history.pop()
            raise

        if span:
            span.finish(SpanStatus.OK)

        if not self._delegates_history:
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
        if not self._delegates_history:
            self._history.append({"role": "user", "content": text})

        if self._config.enable_tracing:
            stt_to_agent = self._record_span("stt_to_agent")
            stt_to_agent.finish(SpanStatus.OK)

        exec_span = self._record_span("agent_execution") if self._config.enable_tracing else None

        accumulated = ""
        stream: AsyncIterator[AgentStreamEvent] | None = None
        history_recorded = False

        try:
            if self._is_streaming:
                if self._delegates_history:
                    stream = self._agent.run_streaming(
                        text,
                        cancel_token=cancel_token,
                    )
                else:
                    context = list(self._history[:-1])
                    stream = self._agent.run_streaming(
                        text,
                        context=context,
                        cancel_token=cancel_token,
                    )

                async def _iter_stream() -> AsyncIterator[AgentStreamEvent]:
                    nonlocal accumulated, history_recorded
                    pending_tool_calls = 0
                    interrupted = False
                    done_received = False
                    async for event in stream:
                        if done_received:
                            continue
                        if cancel_token and cancel_token.is_cancelled:
                            if not interrupted:
                                interrupted = True
                            # Let in-flight tool calls complete before stopping
                            if pending_tool_calls > 0:
                                if event.type == AgentStreamEventType.TOOL_STARTED:
                                    pending_tool_calls += 1
                                    yield event
                                elif event.type == AgentStreamEventType.TOOL_RESULT:
                                    pending_tool_calls = max(0, pending_tool_calls - 1)
                                    yield event
                                    if pending_tool_calls <= 0:
                                        break
                                elif event.type == AgentStreamEventType.TOOL_DELTA:
                                    yield event
                                elif event.type == AgentStreamEventType.DONE:
                                    if event.text:
                                        accumulated = event.text
                                    break
                                # Skip text deltas during drain
                                continue
                            else:
                                # No tool calls in flight — stop immediately
                                break
                        if event.type == AgentStreamEventType.TEXT_DELTA:
                            accumulated += event.text
                        elif event.type == AgentStreamEventType.DONE and event.text:
                            accumulated = event.text
                        if (
                            event.type == AgentStreamEventType.DONE
                            and not self._delegates_history
                            and not history_recorded
                        ):
                            self._history.append({"role": "assistant", "content": accumulated})
                            history_recorded = True
                        if event.type == AgentStreamEventType.TOOL_STARTED:
                            pending_tool_calls += 1
                        elif event.type == AgentStreamEventType.TOOL_RESULT:
                            pending_tool_calls = max(0, pending_tool_calls - 1)
                        yield event
                        if event.type == AgentStreamEventType.DONE:
                            done_received = True

                if self._config.timeout is not None:
                    async with asyncio.timeout(self._config.timeout):
                        async for event in _iter_stream():
                            yield event
                else:
                    async for event in _iter_stream():
                        yield event
            else:
                # Non-streaming fallback: wrap run() result
                if self._config.timeout is not None:
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
            if not self._delegates_history and not history_recorded:
                self._history.pop()
            raise AgentTimeoutError(self._config.timeout or 0)
        except GeneratorExit:
            # Generator was closed by caller (e.g., barge-in) — not an error.
            # The caller is responsible for calling notify_interruption() to
            # record the interruption in history.
            if exec_span:
                exec_span.finish(SpanStatus.CANCELLED)
            if not self._delegates_history and not history_recorded:
                self._history.append({"role": "assistant", "content": accumulated})
            return
        except Exception as exc:
            if exec_span:
                exec_span.set_error(exc)
                exec_span.finish(SpanStatus.ERROR)
            if not self._delegates_history and not history_recorded:
                self._history.pop()
            raise

        interrupted = cancel_token and cancel_token.is_cancelled
        if exec_span:
            exec_span.finish(SpanStatus.CANCELLED if interrupted else SpanStatus.OK)
        if self._config.enable_tracing and not interrupted:
            agent_to_tts = self._record_span("agent_to_tts")
            agent_to_tts.finish(SpanStatus.OK)
        if not self._delegates_history and not history_recorded:
            self._history.append({"role": "assistant", "content": accumulated})
