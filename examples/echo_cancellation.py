"""Local mic/speaker loop with LiveKit WebRTC AEC3 echo cancellation.

When running without headphones, the TTS output bouncing back into the
mic re-triggers VAD/STT (the bot listens to itself). The shortcut
``enable_echo_cancellation=True`` turns on a default
``EchoCancellationConfig``; ``EasyConfig.browser()`` sets it
automatically since browser clients always loop transport audio back.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart --extra aec
Run:   uv run python examples/echo_cancellation.py
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. Install with: uv sync --extra quickstart"
    ) from exc

from easycat import EasyConfig, run

run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
        enable_echo_cancellation=True,
    )
)
