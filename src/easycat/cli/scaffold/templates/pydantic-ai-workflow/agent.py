"""Voice workflow with two PydanticAI specialists — local mic to speaker."""

from pydantic_ai import Agent

from easycat import EasyConfig, run

BASE_PROMPT = "$AGENT_INSTRUCTIONS"


class SupportWorkflow:
    def __init__(self) -> None:
        self._active = "billing"
        self._history: dict[str, list[object] | None] = {"billing": None, "technical": None}
        self._agents = {
            "billing": Agent(
                "openai:gpt-4.1-mini",
                system_prompt=f"{BASE_PROMPT} Handle invoices, refunds, and plan questions.",
            ),
            "technical": Agent(
                "openai:gpt-4.1-mini",
                system_prompt=f"{BASE_PROMPT} Handle setup, audio, and browser troubleshooting.",
            ),
        }

    async def on_user_turn(self, text: str) -> str:
        lowered = text.lower()
        if any(word in lowered for word in ("invoice", "refund", "plan", "billing")):
            self._active = "billing"
        elif any(word in lowered for word in ("audio", "browser", "setup", "install")):
            self._active = "technical"

        result = await self._agents[self._active].run(
            text,
            message_history=self._history[self._active],
        )
        self._history[self._active] = result.new_messages()
        return str(result.output)


run(EasyConfig.mic(agent=SupportWorkflow(), **__EASYCAT_CONFIG_EXTRA__))  # noqa: F821
