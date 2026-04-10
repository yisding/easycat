"""Base class for agent framework adapters.

Provides shared infrastructure so that adapters for different agent
frameworks (PydanticAI, OpenAI Agents SDK, etc.) have a consistent
interface and don't duplicate boilerplate.

Subclasses must implement :meth:`run` and :meth:`run_streaming`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal

from easycat.cancel import CancelToken
from easycat.integrations.agents._legacy_types import AgentStreamEvent, AgentStreamEventType

logger = logging.getLogger(__name__)


def serialize_output(output: Any) -> str:
    """Serialize an agent output value to a human-/machine-readable string.

    Handles Pydantic models (v1 and v2), dicts, lists, and plain values.
    Prefers JSON serialization for structured types so the result is valid
    JSON rather than a Python repr.

    - ``str`` -> returned as-is
    - Pydantic v2 model (has ``model_dump_json``) -> JSON string
    - Pydantic v1 model (has ``json`` method) -> JSON string
    - ``dict`` / ``list`` -> ``json.dumps``
    - anything else -> ``str()``
    """
    if isinstance(output, str):
        return output
    # Pydantic v2
    if hasattr(output, "model_dump_json"):
        return output.model_dump_json()
    # Pydantic v1
    if hasattr(output, "json") and callable(output.json):
        return output.json()
    # dict / list -> JSON
    if isinstance(output, (dict, list)):
        return json.dumps(output, default=str)
    return str(output)


def split_replacement_by_original_parts(
    original_parts: Sequence[str],
    replacement: str,
) -> list[str]:
    """Split a replacement string across original part boundaries.

    This keeps adapter history part granularity when post-processing modifies
    the concatenated assistant text (e.g. Markdown stripping). The returned
    parts always concatenate back to ``replacement``.
    """
    if not original_parts:
        return []
    if len(original_parts) == 1:
        return [replacement]

    original_joined = "".join(original_parts)
    if not original_joined:
        return [replacement, *([""] * (len(original_parts) - 1))]

    # Greedy subsequence mapping: markdown stripping primarily removes
    # characters, so map each original index to the consumed index in the
    # replacement text.
    replacement_len = len(replacement)
    original_to_replacement = [0] * (len(original_joined) + 1)
    replacement_idx = 0
    for original_idx, ch in enumerate(original_joined):
        if replacement_idx < replacement_len and ch == replacement[replacement_idx]:
            replacement_idx += 1
        original_to_replacement[original_idx + 1] = replacement_idx

    split_points: list[int] = []
    running = 0
    for part in original_parts[:-1]:
        running += len(part)
        split_points.append(original_to_replacement[running])

    result_parts: list[str] = []
    prev = 0
    for split_at in split_points:
        bounded = max(prev, min(replacement_len, split_at))
        result_parts.append(replacement[prev:bounded])
        prev = bounded
    result_parts.append(replacement[prev:])
    return result_parts


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

    def replace_last_assistant_text(self, text: str) -> None:
        """Replace the text content of the last assistant message in history.

        Subclasses should override to handle framework-specific message
        formats.  The default implementation is a no-op because message
        history formats vary across agent frameworks.
        """

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

    def done_structured_output(self, raw_output: Any) -> Any:
        """Normalize ``DONE`` event structured output payload.

        By convention, plain-text agents should emit ``structured_output=None``
        on ``DONE`` events even though ``last_output`` is still captured.
        Structured agents (configured ``output_type``) should expose the raw
        validated output object.
        """
        if isinstance(raw_output, str) and self.output_type is None:
            return None
        return raw_output

    def interruption_replacement_text(self, text_spoken: str) -> str:
        """Format assistant text used when truncating after interruption."""
        return text_spoken + "..." if text_spoken else "..."

    def serialize_and_store_output(self, raw_output: Any) -> str:
        """Persist the most recent raw output and return its serialized text."""
        self._last_output = raw_output
        return serialize_output(raw_output)

    def done_event(self, *, text: str, raw_output: Any) -> AgentStreamEvent:
        """Build a normalized ``DONE`` stream event and persist raw output."""
        self._last_output = raw_output
        return AgentStreamEvent(
            type=AgentStreamEventType.DONE,
            text=text,
            structured_output=self.done_structured_output(raw_output),
        )

    # ── Interruption handling ────────────────────────────────

    def notify_interruption(
        self,
        text_spoken: str = "",
        *,
        mode: Literal["truncate", "message"] = "truncate",
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
            ``"truncate"`` (default) -- replace the last assistant message
            with ``text_spoken + "..."`` so the model sees what was heard.
            ``"message"`` -- append an explicit system/developer message
            noting the interruption (requires model support for interleaved
            system messages).

        Subclasses typically override ``_truncate_last_assistant_for_interruption``
        and ``_append_interruption_note`` rather than this method directly.
        """
        if mode == "truncate" and self._truncate_last_assistant_for_interruption(text_spoken):
            return
        self._append_interruption_note()

    def _truncate_last_assistant_for_interruption(self, text_spoken: str) -> bool:
        """Try to truncate the latest assistant response after interruption.

        Returns ``True`` when history was updated and no fallback message is
        required.  The default implementation returns ``False``.
        """
        return False

    def _append_interruption_note(self) -> None:
        """Append a framework-specific interruption note.

        Called when truncation is not possible or when ``mode='message'``.
        The default implementation is a no-op.
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
        yield  # pragma: no cover -- makes this a valid async generator
