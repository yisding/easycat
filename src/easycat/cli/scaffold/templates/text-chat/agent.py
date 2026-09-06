"""Text-mode EasyCat agent for prompt iteration."""

import asyncio

from agents import Agent
from easycat import create_text_session

AGENT_NAME = "$AGENT_NAME"
INSTRUCTIONS = "$AGENT_INSTRUCTIONS"


def make_agent() -> Agent:
    """Build this project's agent; tests import it, so keep it side-effect free."""
    return Agent(name=AGENT_NAME, instructions=INSTRUCTIONS)


async def chat() -> None:
    """Run the REPL; only the __main__ guard calls this."""
    async with create_text_session(agent=make_agent()) as session:
        while user := input("you: ").strip():
            print(f"bot: {await session.send_text(user)}")


if __name__ == "__main__":
    asyncio.run(chat())
