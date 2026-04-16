"""Pinned-specialist support demo using a PydanticAI workflow.

This example routes once, pins the caller to a specialist, and only changes
specialists when the active agent explicitly asks for a hand-off.

Setup:
  export OPENAI_API_KEY="..."
  uv add easycat[local]
  uv add easycat[openai]
  uv add easycat[pydantic-ai]
  uv run python examples/pydantic_ai_support_workflow.py
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)


class SupportRoute(BaseModel):
    target: Literal["billing", "technical"]


class SpecialistReply(BaseModel):
    spoken_response: str
    handoff_to: Literal["billing", "technical"] | None = None


class SupportWorkflow:
    """Entry triage plus pinned specialist routing."""

    output_type = SupportRoute | SpecialistReply  # type: ignore[assignment]

    def __init__(self, *, model_name: str = "openai:gpt-5.2") -> None:
        try:
            from pydantic_ai import Agent, RunUsage  # type: ignore[import-untyped]
        except ImportError as exc:
            raise SystemExit(
                "PydanticAI is required. Install with: uv add easycat[pydantic-ai]"
            ) from exc

        self._usage = RunUsage()
        self._triage_history: list[Any] | None = None
        self._histories: dict[str, list[Any] | None] = {"billing": None, "technical": None}
        self.active_agent_id: str | None = None

        self.triage_agent = Agent(
            model_name,
            output_type=SupportRoute,
            system_prompt=(
                "Route the user to either billing or technical support. "
                "Use billing for invoices, refunds, payments, and plan changes. "
                "Use technical for product bugs, setup, logins, and failures."
            ),
        )
        self.billing_agent = Agent(
            model_name,
            output_type=SpecialistReply,
            system_prompt=(
                "You are the billing specialist. "
                "Answer billing questions directly. "
                "If the issue is actually technical, set handoff_to='technical'."
            ),
        )
        self.technical_agent = Agent(
            model_name,
            output_type=SpecialistReply,
            system_prompt=(
                "You are the technical support specialist. "
                "Answer technical questions directly. "
                "If the issue is actually billing, set handoff_to='billing'."
            ),
        )

    async def on_user_turn(self, text: str) -> str:
        if self.active_agent_id is None:
            route = await self.triage_agent.run(
                text,
                message_history=self._triage_history,
                usage=self._usage,
            )
            self._triage_history = route.new_messages()
            self.active_agent_id = route.output.target

        specialist = (
            self.billing_agent if self.active_agent_id == "billing" else self.technical_agent
        )
        history = self._histories[self.active_agent_id]
        result = await specialist.run(
            text,
            message_history=history,
            usage=self._usage,
        )
        self._histories[self.active_agent_id] = result.new_messages()
        reply = result.output

        if reply.handoff_to is not None and reply.handoff_to != self.active_agent_id:
            self.active_agent_id = reply.handoff_to

        return reply.spoken_response

    def clear_history(self) -> None:
        self._triage_history = None
        self._histories = {"billing": None, "technical": None}
        self.active_agent_id = None


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    workflow = SupportWorkflow()

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=workflow,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
