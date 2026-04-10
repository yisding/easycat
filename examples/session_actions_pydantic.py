"""Session actions demo — PydanticAI.

Shows the simplest session action that works on every transport: ending
the session after the current reply finishes.

For telephony-specific actions such as transfer, DTMF, and SMS, use the
Twilio example instead: ``examples/twilio_app.py``.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv add easycat[pydantic-ai]
  uv run python examples/session_actions_pydantic.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
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
        "openai:gpt-5.4",
        deps_type=Deps,
        system_prompt=(
            "You are a helpful voice assistant. "
            "When the user says goodbye, use the end_call tool. "
            "Be concise — you are speaking, not writing."
        ),
    )

    @voice_agent.tool
    def end_call(ctx: RunContext[Deps], reason: str = "") -> str:  # type: ignore[type-arg]
        """End the call gracefully. Use when the user says goodbye."""
        ctx.deps.actions.end_call(reason=reason)
        return "Ending the call now."

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=voice_agent,
        session_actions=actions,
        event_logging=default_event_logging(),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
