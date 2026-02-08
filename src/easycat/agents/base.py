"""Base class for agent framework adapters.

Provides shared infrastructure so that adapters for different agent
frameworks (PydanticAI, OpenAI Agents SDK, etc.) have a consistent
interface and don't duplicate boilerplate.

Subclasses must implement :meth:`run` and :meth:`run_streaming`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from easycat.agent_runner import AgentStreamEvent
from easycat.cancel import CancelToken

logger = logging.getLogger(__name__)


class BaseAgentAdapter:
    """Shared base for agent framework adapters.

    Handles message-history storage and the ``clear_history`` /
    ``message_history`` interface that :class:`easycat.Session` relies on.

    Subclasses implement framework-specific ``run()`` and
    ``run_streaming()`` methods while inheriting the history plumbing.
    """

    def __init__(self) -> None:
        self._message_history: list[Any] = []

    # ── History management ────────────────────────────────────

    def clear_history(self) -> None:
        """Clear the internal conversation history."""
        self._message_history.clear()

    @property
    def message_history(self) -> list[Any]:
        """Return a copy of the current message history."""
        return list(self._message_history)

    # ── Protocol methods (subclasses must override) ───────────

    async def run(self, text: str) -> str:
        """Invoke the agent and return the full response text."""
        raise NotImplementedError

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        """Run the agent with streaming output.

        Yields ``AgentStreamEvent`` objects (TEXT_DELTA, TOOL_*, DONE).
        """
        raise NotImplementedError
        yield  # pragma: no cover – makes this a valid async generator
