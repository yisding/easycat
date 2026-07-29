"""Try the custom VAD provider in a live mic session."""

from agents import Agent
from easycat import EasyConfig, run

from custom_vad import register

agent = Agent(name="$AGENT_NAME", instructions="$AGENT_INSTRUCTIONS")
register()
run(EasyConfig.mic(vad="energy", agent=agent))
