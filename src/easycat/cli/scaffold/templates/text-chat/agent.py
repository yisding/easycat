"""Text REPL for iterating on an agent without audio infrastructure."""

import asyncio

from agents import Agent

from easycat import create_text_session


async def main() -> None:
    agent = Agent(name="$AGENT_NAME", instructions="$AGENT_INSTRUCTIONS")
    session = create_text_session(agent=agent)
    while user := input("you: ").strip():
        print(f"bot: {await session.send_text(user)}")


if __name__ == "__main__":
    asyncio.run(main())
