"""Chapter 4 — Add agent tools, session actions, events, and speech rules.

Dependencies:
    uv sync --extra quickstart --group dev
    OPENAI_API_KEY (run mode only)

Preflight:
    uv run easycat doctor
    uv run easycat doctor --json
    uv run easycat doctor --env-file .env
    uv run easycat doctor --env-file .env --json

Run:
    uv run python docs/using-easycat/04-tools-actions/main.py preview
    uv run python docs/using-easycat/04-tools-actions/main.py run
    If the key lives in .env, add `--env-file .env` after `uv run`.
"""

import argparse
from typing import Literal, cast

from easycat import (
    EasyConfig,
    PauseProcessor,
    PhoneticReplacementProcessor,
    Session,
    SessionActions,
    VoiceApp,
    require_env,
)
from easycat.helpers import run_session
from easycat.integrations.agents import OpenAIAgentsBridge
from easycat.tts.input import TTSInput

Mode = Literal["preview", "run"]
CONTACT_RESULT = "Siobhan Nguyen's phone number is +1 (555) 123-4567."


def parse_mode() -> Mode:
    parser = argparse.ArgumentParser(
        description="Preview speech rules offline or run the tool-enabled voice app."
    )
    parser.add_argument("mode", choices=("preview", "run"))
    return cast(Mode, parser.parse_args().mode)


def build_output_processors() -> list[PhoneticReplacementProcessor | PauseProcessor]:
    return [
        PhoneticReplacementProcessor({"Siobhan": "shi-vawn", "Nguyen": "win"}),
        PauseProcessor(
            pattern=r"\+?\d[\d\s().-]{5,}\d",
            unit_pattern=r"\d",
            minimum_units=7,
            style="ellipsis",
            ellipsis_count=1,
        ),
    ]


def spoken_text(text: str) -> str:
    payload = TTSInput(text=text)
    for processor in build_output_processors():
        payload = processor.process(payload, is_final=True, is_streaming=False)
    return payload.text


def preview() -> None:
    print("Agent text:", CONTACT_RESULT)
    print("Spoken text:", spoken_text(CONTACT_RESULT))


def build_live_session() -> Session:
    try:
        from agents import Agent, RunContextWrapper, function_tool  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "This chapter needs the quickstart dependencies. Run "
            "`uv sync --extra quickstart --group dev`."
        ) from exc

    require_env("OPENAI_API_KEY")
    actions = SessionActions()

    @function_tool
    def lookup_contact(name: str) -> str:
        """Look up one fictional contact by name."""
        if name.casefold() == "siobhan nguyen":
            return CONTACT_RESULT
        return f"No contact named {name} was found."

    @function_tool
    def finish_conversation(
        ctx: RunContextWrapper[SessionActions],
        reason: str = "",
    ) -> str:
        """End the voice session gracefully after the user says goodbye."""
        ctx.context.end_call(reason=reason or "user said goodbye")
        return "I'll end our conversation after this response."

    bridge = OpenAIAgentsBridge(
        agent=Agent(
            name="contact-guide",
            instructions=(
                "You are a concise voice assistant for a fictional contact directory. "
                "Use lookup_contact for contact questions. When the user says goodbye, "
                "use finish_conversation. Do not invent contacts or phone numbers."
            ),
            tools=[lookup_contact, finish_conversation],
        ),
        context=actions,
    )
    config = EasyConfig.mic(
        agent=bridge,
        session_actions=actions,
        output_processors=build_output_processors(),
    )
    session = VoiceApp(config=config).session("local")
    session.on(
        tool_started=lambda tool_name, call_id: print(f"[tool started] {tool_name} ({call_id})"),
        tool_result=lambda call_id, result: print(f"[tool result] {call_id}: {result}"),
    )
    return session


def main() -> None:
    mode = parse_mode()
    if mode == "preview":
        preview()
        return
    run_session(build_live_session())


if __name__ == "__main__":
    main()
