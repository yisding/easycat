"""OpenAI Agents SDK adapter for the EasyCat voice pipeline.

Wraps an ``agents.Agent`` (from the ``openai-agents`` package) so it can be
used directly as the ``agent`` parameter in :class:`easycat.SessionConfig`.
Satisfies both the basic ``Agent`` protocol (``run()``) and the
``StreamingAgent`` protocol (``run_streaming()``) expected by
:class:`easycat.Session`.

Conversation history is managed internally via ``to_input_list()``.  Streaming
produces ``TEXT_DELTA`` events for incremental TTS, ``TOOL_STARTED`` /
``TOOL_DELTA`` / ``TOOL_RESULT`` events for full tool-call observability.

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
from typing import Any, Literal

from easycat.agent_runner import (
    INTERRUPTION_NOTE,
    AgentStreamEvent,
    AgentStreamEventType,
)
from easycat.agents.base import (
    BaseAgentAdapter,
    serialize_output,
    split_replacement_by_original_parts,
)
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

    # ── Interruption handling ────────────────────────────────

    def notify_interruption(
        self,
        text_spoken: str = "",
        *,
        mode: Literal["truncate", "message"] = "truncate",
    ) -> None:
        """Record an interruption in the OpenAI-format message history.

        * ``mode="truncate"`` — find the last assistant item and replace
          its text content with *text_spoken* + ``"..."``.
        * ``mode="message"`` — append a ``developer`` message.
        """
        if mode == "truncate":
            updated = False
            for i in range(len(self._message_history) - 1, -1, -1):
                item = self._message_history[i]
                role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
                if role == "assistant":
                    if isinstance(item, dict) and "content" in item:
                        item["content"] = text_spoken + "..." if text_spoken else "..."
                        updated = True
                    elif not isinstance(item, dict) and hasattr(item, "content"):
                        item.content = text_spoken + "..." if text_spoken else "..."
                        updated = True
                    break  # Always stop at the newest assistant entry
            if not updated:
                self._message_history.append({"role": "developer", "content": INTERRUPTION_NOTE})
        else:
            self._message_history.append({"role": "developer", "content": INTERRUPTION_NOTE})

    # ── History patching ─────────────────────────────────────

    def replace_last_assistant_text(self, text: str) -> None:
        """Replace the text in the last assistant message.

        ``to_input_list()`` returns dicts following the Responses API
        format. Walk backwards to find the last assistant/output message
        and patch only output-text parts while preserving part boundaries.
        """
        for item in reversed(self._message_history):
            if not isinstance(item, dict):
                break
            role = item.get("role")
            if role == "assistant":
                content = item.get("content")
                if isinstance(content, list):
                    output_text_parts: list[dict[str, Any]] = []
                    original_segments: list[str] = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            output_text_parts.append(part)
                            original_segments.append(str(part.get("text", "")))

                    if output_text_parts:
                        replacement_segments = split_replacement_by_original_parts(
                            original_segments,
                            text,
                        )
                        for part, replacement_segment in zip(
                            output_text_parts, replacement_segments, strict=False
                        ):
                            part["text"] = replacement_segment
                        return
                elif isinstance(content, str):
                    item["content"] = text
                    return
                break

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
        self._last_output = result.final_output
        return serialize_output(result.final_output)

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
        - ``TOOL_STARTED`` / ``TOOL_DELTA`` / ``TOOL_RESULT`` for tool-call observability
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
        pending_tool_calls: set[str] = set()
        interrupted = False
        try:
            async for event in result.stream_events():
                if cancel_token and cancel_token.is_cancelled:
                    if not interrupted:
                        interrupted = True
                    # Let in-flight tool calls complete before stopping
                    if pending_tool_calls:
                        if event.type == "run_item_stream_event":
                            agent_event = _map_run_item_event(event.item)
                            if agent_event is not None:
                                if agent_event.type == AgentStreamEventType.TOOL_RESULT:
                                    pending_tool_calls.discard(agent_event.call_id)
                                    yield agent_event
                                    if not pending_tool_calls:
                                        break
                                elif agent_event.type == AgentStreamEventType.TOOL_STARTED:
                                    pending_tool_calls.add(agent_event.call_id)
                                    yield agent_event
                                elif agent_event.type == AgentStreamEventType.TOOL_DELTA:
                                    yield agent_event
                        elif event.type == "raw_response_event":
                            tool_delta = _extract_tool_delta(event.data)
                            if tool_delta is not None:
                                yield tool_delta
                        # Skip text deltas during drain
                        continue
                    else:
                        break

                if event.type == "raw_response_event":
                    delta = _extract_text_delta(event.data)
                    if delta:
                        accumulated += delta
                        yield AgentStreamEvent(
                            type=AgentStreamEventType.TEXT_DELTA,
                            text=delta,
                        )
                    else:
                        tool_delta = _extract_tool_delta(event.data)
                        if tool_delta is not None:
                            yield tool_delta
                elif event.type == "run_item_stream_event":
                    agent_event = _map_run_item_event(event.item)
                    if agent_event is not None:
                        if agent_event.type == AgentStreamEventType.TOOL_STARTED:
                            pending_tool_calls.add(agent_event.call_id)
                        elif agent_event.type == AgentStreamEventType.TOOL_RESULT:
                            pending_tool_calls.discard(agent_event.call_id)
                        yield agent_event
        finally:
            self._message_history = result.to_input_list()

        # Capture structured output when available
        raw_output = getattr(result, "final_output", None)
        self._last_output = raw_output

        # Only expose structured_output when it is actually structured (non-str)
        # or when an explicit output_type is configured on the adapter.
        if isinstance(raw_output, str) and self.output_type is None:
            structured_output = None
        else:
            structured_output = raw_output

        yield AgentStreamEvent(
            type=AgentStreamEventType.DONE,
            text=accumulated,
            structured_output=structured_output,
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


def _extract_tool_delta(data: Any) -> AgentStreamEvent | None:
    """Extract a tool-call argument delta from a raw Responses API event.

    Works with ``ResponseFunctionCallArgumentsDeltaEvent`` objects that have
    ``type == "response.function_call_arguments.delta"`` and a ``.delta``
    attribute containing the argument string chunk.  Returns ``None`` for
    non-matching events.
    """
    event_type = getattr(data, "type", "")
    if event_type == "response.function_call_arguments.delta":
        delta = getattr(data, "delta", "") or ""
        if delta:
            return AgentStreamEvent(
                type=AgentStreamEventType.TOOL_DELTA,
                text=delta,
                call_id=getattr(data, "call_id", "") or getattr(data, "item_id", "") or "",
            )
    return None


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
