"""Text-mode EasyCat agent for prompt iteration."""

import asyncio

from agents import Agent
from easycat import create_text_session

agent = Agent(name="$AGENT_NAME", instructions="$AGENT_INSTRUCTIONS")


async def main() -> None:
    async with create_text_session(agent=agent) as session:
        while user := input("you: ").strip():
            print(f"bot: {await session.send_text(user)}")


asyncio.run(main())
