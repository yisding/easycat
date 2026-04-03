"""Session actions demo — PydanticAI.

Shows how agent tools can trigger session-level actions (end call,
transfer, send DTMF) using the SessionActions queue with PydanticAI deps.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv add easycat[pydantic-ai]
  uv run python examples/session_actions_pydantic.py
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    PydanticAIAdapter,
    SessionActions,
    attach_runtime_feedback,
    create_session,
    default_event_logging,
    require_env,
    wait_for_shutdown_signal,
)

# ── Dependencies ─────────────────────────────────────────────────
# PydanticAI tools access SessionActions through the deps object.


@dataclass
class Deps:
    actions: SessionActions


# ── Shared action queue ──────────────────────────────────────────

actions = SessionActions()
deps = Deps(actions=actions)


# ── Agent + tool definitions ─────────────────────────────────────


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    try:
        from pydantic_ai import Agent, RunContext  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "PydanticAI is required. Install with: uv add easycat[pydantic-ai]"
        ) from exc

    voice_agent = Agent(
        os.getenv("PYDANTIC_AI_MODEL", "openai:gpt-4o"),
        deps_type=Deps,
        system_prompt=(
            "You are a helpful voice assistant. "
            "When the user says goodbye, use the end_call tool. "
            "If they ask to speak to a human, use transfer_to_human. "
            "Be concise — you are speaking, not writing."
        ),
    )

    @voice_agent.tool
    def end_call(ctx: RunContext[Deps], reason: str = "") -> str:  # type: ignore[type-arg]
        """End the call gracefully. Use when the user says goodbye."""
        ctx.deps.actions.end_call(reason=reason)
        return "Ending the call now."

    @voice_agent.tool
    def transfer_to_human(ctx: RunContext[Deps], department: str) -> str:  # type: ignore[type-arg]
        """Transfer the caller to a human agent in the specified department."""
        ctx.deps.actions.transfer_call(department)
        return f"Transferring you to {department}. Please hold."

    @voice_agent.tool
    def send_dtmf_tones(ctx: RunContext[Deps], digits: str) -> str:  # type: ignore[type-arg]
        """Send DTMF tones (for navigating phone menus)."""
        ctx.deps.actions.send_dtmf(digits)
        return f"Sending tones: {digits}"

    adapter = PydanticAIAdapter(voice_agent, deps=deps)

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=adapter,
        session_actions=actions,
        event_logging=default_event_logging(),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
