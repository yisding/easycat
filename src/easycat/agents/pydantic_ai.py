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
        """Run the agent with streaming text output.

        Yields ``AgentStreamEvent`` objects — ``TEXT_DELTA`` events as the
        model generates text, followed by a single ``DONE`` event carrying
        the full accumulated response.

        PydanticAI message history is managed internally.  The *context*
        parameter (provided by EasyCat's ``AgentRunner``) is accepted for
        protocol compatibility but is not used.
        """
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
