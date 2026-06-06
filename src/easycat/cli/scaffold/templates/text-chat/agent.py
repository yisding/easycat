from asyncio import run
from agents import Agent
from easycat import create_text_session
async def main() -> None:
    async with create_text_session(agent=Agent(name="$AGENT_NAME", instructions="$AGENT_INSTRUCTIONS")) as session:
        while user := input("you: ").strip():
            print(f"bot: {await session.send_text(user)}")
run(main())
