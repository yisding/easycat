"""Use a remote agent over the OpenAI Responses API (HTTP + SSE).

``RemoteResponsesAPIBridge`` lets the session call an agent that runs on
another service — anywhere that speaks the ``/v1/responses`` protocol —
instead of an in-process OpenAI Agents SDK or PydanticAI object.  It
implements the full ``ExternalAgentBridge`` contract (streaming,
cancellation, interruption replay), so from the session's point of view
it behaves exactly like a local bridge.

The bridge picks up its bearer token from ``EASYCAT_REMOTE_AGENT_API_KEY``
by default, but you can also pass ``remote_agent_api_key`` through
``EasyCatConfig`` (shown below).

Setup:
  export OPENAI_API_KEY="..."                          # for STT/TTS
  export EASYCAT_REMOTE_AGENT_BASE_URL="https://api.openai.com"
  export EASYCAT_REMOTE_AGENT_API_KEY="..."            # bearer for the Responses API
  export EASYCAT_REMOTE_AGENT_MODEL="gpt-4o-mini"
  uv sync --extra quickstart
  uv run python examples/responses_api_bridge.py
"""

from __future__ import annotations

import asyncio

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.integrations.agents import RemoteResponsesAPIBridge


async def main() -> None:
    openai_key = require_env("OPENAI_API_KEY")
    base_url = require_env("EASYCAT_REMOTE_AGENT_BASE_URL")
    remote_key = require_env("EASYCAT_REMOTE_AGENT_API_KEY")
    model = require_env("EASYCAT_REMOTE_AGENT_MODEL")

    bridge = RemoteResponsesAPIBridge(
        base_url=base_url,
        model=model,
        api_key=remote_key,
    )

    config = EasyCatConfig(
        openai_api_key=openai_key,
        transport=LocalTransportConfig(),
        agent=bridge,
        # Equivalent to setting EASYCAT_REMOTE_AGENT_API_KEY before start;
        # shown here so the example is self-contained.
        remote_agent_api_key=remote_key,
        agent_model=model,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
