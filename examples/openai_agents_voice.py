"""Local voice bot demo using a single OpenAI Agents SDK agent.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart --group dev
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
       uv run easycat doctor --env-file .env --json  # for parseable checks
Run:   uv run python examples/openai_agents_voice.py
       uv run --env-file .env python examples/openai_agents_voice.py  # if keys live in .env
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. For an app, run: "
        "uv add 'easycat[quickstart]'. In this repo, run: "
        "uv sync --extra quickstart --group dev"
    ) from exc

from easycat import EasyConfig, run

run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant.")
    )
)

# Next, try (change one token, or type `easycat.` to browse the surface):
#   stt="deepgram/nova-2"          swap STT (needs DEEPGRAM_API_KEY + --extra deepgram)
#   tools=[...] on your Agent      tools live on YOUR Agent, not on EasyCat
#   EasyConfig.browser(agent=...)  serve in a browser (needs a server + --extra webrtc)
#   debug="full"                   record a journal under .easycat/journals/
#   uv run easycat inspect .easycat/journals/<session_id>.sqlite
#   uv run easycat docs --audience learners
# Full ground-up ladder: docs/teaching/00-hello-audio/
