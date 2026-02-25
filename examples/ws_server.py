"""WebSocket server example for EasyCat.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra openai-agents
  uv run python examples/ws_server.py

Connect a client that streams raw PCM16 audio to ws://localhost:8765.
"""

from __future__ import annotations

import asyncio

from easycat import EasyCatConfig, WebSocketTransportConfig, create_session

try:
    from examples.common import (
        build_openai_agents_adapter,
        default_event_logging,
        require_env,
        wait_for_shutdown_signal,
    )
    from examples.runtime_feedback import attach_runtime_feedback
except ModuleNotFoundError:  # direct script execution from examples/
    from common import (
        build_openai_agents_adapter,
        default_event_logging,
        require_env,
        wait_for_shutdown_signal,
    )
    from runtime_feedback import attach_runtime_feedback


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    adapter = build_openai_agents_adapter(instructions="You are a helpful voice assistant.")

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=WebSocketTransportConfig(),
        agent=adapter,
        wrap_agent=False,
        event_logging=default_event_logging(),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()

    print("\nServer ready. Connect a WebSocket client to ws://localhost:8765")
    print("Press Ctrl+C to stop.\n")

    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
