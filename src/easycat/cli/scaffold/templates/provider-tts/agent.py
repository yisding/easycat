"""Try the custom TTS provider in a live mic session."""

from agents import Agent
from easycat import EasyConfig, run

from custom_tts import register

agent = Agent(name="$AGENT_NAME", instructions="$AGENT_INSTRUCTIONS")
register()
run(EasyConfig.mic(tts="tone", agent=agent))
