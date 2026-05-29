"""Local voice bot demo using a single OpenAI Agents SDK agent.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart
Run:   uv run python examples/openai_agents_voice.py
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
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant.")
    )
)

# Next, try (change one token, or type `easycat.` to browse the surface):
#   stt="deepgram/nova-2"          swap STT (needs DEEPGRAM_API_KEY + easycat[deepgram])
#   tools=[...] on your Agent      tools live on YOUR Agent, not on EasyCat
#   EasyConfig.browser(agent=...)  serve in a browser (needs a server + easycat[webrtc])
#   debug="full"                   record a journal for `easycat inspect`
# Full ground-up ladder: docs/teaching/00-hello-audio/
