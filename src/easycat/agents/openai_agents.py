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
from typing import Any

from easycat.agent_runner import (
    INTERRUPTION_NOTE,
    AgentStreamEvent,
    AgentStreamEventType,
)
from easycat.agents.base import (
    BaseAgentAdapter,
    split_replacement_by_original_parts,
)
from easycat.cancel import CancelToken

logger = logging.getLogger(__name__)

try:
    from agents import Runner  # type: ignore[import-untyped]
except ImportError:
    Runner = None  # type: ignore[assignment,misc]


def build_openai_agents_adapter(
    *,
    name: str = "VoiceAssistant",
    instructions: str,
    tools: list[Any] | None = None,
    store: bool | None = None,
    max_turns: int | None = None,
    hooks: Any = None,
) -> OpenAIAgentsAdapter:
    """Create an OpenAI Agents SDK adapter configured for the Responses WebSocket API.

    Handles version detection and graceful fallback for ``RunConfig``,
    ``OpenAIProvider``, and ``ModelSettings``.  Raises ``SystemExit`` with a
    clear install hint if the ``agents`` package is not available.

    Parameters
    ----------
    store:
        Controls ``ModelSettings.store``.  Set to ``False`` for zero-data-
        retention deployments — this also disables ``previous_response_id``
        chaining and enables ``include=["reasoning.encrypted_content"]`` so
        that reasoning tokens are round-tripped via client-managed history.
    max_turns:
        Maximum number of LLM turns (including tool calls) per
        ``Runner.run`` invocation.
    hooks:
        ``RunHooks`` instance forwarded to every ``Runner.run`` /
        ``Runner.run_streamed`` call for lifecycle callbacks.
    """
    try:
        from agents import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "OpenAI Agents SDK is required. Install with: uv sync --extra openai-agents"
        ) from exc

    use_previous_response_id = store is not False

    run_config = None
    try:
        from agents import ModelSettings, OpenAIProvider, RunConfig  # type: ignore[import-untyped]

        reasoning = None
        try:
            from openai.types.shared import Reasoning  # type: ignore[import-untyped]

            reasoning = Reasoning(effort="none")
        except (ImportError, TypeError):
            reasoning = {"effort": "none"}

        provider = OpenAIProvider(use_responses=True, use_responses_websocket=True)

        model_settings_kwargs: dict[str, Any] = {
            "reasoning": reasoning,
            "verbosity": "low",
        }
        if store is not None:
            model_settings_kwargs["store"] = store
        if not use_previous_response_id:
            # Round-trip reasoning tokens via client-managed history
            model_settings_kwargs["include"] = ["reasoning.encrypted_content"]
        try:
            model_settings = ModelSettings(**model_settings_kwargs)
        except TypeError:
            # Older SDK version without store/include fields
            model_settings = ModelSettings(reasoning=reasoning, verbosity="low")

        run_config = RunConfig(
            model_provider=provider,
            model_settings=model_settings,
        )
    except (ImportError, TypeError) as exc:
        logger.debug("RunConfig/OpenAIProvider setup failed, falling back: %s", exc)
        try:
            from agents import set_default_openai_api  # type: ignore[import-untyped]

            set_default_openai_api("responses")
        except (ImportError, AttributeError) as exc:
            logger.debug("set_default_openai_api unavailable: %s", exc)

    agent_kwargs: dict[str, Any] = {"name": name, "instructions": instructions}
    if tools is not None:
        agent_kwargs["tools"] = tools
    voice_agent = Agent(**agent_kwargs)
    return OpenAIAgentsAdapter(
        voice_agent,
        run_config=run_config,
        use_previous_response_id=use_previous_response_id,
        max_turns=max_turns,
        hooks=hooks,
    )


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
        ``Runner.run_streamed`` call.  When ``store=False`` (zero-data-
        retention), configure
        ``ModelSettings(include=["reasoning.encrypted_content"])``
        so that reasoning tokens are round-tripped via client-managed
        history.
    context:
        Optional run context (``RunContextWrapper``) forwarded to every call.
    use_previous_response_id:
        Enable server-managed conversation state via OpenAI's
        ``previous_response_id`` response chaining.  Reduces latency and
        cost by avoiding full history resend.  Disable for zero-data-
        retention deployments where responses are not stored server-side.
        Defaults to ``True``.
    max_turns:
        Maximum number of LLM turns (including tool calls) per
        ``Runner.run`` invocation.  ``None`` uses the SDK default.
    hooks:
        ``RunHooks`` instance forwarded to every ``Runner.run`` /
        ``Runner.run_streamed`` call for lifecycle callbacks (e.g. handoff
        events, filler audio triggers).
    """

    def __init__(
        self,
        agent: Any,
        *,
        run_config: Any = None,
        context: Any = None,
        use_previous_response_id: bool = True,
        max_turns: int | None = None,
        hooks: Any = None,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._original_agent = agent
        self._run_config = run_config
        self._context = context
        self._use_previous_response_id = use_previous_response_id
        self._max_turns = max_turns
        self._hooks = hooks
        self._previous_response_id: str | None = None
        self._pending_interruption: str | None = None

    # ── History management ────────────────────────────────────

    def clear_history(self) -> None:
        """Clear conversation history, server-side state, and restore the original agent."""
        super().clear_history()
        self._agent = self._original_agent
        self._previous_response_id = None
        self._pending_interruption = None

    # ── Interruption handling ────────────────────────────────

    def _truncate_last_assistant_for_interruption(self, text_spoken: str) -> bool:
        """Try truncating the latest assistant entry in OpenAI history format."""
        replacement = self.interruption_replacement_text(text_spoken)

        # Always update local history for consistency / fallback
        local_truncated = False
        for i in range(len(self._message_history) - 1, -1, -1):
            item = self._message_history[i]
            role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
            if role == "assistant":
                if isinstance(item, dict) and "content" in item:
                    item["content"] = replacement
                    local_truncated = True
                elif not isinstance(item, dict) and hasattr(item, "content"):
                    item.content = replacement
                    local_truncated = True
                break  # Always stop at the newest assistant entry

        # With server-managed state, queue an interruption note for the next turn
        if self._use_previous_response_id and self._previous_response_id is not None:
            self._pending_interruption = (
                "[The user interrupted the assistant's response. "
                f'They approximately heard: "{replacement}"]'
            )
            return True

        return local_truncated

    def _append_interruption_note(self) -> None:
        """Append an interruption note in developer-role format."""
        if self._use_previous_response_id and self._previous_response_id is not None:
            self._pending_interruption = INTERRUPTION_NOTE
            return
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
                            output_text_parts, replacement_segments
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
        if self._use_previous_response_id and self._previous_response_id is not None:
            # Server has conversation state — only send new input
            parts: list[dict[str, str]] = []
            if self._pending_interruption is not None:
                parts.append({"role": "developer", "content": self._pending_interruption})
                self._pending_interruption = None
            parts.append({"role": "user", "content": text})
            return parts
        if self._message_history:
            return self._message_history + [{"role": "user", "content": text}]
        return text

    def _build_kwargs(self) -> dict[str, Any]:
        """Build shared keyword arguments for ``Runner.run`` / ``run_streamed``."""
        kwargs: dict[str, Any] = {}
        if self._run_config is not None:
            kwargs["run_config"] = self._run_config
        if self._context is not None:
            kwargs["context"] = self._context
        if self._use_previous_response_id:
            if self._previous_response_id is not None:
                kwargs["previous_response_id"] = self._previous_response_id
            kwargs["auto_previous_response_id"] = True
        if self._max_turns is not None:
            kwargs["max_turns"] = self._max_turns
        if self._hooks is not None:
            kwargs["hooks"] = self._hooks
        return kwargs

    # ── Basic Agent protocol ──────────────────────────────────

    async def run(self, text: str) -> str:
        """Invoke the agent and return the full response as a string."""
        if Runner is None:
            raise ImportError(
                "openai-agents package is required: pip install 'easycat[openai-agents]'"
            )

        input_data = self._build_input(text)
        kwargs = self._build_kwargs()

        result = await Runner.run(self._agent, input_data, **kwargs)
        self._pending_interruption = None
        self._message_history = result.to_input_list()

        if self._use_previous_response_id:
            self._previous_response_id = getattr(result, "last_response_id", None)

        last_agent = getattr(result, "last_agent", None)
        if last_agent is not None and last_agent is not self._agent:
            self._agent = last_agent

        return self.serialize_and_store_output(result.final_output)

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
        kwargs = self._build_kwargs()

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
            self._pending_interruption = None
            self._message_history = result.to_input_list()
            if self._use_previous_response_id:
                self._previous_response_id = getattr(result, "last_response_id", None)
            last_agent = getattr(result, "last_agent", None)
            if last_agent is not None and last_agent is not self._agent:
                self._agent = last_agent

        # Capture structured output when available
        raw_output = getattr(result, "final_output", None)

        yield self.done_event(text=accumulated, raw_output=raw_output)


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
