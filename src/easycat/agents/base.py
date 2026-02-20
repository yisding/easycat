"""Base class for agent framework adapters.

Provides shared infrastructure so that adapters for different agent
frameworks (PydanticAI, OpenAI Agents SDK, etc.) have a consistent
interface and don't duplicate boilerplate.

Subclasses must implement :meth:`run` and :meth:`run_streaming`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from easycat.agent_runner import AgentStreamEvent
from easycat.cancel import CancelToken

logger = logging.getLogger(__name__)


def serialize_output(output: Any) -> str:
    """Serialize an agent output value to a human-/machine-readable string.

    Handles Pydantic models (v1 and v2), dicts, lists, and plain values.
    Prefers JSON serialization for structured types so the result is valid
    JSON rather than a Python repr.

    - ``str`` → returned as-is
    - Pydantic v2 model (has ``model_dump_json``) → JSON string
    - Pydantic v1 model (has ``json`` method) → JSON string
    - ``dict`` / ``list`` → ``json.dumps``
    - anything else → ``str()``
    """
    if isinstance(output, str):
        return output
    # Pydantic v2
    if hasattr(output, "model_dump_json"):
        return output.model_dump_json()
    # Pydantic v1
    if hasattr(output, "json") and callable(output.json):
        return output.json()
    # dict / list → JSON
    if isinstance(output, (dict, list)):
        return json.dumps(output, default=str)
    return str(output)


class BaseAgentAdapter:
    """Shared base for agent framework adapters.

    Handles message-history storage, output tracking, and the
    ``clear_history`` / ``message_history`` interface that
    :class:`easycat.Session` relies on.

    Subclasses implement framework-specific ``run()`` and
    ``run_streaming()`` methods while inheriting the shared plumbing.
    """

    def __init__(self) -> None:
        self._message_history: list[Any] = []
        self._last_output: Any = None

    # ── History management ────────────────────────────────────

    def clear_history(self) -> None:
        """Clear the internal conversation history."""
        self._message_history.clear()
        self._last_output = None

    @property
    def message_history(self) -> list[Any]:
        """Return a copy of the current message history."""
        return list(self._message_history)

    # ── Structured output access ─────────────────────────────

    @property
    def output_type(self) -> type | None:
        """The structured output type configured on the underlying agent.

        Returns ``None`` when the agent produces plain-text output (or when
        the concept doesn't apply).  Adapters that wrap frameworks with an
        ``output_type`` parameter (PydanticAI, OpenAI Agents SDK) should
        override this to expose the configured type.
        """
        agent = getattr(self, "_agent", None)
        if agent is None:
            return None
        otype = getattr(agent, "output_type", None)
        # PydanticAI and OpenAI both default to str when no output_type is set
        if otype is str or otype is None:
            return None
        return otype

    @property
    def last_output(self) -> Any:
        """The raw output value from the most recent ``run()`` or
        ``run_streaming()`` call.

        For plain-text agents this is the response string.  For agents
        with a structured ``output_type`` this is the validated model
        instance (e.g. a Pydantic ``BaseModel``).  ``None`` before the
        first call or after ``clear_history()``.
        """
        return self._last_output

    # ── Interruption handling ────────────────────────────────

    def notify_interruption(
        self,
        text_spoken: str = "",
        *,
        mode: str = "truncate",
    ) -> None:
        """Record that the user interrupted the assistant's last response.

        Called by :class:`easycat.Session` after a barge-in when the agent
        stream has been drained (tool calls completed).

        Parameters
        ----------
        text_spoken:
            The portion of the assistant's response that was approximately
            delivered to the user before the interruption.
        mode:
            ``"truncate"`` (default) — replace the last assistant message
            with ``text_spoken + "..."`` so the model sees what was heard.
            ``"message"`` — append an explicit system/developer message
            noting the interruption (requires model support for interleaved
            system messages).

        Subclasses override to apply the note in the appropriate format
        for their framework.  The default implementation is a no-op.
        """

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
