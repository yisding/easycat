"""PydanticAI adapter for the EasyCat voice pipeline.

Wraps a ``pydantic_ai.Agent`` so it can be used directly as the ``agent``
parameter in :class:`easycat.SessionConfig`.  Satisfies both the basic
``Agent`` protocol (``run()``) and the ``StreamingAgent`` protocol
(``run_streaming()``) expected by :class:`easycat.Session`.

PydanticAI message history is managed internally so multi-turn conversations
work out of the box.

Usage::

    from pydantic_ai import Agent as PydanticAgent
    from easycat.agents.pydantic_ai import PydanticAIAdapter
    from easycat import Session, SessionConfig

    pydantic_agent = PydanticAgent(
        "openai:gpt-4o",
        system_prompt="You are a helpful voice assistant.",
    )
    adapter = PydanticAIAdapter(pydantic_agent)
    session = Session(SessionConfig(agent=adapter, ...))
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from easycat.agent_runner import AgentStreamEvent, AgentStreamEventType
from easycat.agents.base import BaseAgentAdapter
from easycat.cancel import CancelToken

logger = logging.getLogger(__name__)


class PydanticAIAdapter(BaseAgentAdapter):
    """Wraps a PydanticAI ``Agent`` for use with EasyCat's ``Session``.

    Implements both the basic ``Agent`` protocol (``run(text) -> str``) and the
    ``StreamingAgent`` protocol (``run_streaming(...)``).  PydanticAI's own
    message history is stored internally so multi-turn conversations work
    without any manual message passing.

    When the agent supports the ``iter()`` API, streaming includes tool
    events (``TOOL_STARTED``, ``TOOL_DELTA``, ``TOOL_RESULT``) alongside
    text deltas.  Falls back to ``run_stream()`` for text-only streaming
    on older PydanticAI versions.

    Parameters
    ----------
    agent:
        A ``pydantic_ai.Agent`` instance.
    deps:
        Optional dependencies forwarded to every PydanticAI ``run`` /
        ``run_stream`` call.  Must match the agent's ``deps_type``.
    model_settings:
        Optional ``ModelSettings`` override applied to every call.
    """

    def __init__(
        self,
        agent: Any,
        *,
        deps: Any = None,
        model_settings: Any = None,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._deps = deps
        self._model_settings = model_settings

    # ── Basic Agent protocol ──────────────────────────────────

    async def run(self, text: str) -> str:
        """Invoke the agent and return the full response as a string."""
        result = await self._agent.run(
            text,
            message_history=self._message_history or None,
            deps=self._deps,
            model_settings=self._model_settings,
        )
        self._message_history = result.new_messages()
        return str(result.output)

    # ── StreamingAgent protocol ───────────────────────────────

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        """Run the agent with streaming output.

        Uses PydanticAI's ``iter()`` API to stream both text and tool events.
        Falls back to ``run_stream()`` (text-only) when ``iter()`` is not
        available.

        PydanticAI message history is managed internally.  The *context*
        parameter (provided by EasyCat's ``AgentRunner``) is accepted for
        protocol compatibility but is not used.
        """
        if hasattr(self._agent, "iter"):
            async for event in self._stream_via_iter(text, cancel_token):
                yield event
        else:
            async for event in self._stream_via_run_stream(text, cancel_token):
                yield event

    # ── iter()-based streaming (text + tools) ─────────────────

    async def _stream_via_iter(
        self,
        text: str,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[AgentStreamEvent]:
        """Stream using ``agent.iter()`` — full text + tool event support."""
        accumulated = ""

        async with self._agent.iter(
            text,
            message_history=self._message_history or None,
            deps=self._deps,
            model_settings=self._model_settings,
        ) as agent_run:
            async for node in agent_run:
                if cancel_token and cancel_token.is_cancelled:
                    break

                # Both ModelRequestNode and CallToolsNode expose stream()
                if not hasattr(node, "stream"):
                    continue

                async with node.stream(agent_run.ctx) as stream:
                    async for event in stream:
                        if cancel_token and cancel_token.is_cancelled:
                            break
                        mapped = _map_pydantic_event(event)
                        if mapped is not None:
                            if mapped.type == AgentStreamEventType.TEXT_DELTA:
                                accumulated += mapped.text
                            yield mapped

            self._message_history = agent_run.new_messages()

        yield AgentStreamEvent(
            type=AgentStreamEventType.DONE,
            text=accumulated,
        )

    # ── run_stream()-based streaming (text only, fallback) ────

    async def _stream_via_run_stream(
        self,
        text: str,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[AgentStreamEvent]:
        """Stream using ``agent.run_stream()`` — text deltas only."""
        async with self._agent.run_stream(
            text,
            message_history=self._message_history or None,
            deps=self._deps,
            model_settings=self._model_settings,
        ) as result:
            accumulated = ""
            async for full_text in result.stream_text():
                if cancel_token and cancel_token.is_cancelled:
                    break
                delta = full_text[len(accumulated) :]
                if delta:
                    yield AgentStreamEvent(
                        type=AgentStreamEventType.TEXT_DELTA,
                        text=delta,
                    )
                accumulated = full_text

            self._message_history = result.new_messages()

        yield AgentStreamEvent(
            type=AgentStreamEventType.DONE,
            text=accumulated,
        )


# ── Event mapping helpers ─────────────────────────────────────────


def _map_pydantic_event(event: Any) -> AgentStreamEvent | None:
    """Map a PydanticAI streaming event to an EasyCat ``AgentStreamEvent``.

    Uses duck typing (class-name checks) so this works without importing
    PydanticAI types.  Returns ``None`` for events that don't map to
    EasyCat events (e.g. ``PartStartEvent``, ``ThinkingPartDelta``).
    """
    event_cls = type(event).__name__

    # PartDeltaEvent → TEXT_DELTA or TOOL_DELTA
    delta = getattr(event, "delta", None)
    if delta is not None:
        delta_cls = type(delta).__name__
        if delta_cls == "TextPartDelta":
            content = getattr(delta, "content_delta", "") or ""
            if content:
                return AgentStreamEvent(
                    type=AgentStreamEventType.TEXT_DELTA,
                    text=content,
                )
        elif delta_cls == "ToolCallPartDelta":
            args = getattr(delta, "args_delta", "") or ""
            if args:
                return AgentStreamEvent(
                    type=AgentStreamEventType.TOOL_DELTA,
                    text=args,
                )

    # FunctionToolCallEvent → TOOL_STARTED
    if event_cls == "FunctionToolCallEvent":
        part = getattr(event, "part", None)
        return AgentStreamEvent(
            type=AgentStreamEventType.TOOL_STARTED,
            tool_name=getattr(part, "tool_name", "") or "",
            call_id=getattr(part, "tool_call_id", "") or "",
        )

    # FunctionToolResultEvent → TOOL_RESULT
    if event_cls == "FunctionToolResultEvent":
        return AgentStreamEvent(
            type=AgentStreamEventType.TOOL_RESULT,
            call_id=getattr(event, "tool_call_id", "") or "",
            result=str(getattr(event, "result", "")) if hasattr(event, "result") else "",
        )

    return None
