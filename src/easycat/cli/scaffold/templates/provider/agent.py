"""Try the custom VAD provider in a live mic session."""

from agents import Agent
from easycat import EasyConfig, run

from custom_vad import register

AGENT_NAME = "$AGENT_NAME"
INSTRUCTIONS = "$AGENT_INSTRUCTIONS"

register()


def make_agent() -> Agent:
    """Build this project's agent; tests import it, so keep it side-effect free."""
    return Agent(name=AGENT_NAME, instructions=INSTRUCTIONS)


def make_config() -> EasyConfig:
    """Wire the agent into a voice config using the custom VAD. No mic opens until run()."""
    return EasyConfig.mic(vad="energy", agent=make_agent())


if __name__ == "__main__":
    run(make_config())
