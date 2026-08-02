"""Try the custom STT provider in a live mic session."""

from agents import Agent
from easycat import EasyConfig, run

from custom_stt import register

agent = Agent(name="$AGENT_NAME", instructions="$AGENT_INSTRUCTIONS")
register()
run(EasyConfig.mic(stt="scripted", agent=agent))
