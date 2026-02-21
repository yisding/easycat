"""WebSocket server example for EasyCat.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra openai-agents
  uv run python examples/ws_server.py

Connect a client that streams raw PCM16 audio to ws://localhost:8765.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from easycat import (
    EasyCatConfig,
    OpenAIAgentsAdapter,
    WebSocketTransportConfig,
    create_session,
)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required.")

    try:
        from agents import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "OpenAI Agents SDK is required. Install with: uv sync --extra openai-agents"
        ) from exc

    voice_agent = Agent(
        name="VoiceAssistant",
        instructions="You are a helpful voice assistant.",
    )
    adapter = OpenAIAgentsAdapter(voice_agent)

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=WebSocketTransportConfig(),
        agent=adapter,
        wrap_agent=False,
    )
    session = create_session(config)

    await session.start()

    print("\nServer ready. Connect a WebSocket client to ws://localhost:8765")
    print("Press Ctrl+C to stop.\n")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
