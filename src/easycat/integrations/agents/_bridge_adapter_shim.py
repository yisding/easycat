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

from easycat.agent_runner import AgentStreamEvent, AgentStreamEventType
from easycat.agents.base import BaseAgentAdapter
from easycat.cancel import CancelToken
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

    @property
    def bridge(self) -> ExternalAgentBridge:
        """Access the wrapped bridge."""
        return self._bridge

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
        turn_id = f"turn-{uuid4().hex[:8]}"
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

    def _truncate_last_assistant_for_interruption(self, text_spoken: str) -> bool:
        recorder = self._make_recorder()
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
