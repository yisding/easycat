"""Try the custom VAD provider in a live mic session."""

from agents import Agent
from easycat import EasyConfig, run

from custom_vad import EnergyVAD, EnergyVADConfig

agent = Agent(name="$AGENT_NAME", instructions="$AGENT_INSTRUCTIONS")
run(EasyConfig.mic(vad=EnergyVAD(EnergyVADConfig()), agent=agent))
