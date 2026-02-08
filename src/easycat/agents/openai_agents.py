"""OpenAI Agents SDK adapter for the EasyCat voice pipeline.

Wraps an ``agents.Agent`` (from the ``openai-agents`` package) so it can be
used directly as the ``agent`` parameter in :class:`easycat.SessionConfig`.
Satisfies both the basic ``Agent`` protocol (``run()``) and the
``StreamingAgent`` protocol (``run_streaming()``) expected by
:class:`easycat.Session`.

Conversation history is managed internally via ``to_input_list()``.  Streaming
produces ``TEXT_DELTA`` events for incremental TTS and ``TOOL_STARTED`` /
``TOOL_RESULT`` events for pipeline observability.

Usage::

    from agents import Agent
    from easycat.agents.openai_agents import OpenAIAgentsAdapter
    from easycat import Session, SessionConfig

    agent = Agent(
        name="Assistant",
        instructions="You are a helpful voice assistant.",
    )
    adapter = OpenAIAgentsAdapter(agent)
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

try:
    from agents import Runner  # type: ignore[import-untyped]
except ImportError:
    Runner = None  # type: ignore[assignment,misc]


class OpenAIAgentsAdapter(BaseAgentAdapter):
    """Wraps an OpenAI Agents SDK ``Agent`` for use with EasyCat's ``Session``.

    Implements both the basic ``Agent`` protocol (``run(text) -> str``) and the
    ``StreamingAgent`` protocol (``run_streaming(...)``).  Conversation history
    is stored internally via ``RunResult.to_input_list()`` so multi-turn
    conversations work without manual wiring.

    Parameters
    ----------
    agent:
        An ``agents.Agent`` instance.
    run_config:
        Optional ``RunConfig`` forwarded to every ``Runner.run`` /
        ``Runner.run_streamed`` call.
    context:
        Optional run context (``RunContextWrapper``) forwarded to every call.
    """

    def __init__(
        self,
        agent: Any,
        *,
        run_config: Any = None,
        context: Any = None,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._run_config = run_config
        self._context = context

    # ── Helpers ────────────────────────────────────────────────

    def _build_input(self, text: str) -> Any:
        """Build the ``input`` parameter for Runner, appending to history."""
        if self._message_history:
            return self._message_history + [{"role": "user", "content": text}]
        return text

    # ── Basic Agent protocol ──────────────────────────────────

    async def run(self, text: str) -> str:
        """Invoke the agent and return the full response as a string."""
        if Runner is None:
            raise ImportError(
                "openai-agents package is required: pip install 'easycat[openai-agents]'"
            )

        input_data = self._build_input(text)
        kwargs: dict[str, Any] = {}
        if self._run_config is not None:
            kwargs["run_config"] = self._run_config
        if self._context is not None:
            kwargs["context"] = self._context

        result = await Runner.run(self._agent, input_data, **kwargs)
        self._message_history = result.to_input_list()
        return str(result.final_output)

    # ── StreamingAgent protocol ───────────────────────────────

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        """Run the agent with streaming output.

        Yields ``AgentStreamEvent`` objects:

        - ``TEXT_DELTA`` for each chunk of generated text (for incremental TTS)
        - ``TOOL_STARTED`` / ``TOOL_RESULT`` for tool-call observability
        - ``DONE`` with the full accumulated text when finished

        The *context* parameter from EasyCat's ``AgentRunner`` is accepted for
        protocol compatibility but is not used — the adapter manages its own
        history via ``to_input_list()``.
        """
        if Runner is None:
            raise ImportError(
                "openai-agents package is required: pip install 'easycat[openai-agents]'"
            )

        input_data = self._build_input(text)
        kwargs: dict[str, Any] = {}
        if self._run_config is not None:
            kwargs["run_config"] = self._run_config
        if self._context is not None:
            kwargs["context"] = self._context

        result = Runner.run_streamed(self._agent, input_data, **kwargs)

        accumulated = ""
        async for event in result.stream_events():
            if cancel_token and cancel_token.is_cancelled:
                break

            if event.type == "raw_response_event":
                delta = _extract_text_delta(event.data)
                if delta:
                    accumulated += delta
                    yield AgentStreamEvent(
                        type=AgentStreamEventType.TEXT_DELTA,
                        text=delta,
                    )
            elif event.type == "run_item_stream_event":
                agent_event = _map_run_item_event(event.item)
                if agent_event is not None:
                    yield agent_event

        self._message_history = result.to_input_list()

        yield AgentStreamEvent(
            type=AgentStreamEventType.DONE,
            text=accumulated,
        )


# ── Event mapping helpers (module-level, testable) ────────────────


def _extract_text_delta(data: Any) -> str:
    """Extract a text delta string from a raw Responses API event.

    Works with ``ResponseTextDeltaEvent`` objects that have a ``.delta``
    attribute.  Returns an empty string for non-text events.
    """
    # ResponseTextDeltaEvent has .type == "response.output_text.delta"
    # and .delta containing the text chunk.
    event_type = getattr(data, "type", "")
    if event_type == "response.output_text.delta":
        return getattr(data, "delta", "") or ""
    return ""


def _map_run_item_event(item: Any) -> AgentStreamEvent | None:
    """Map a ``RunItem`` to an ``AgentStreamEvent``, or ``None`` to skip."""
    item_type = getattr(item, "type", "")

    if item_type == "tool_call_item":
        raw = getattr(item, "raw_item", None)
        return AgentStreamEvent(
            type=AgentStreamEventType.TOOL_STARTED,
            tool_name=getattr(raw, "name", "") or "",
            call_id=getattr(raw, "call_id", "") or "",
        )

    if item_type == "tool_call_output_item":
        raw = getattr(item, "raw_item", None)
        return AgentStreamEvent(
            type=AgentStreamEventType.TOOL_RESULT,
            call_id=getattr(raw, "call_id", "") or "",
            result=str(getattr(item, "output", "")) if hasattr(item, "output") else "",
        )

    return None
