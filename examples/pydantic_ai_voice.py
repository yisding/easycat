"""Local voice bot demo using a single PydanticAI agent.

If your app needs multiple specialists or step-based control flow, prefer:
  - examples/pydantic_ai_workflow_voice.py
  - examples/pydantic_ai_support_workflow.py

Setup:
  export OPENAI_API_KEY="..."
  uv add easycat[local]
  uv add easycat[openai]
  uv add easycat[pydantic-ai]
  uv run python examples/pydantic_ai_voice.py
"""

from __future__ import annotations

import asyncio

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    PydanticAIAdapter,
    attach_runtime_feedback,
    create_session,
    default_event_logging,
    require_env,
    wait_for_shutdown_signal,
)


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    try:
        from pydantic_ai import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "PydanticAI is required. Install with: uv add easycat[pydantic-ai]"
        ) from exc

    voice_agent = Agent(
        "openai:gpt-5.2",
        system_prompt="You are a helpful voice assistant.",
    )
    adapter = PydanticAIAdapter(voice_agent)

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=adapter,
        event_logging=default_event_logging(),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()

    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
