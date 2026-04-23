"""Enable echo cancellation for the local mic/speaker loop.

When running a voice bot on a laptop without headphones, the TTS output
from the speaker is picked up by the microphone and re-triggers VAD/STT
— the bot starts listening to itself.  LiveKit's WebRTC AEC3 module
handles this by subtracting the far-end (speaker) signal from the
near-end (microphone) signal.

``EasyCatConfig`` has two knobs:

  * ``enable_echo_cancellation=True`` — shortcut that auto-constructs a
    default ``EchoCancellationConfig`` with ``enabled=True``.
  * ``echo_cancellation=EchoCancellationConfig(enabled=True)`` — explicit
    form if you want to build the config yourself.

The shortcut form is used below.  WebRTC/Twilio transports already set
the shortcut to ``True`` by default; local transport does not (most
users run headphones) so you opt in explicitly.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart --extra aec
  uv run python examples/echo_cancellation.py
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


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=agent,
        enable_echo_cancellation=True,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
