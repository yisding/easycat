"""Tests for PydanticAIWorkflowAdapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from easycat.agent_runner import AgentStreamEvent, AgentStreamEventType
from easycat.agents.pydantic_ai_workflow import (
    PydanticAIWorkflowAdapter,
    WorkflowTurnResult,
)


class MockWorkflow:
    """Simple stateful workflow for non-streaming tests."""

    def __init__(self, results: list[str | WorkflowTurnResult]) -> None:
        self._results = list(results)
        self.calls: list[str] = []
        self.active_agent_id: str | None = None
        self.cleared = False
        self.replaced_text: str | None = None
        self.interruptions: list[tuple[str, str]] = []

    async def on_user_turn(self, text: str) -> str | WorkflowTurnResult:
        self.calls.append(text)
        result = self._results.pop(0)
        if isinstance(result, WorkflowTurnResult) and result.active_agent_id is not None:
            self.active_agent_id = result.active_agent_id
        return result

    def clear_history(self) -> None:
        self.cleared = True
        self.active_agent_id = None

    def replace_last_assistant_text(self, text: str) -> None:
        self.replaced_text = text

    def notify_interruption(self, text_spoken: str = "", *, mode: str = "truncate") -> None:
        self.interruptions.append((text_spoken, mode))


class MockStreamingWorkflow:
    """Workflow that streams text and exposes active-agent transitions."""

    def __init__(self) -> None:
        self.active_agent_id = "flight_search"
        self.calls: list[dict[str, Any]] = []

    async def on_user_turn(self, text: str) -> str:
        return text

    async def on_user_turn_streaming(
        self,
        text: str,
        *,
        cancel_token: Any = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        self.calls.append({"text": text, "cancel_token": cancel_token})
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="I found flight ")
        self.active_agent_id = "seat_selection"
        yield AgentStreamEvent(
            type=AgentStreamEventType.DONE,
            text="I found flight AK456. Which seat would you like?",
            structured_output={"flight_number": "AK456"},
        )


@dataclass
class SeatChoice:
    row: int
    seat: str


@pytest.mark.asyncio
async def test_workflow_run_returns_plain_text():
    workflow = MockWorkflow(["Hello there"])
    adapter = PydanticAIWorkflowAdapter(workflow)

    reply = await adapter.run("hi")

    assert reply == "Hello there"
    assert adapter.last_output == "Hello there"
    assert adapter.message_history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello there"},
    ]


@pytest.mark.asyncio
async def test_workflow_run_tracks_structured_output_and_active_agent():
    workflow = MockWorkflow(
        [
            WorkflowTurnResult(
                text="I found flight AK456. Which seat would you like?",
                structured_output={"flight_number": "AK456"},
                active_agent_id="seat_selection",
            )
        ]
    )
    adapter = PydanticAIWorkflowAdapter(workflow, output_type=dict)

    reply = await adapter.run("find me a flight to Paris")

    assert reply == "I found flight AK456. Which seat would you like?"
    assert adapter.last_output == {"flight_number": "AK456"}
    assert adapter.output_type is dict
    assert adapter.active_agent_id == "seat_selection"


@pytest.mark.asyncio
async def test_workflow_streaming_updates_state_on_done():
    workflow = MockStreamingWorkflow()
    adapter = PydanticAIWorkflowAdapter(workflow)

    events = [event async for event in adapter.run_streaming("I need a flight")]

    assert [event.type for event in events] == [
        AgentStreamEventType.TEXT_DELTA,
        AgentStreamEventType.DONE,
    ]
    assert events[0].text == "I found flight "
    assert events[1].text == "I found flight AK456. Which seat would you like?"
    assert events[1].structured_output == {"flight_number": "AK456"}
    assert adapter.last_output == {"flight_number": "AK456"}
    assert adapter.active_agent_id == "seat_selection"
    assert adapter.message_history == [
        {"role": "user", "content": "I need a flight"},
        {
            "role": "assistant",
            "content": "I found flight AK456. Which seat would you like?",
        },
    ]


@pytest.mark.asyncio
async def test_workflow_streaming_falls_back_to_single_turn_response():
    workflow = MockWorkflow(
        [
            WorkflowTurnResult(
                text="I booked seat 1A for you.",
                structured_output=SeatChoice(row=1, seat="A"),
                active_agent_id="seat_selection",
            )
        ]
    )
    adapter = PydanticAIWorkflowAdapter(workflow, output_type=SeatChoice)

    events = [event async for event in adapter.run_streaming("I want seat 1A")]

    assert [event.type for event in events] == [
        AgentStreamEventType.TEXT_DELTA,
        AgentStreamEventType.DONE,
    ]
    assert events[0].text == "I booked seat 1A for you."
    assert events[1].text == "I booked seat 1A for you."
    assert events[1].structured_output == SeatChoice(row=1, seat="A")
    assert adapter.last_output == SeatChoice(row=1, seat="A")


@pytest.mark.asyncio
async def test_workflow_clear_history_resets_local_and_delegates():
    workflow = MockWorkflow(["Hello there"])
    adapter = PydanticAIWorkflowAdapter(workflow)

    await adapter.run("hi")
    adapter.clear_history()

    assert workflow.cleared is True
    assert adapter.message_history == []
    assert adapter.last_output is None
    assert adapter.active_agent_id is None


@pytest.mark.asyncio
async def test_workflow_history_mutation_hooks_delegate_and_update_local_state():
    workflow = MockWorkflow(["Longer response"])
    adapter = PydanticAIWorkflowAdapter(workflow)

    await adapter.run("hello")
    adapter.replace_last_assistant_text("Patched response")
    adapter.notify_interruption("Patched", mode="truncate")

    assert workflow.replaced_text == "Patched response"
    assert workflow.interruptions == [("Patched", "truncate")]
    assert adapter.message_history[-1] == {"role": "assistant", "content": "Patched..."}
