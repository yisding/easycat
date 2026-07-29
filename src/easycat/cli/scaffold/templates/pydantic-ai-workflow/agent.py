"""Voice workflow with two PydanticAI specialists — local mic to speaker."""

from easycat import EasyConfig, run
from pydantic_ai import Agent

PROMPT = "$AGENT_INSTRUCTIONS"
TECH_TERMS = ("audio", "browser", "setup", "install")

billing = Agent("openai:gpt-4.1-mini", system_prompt=f"{PROMPT} Handle invoices and plans.")
technical = Agent("openai:gpt-4.1-mini", system_prompt=f"{PROMPT} Handle setup and audio.")
agents = {"billing": billing, "technical": technical}


class SupportWorkflow:
    async def on_user_turn(self, text: str) -> str:
        key = "technical" if any(word in text.lower() for word in TECH_TERMS) else "billing"
        return str((await agents[key].run(text)).output)


run(EasyConfig.mic(agent=SupportWorkflow(), **__EASYCAT_CONFIG_EXTRA__))  # noqa: F821
