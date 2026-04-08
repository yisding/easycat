"""Example 6: OpenAIAgentsBridge reference usage.

Mirrors plan appendix Example 6 — OpenAI Agents SDK agent with tools,
wrapped in ``OpenAIAgentsBridge``.  Uses mocks to avoid requiring the
real SDK at test time.

This fixture runs end-to-end using duck-typed mock objects.
"""

from __future__ import annotations

from typing import Any

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import AgentTurnInput, RecorderContext
from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge
from easycat.runtime.journal import InMemoryRingBuffer

# ── Mock SDK objects ─────────────────────────────────────────────


class _MockAgent:
    def __init__(self, name: str = "SupportAgent") -> None:
        self.name = name


class _MockRawResponseEvent:
    def __init__(self, delta: str) -> None:
        self.type = "response.output_text.delta"
        self.delta = delta


class _MockStreamEvent:
    def __init__(self, *, type: str, data: Any = None, item: Any = None) -> None:
        self.type = type
        self.data = data
        self.item = item


class _MockRunResult:
    def __init__(self, events: list[_MockStreamEvent], *, last_agent: _MockAgent) -> None:
        self._events = events
        self.last_agent = last_agent
        self.last_response_id = "resp-001"
        self.final_output = "The time in Tokyo is 3:47 PM."
        self._message_history: list[dict[str, str]] = []

    async def stream_events(self):
        for ev in self._events:
            yield ev

    def to_input_list(self) -> list[dict[str, str]]:
        return self._message_history


class _MockRunner:
    def __init__(self, result: _MockRunResult) -> None:
        self._result = result

    def run_streamed(self, agent: Any, input_data: Any, **kwargs: Any) -> _MockRunResult:
        return self._result


# ── Tests ────────────────────────────────────────────────────────


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


class TestOpenAIAgentsExample:
    """Plan appendix Example 6 — OpenAIAgentsBridge."""

    @pytest.mark.asyncio
    async def test_invoke_streams_text_and_done(self):
        agent = _MockAgent("SupportAgent")
        events = [
            _MockStreamEvent(
                type="raw_response_event",
                data=_MockRawResponseEvent("The time in Tokyo is 3:47 PM."),
            ),
        ]
        run_result = _MockRunResult(events, last_agent=agent)

        bridge = OpenAIAgentsBridge(agent=agent)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        import easycat.integrations.agents.openai_agents as oai_mod

        original_runner = oai_mod.Runner
        try:
            oai_mod.Runner = _MockRunner(run_result)

            collected = []
            async for ev in bridge.invoke(
                AgentTurnInput.from_text("What time is it in Tokyo?"), rec
            ):
                collected.append(ev)
        finally:
            oai_mod.Runner = original_runner

        text_events = [e for e in collected if e.kind == "text_delta"]
        done_events = [e for e in collected if e.kind == "done"]
        assert len(text_events) >= 1
        assert len(done_events) == 1

    @pytest.mark.asyncio
    async def test_journal_records_agent_cursor(self):
        agent = _MockAgent("SupportAgent")
        events = [
            _MockStreamEvent(
                type="raw_response_event",
                data=_MockRawResponseEvent("hello"),
            ),
        ]
        run_result = _MockRunResult(events, last_agent=agent)

        bridge = OpenAIAgentsBridge(agent=agent)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        import easycat.integrations.agents.openai_agents as oai_mod

        original_runner = oai_mod.Runner
        try:
            oai_mod.Runner = _MockRunner(run_result)
            async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
                pass
        finally:
            oai_mod.Runner = original_runner

        records = journal.read()
        names = [r.name for r in records]
        assert "unit_entered" in names
        assert "unit_exited" in names

    def test_snapshot_state(self):
        agent = _MockAgent("SupportAgent")
        bridge = OpenAIAgentsBridge(agent=agent)
        snap = bridge.snapshot_state()
        assert snap.kind == "openai_agents"
        assert snap.fields["agent"] == "SupportAgent"

    def test_committable_boundaries_published(self):
        assert OpenAIAgentsBridge.COMMITTABLE_BOUNDARIES
