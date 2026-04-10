"""BridgeAdapterShim — dual-interface wrapper for ExternalAgentBridge.

Inherits from ``BaseAgentAdapter`` so that Session, AgentRunner, and all
existing tests see the legacy adapter surface (``run_streaming``,
``notify_interruption``, ``clear_history``).  Internally delegates to the
bridge's ``invoke()`` / ``apply_interruption()`` / ``reset()`` methods
and records execution state to the journal via ``JournalAgentRecorder``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.integrations.agents._base_adapter import BaseAgentAdapter
from easycat.integrations.agents._legacy_types import AgentStreamEvent, AgentStreamEventType
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentTurnInput,
    CancellationMode,
    ExternalAgentBridge,
    RecorderContext,
)

if TYPE_CHECKING:
    from easycat.runtime.artifacts import ArtifactStore
    from easycat.runtime.journal import ExecutionJournal

logger = logging.getLogger(__name__)

# Map AgentBridgeEvent.kind → AgentStreamEventType for Session consumption.
_BRIDGE_TO_STREAM: dict[str, AgentStreamEventType] = {
    "text_delta": AgentStreamEventType.TEXT_DELTA,
    "tool_started": AgentStreamEventType.TOOL_STARTED,
    "tool_delta": AgentStreamEventType.TOOL_DELTA,
    "tool_result": AgentStreamEventType.TOOL_RESULT,
    "done": AgentStreamEventType.DONE,
}


def _translate_bridge_to_stream(event: AgentBridgeEvent) -> AgentStreamEvent | None:
    """Map an ``AgentBridgeEvent`` to the legacy ``AgentStreamEvent``.

    Cursor/handoff/state events are consumed by the recorder and not
    surfaced to the Session streaming path — they return ``None``.
    """
    stream_type = _BRIDGE_TO_STREAM.get(event.kind)
    if stream_type is None:
        return None
    return AgentStreamEvent(
        type=stream_type,
        text=event.text,
        tool_name=event.tool_name,
        call_id=event.call_id,
        result=event.result,
        structured_output=event.structured_output,
    )


class BridgeAdapterShim(BaseAgentAdapter):
    """Wraps an ``ExternalAgentBridge`` in the legacy adapter interface.

    Session calls ``run_streaming()`` → the shim translates to
    ``bridge.invoke()``, mapping ``AgentBridgeEvent`` →
    ``AgentStreamEvent``.  Journal writes happen inside the bridge via
    the ``AgentRecorder`` the shim creates per invocation.
    """

    def __init__(
        self,
        bridge: ExternalAgentBridge,
        *,
        journal: ExecutionJournal | None = None,
        artifact_store: ArtifactStore | None = None,
        session_id: str = "",
        mcp_servers: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self._bridge = bridge
        self._journal = journal
        self._artifact_store = artifact_store
        self._session_id = session_id
        self._mcp_servers = mcp_servers
        self._active_turn_id: str | None = None
        self._last_turn_id: str | None = None

    @property
    def bridge(self) -> ExternalAgentBridge:
        """Access the wrapped bridge."""
        return self._bridge

    def set_active_turn_id(self, turn_id: str) -> None:
        """Set the turn ID for the next invocation.

        Called by Session before ``run_streaming()`` so that bridge journal
        records share the same turn_id as the rest of the session.
        """
        self._active_turn_id = turn_id

    # ── Legacy adapter interface ─────────────────────────────────

    async def run(self, text: str) -> str:
        accumulated = ""
        async for event in self.run_streaming(text):
            if event.type == AgentStreamEventType.TEXT_DELTA:
                accumulated += event.text
            elif event.type == AgentStreamEventType.DONE:
                accumulated = event.text or accumulated
        return accumulated

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        # Prefer the turn ID set by Session (via set_active_turn_id) so
        # bridge records are attributed to the same turn as the rest of
        # the session.  Fall back to a generated ID for standalone use.
        turn_id = self._active_turn_id or f"turn-{uuid4().hex[:8]}"
        self._active_turn_id = None  # consumed
        self._last_turn_id = turn_id
        turn_input = AgentTurnInput.from_text(text, context=context, turn_id=turn_id)
        recorder = self._make_recorder(turn_id)

        accumulated = ""
        async for bridge_event in self._bridge.invoke(turn_input, recorder, cancel_token):
            stream_event = _translate_bridge_to_stream(bridge_event)
            if stream_event is not None:
                if stream_event.type == AgentStreamEventType.TEXT_DELTA:
                    accumulated += stream_event.text
                elif stream_event.type == AgentStreamEventType.DONE:
                    self._last_output = stream_event.structured_output
                yield stream_event

        # Maintain shadow history for AgentRunner compatibility.
        self._message_history.append({"role": "user", "content": text})
        self._message_history.append({"role": "assistant", "content": accumulated})

    def replace_last_assistant_text(self, text: str) -> None:
        """Update the last assistant message in shadow history.

        Called by Session after markdown stripping so that subsequent
        turns are conditioned on the cleaned text the user actually heard.
        """
        if self._message_history and self._message_history[-1].get("role") == "assistant":
            self._message_history[-1]["content"] = text

    def _truncate_last_assistant_for_interruption(self, text_spoken: str) -> bool:
        recorder = self._make_recorder(self._last_turn_id)
        self._bridge.apply_interruption(
            text_spoken,
            CancellationMode.IMMEDIATE_STOP,
            recorder=recorder,
        )
        # Update shadow history.
        if self._message_history and self._message_history[-1].get("role") == "assistant":
            replacement = self.interruption_replacement_text(text_spoken)
            self._message_history[-1]["content"] = replacement
        return True

    def _append_interruption_note(self) -> None:
        """Append an interruption note for message-mode barge-in.

        Called when ``interruption_mode='message'``.  In message mode we
        do NOT call ``bridge.apply_interruption`` because that would
        blank the assistant message in the bridge's own history.  Instead
        we only append a note to the shadow history so the next turn
        sees the interruption signal without losing what was actually said.
        """
        self._message_history.append(
            {
                "role": "system",
                "content": "[The user interrupted the assistant's response.]",
            }
        )

    async def aclose(self) -> None:
        """Close the underlying bridge if it supports it."""
        if hasattr(self._bridge, "aclose"):
            await self._bridge.aclose()

    def clear_history(self) -> None:
        super().clear_history()
        self._bridge.reset()

    # ── Internal ─────────────────────────────────────────────────

    def _make_recorder(self, turn_id: str | None = None) -> JournalAgentRecorder:
        return JournalAgentRecorder(
            journal=self._journal,
            artifact_store=self._artifact_store,
            context=RecorderContext(
                run_id=f"run-{uuid4().hex[:8]}",
                session_id=self._session_id,
                turn_id=turn_id,
                mcp_servers=self._mcp_servers,
            ),
        )
