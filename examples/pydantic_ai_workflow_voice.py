"""Local voice bot demo using a PydanticAI workflow.

This example mirrors PydanticAI's "programmatic hand-off" pattern:
the EasyCat integration point is the workflow object, not an individual
PydanticAI agent. The workflow decides which specialist handles the turn
and persists that state across turns.

Setup:
  export OPENAI_API_KEY="..."
  uv add easycat[local]
  uv add easycat[openai]
  uv add easycat[pydantic-ai]
  uv run python examples/pydantic_ai_workflow_voice.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    attach_runtime_feedback,
    create_session,
    default_event_logging,
    require_env,
    wait_for_shutdown_signal,
)


class FlightDetails(BaseModel):
    flight_number: str


class FlightSearchFailed(BaseModel):
    reason: str


class SeatChoice(BaseModel):
    row: int
    seat: str


class FlightBookingWorkflow:
    """Two-step workflow that persists the active specialist between turns."""

    output_type = FlightDetails | FlightSearchFailed | SeatChoice  # type: ignore[assignment]

    def __init__(self, *, model_name: str = "openai:gpt-5.2") -> None:
        try:
            from pydantic_ai import Agent, RunUsage  # type: ignore[import-untyped]
        except ImportError as exc:
            raise SystemExit(
                "PydanticAI is required. Install with: uv add easycat[pydantic-ai]"
            ) from exc

        self._usage = RunUsage()
        self._search_history: list[Any] | None = None
        self._seat_history: list[Any] | None = None
        self.flight_details: FlightDetails | None = None
        self.active_agent_id = "flight_search"

        self.search_agent = Agent(
            model_name,
            output_type=FlightDetails | FlightSearchFailed,  # type: ignore[arg-type]
            system_prompt=(
                "You help users find flights. "
                "Extract the best flight from the user's request. "
                "Return FlightSearchFailed if essential trip details are missing."
            ),
        )
        self.seat_agent = Agent(
            model_name,
            output_type=SeatChoice,
            system_prompt=(
                "You help users pick a seat after a flight has already been chosen. "
                "Extract the row number and seat letter from the user's reply."
            ),
        )

    async def on_user_turn(self, text: str) -> str:
        if self.flight_details is None:
            result = await self.search_agent.run(
                text,
                message_history=self._search_history,
                usage=self._usage,
            )
            self._search_history = result.new_messages()
            output = result.output
            if isinstance(output, FlightSearchFailed):
                return (
                    "I still need the route or date before I can search. "
                    "Please say where you want to go and when."
                )

            self.flight_details = output
            self.active_agent_id = "seat_selection"
            return f"I found flight {output.flight_number}. What seat would you like?"

        result = await self.seat_agent.run(
            text,
            message_history=self._seat_history,
            usage=self._usage,
        )
        self._seat_history = result.new_messages()
        choice = result.output
        assert self.flight_details is not None
        return (
            f"Got it. I saved seat {choice.row}{choice.seat} "
            f"for flight {self.flight_details.flight_number}."
        )

    def clear_history(self) -> None:
        self._search_history = None
        self._seat_history = None
        self.flight_details = None
        self.active_agent_id = "flight_search"


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    workflow = FlightBookingWorkflow()

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=workflow,
        event_logging=default_event_logging(),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
