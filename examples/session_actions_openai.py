"""Session actions demo — OpenAI Agents SDK.

Shows how agent tools can trigger session-level actions (end call,
transfer, send DTMF) using the SessionActions queue.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv run python examples/session_actions_openai.py
"""

from __future__ import annotations

import asyncio

from agents import Agent, RunContextWrapper, function_tool  # type: ignore[import-untyped]

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    OpenAIAgentsAdapter,
    SessionActions,
    attach_runtime_feedback,
    create_session,
    default_event_logging,
    require_env,
    wait_for_shutdown_signal,
)

# ── Shared action queue ──────────────────────────────────────────
# The same SessionActions instance is injected into the agent's
# context AND passed to EasyCatConfig.  Tools enqueue; Session drains.

actions = SessionActions()


# ── Tool definitions ─────────────────────────────────────────────
# Tools receive the SessionActions object via RunContextWrapper.


@function_tool
def end_call(ctx: RunContextWrapper[SessionActions], reason: str = "") -> str:
    """End the call gracefully. Use when the user says goodbye."""
    ctx.context.end_call(reason=reason)
    return "Ending the call now."


@function_tool
def transfer_to_human(ctx: RunContextWrapper[SessionActions], department: str) -> str:
    """Transfer the caller to a human agent in the specified department."""
    ctx.context.transfer_call(department)
    return f"Transferring you to {department}. Please hold."


@function_tool
def send_dtmf_tones(ctx: RunContextWrapper[SessionActions], digits: str) -> str:
    """Send DTMF tones (for navigating phone menus)."""
    ctx.context.send_dtmf(digits)
    return f"Sending tones: {digits}"


# ── Agent setup ──────────────────────────────────────────────────

agent = Agent(
    name="Assistant",
    instructions=(
        "You are a helpful voice assistant. "
        "When the user says goodbye, use the end_call tool. "
        "If they ask to speak to a human, use transfer_to_human. "
        "Be concise — you are speaking, not writing."
    ),
    tools=[end_call, transfer_to_human, send_dtmf_tones],
)


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    adapter = OpenAIAgentsAdapter(agent, context=actions)

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
