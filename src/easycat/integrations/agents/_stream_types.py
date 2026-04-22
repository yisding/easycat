"""Agent stream types.

Defines the streaming event protocol, event types, and constants used
by AgentRunner, BaseAgentAdapter, and Session's streaming pipeline.
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from easycat.cancel import CancelToken

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
