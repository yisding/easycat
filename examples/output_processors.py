"""Pre-TTS output processors: pronunciation fixes and phone-number pacing.

EasyCat can rewrite the assistant's text just before it reaches the TTS stage.
Two common processors are shown here:

  * ``PhoneticReplacementProcessor`` — rewrite hard-to-pronounce names with a
    spelling the TTS model will say correctly (``Siobhan`` → ``shi-vawn``).
  * ``PauseProcessor`` — insert short pauses between digits in phone numbers
    so they come out as "five five five ... one two three ..." instead of a
    rushed run of digits.

Providers that support SSML receive ``<break time="..."/>`` markers; the
ellipsis style is the plain-text fallback.  The README's convenience helper
``default_pronunciation_processors(...)`` bundles both processors into one
call — the commented block below shows the one-liner form.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv run python examples/output_processors.py

Once running, ask: "What's Siobhan's number?" — the assistant should answer
with a phone number, and you should hear the name pronounced "shi-vawn" with
clear pauses between digit groups.
"""

from __future__ import annotations

import asyncio

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    PauseProcessor,
    PhoneticReplacementProcessor,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(
        name="assistant",
        instructions=(
            "You are a helpful voice assistant for a fictional contact directory. "
            "When asked about a contact, answer with their full name and a phone "
            "number. Use the contact 'Siobhan Nguyen' at '+1 555 123 4567' as "
            "your canned answer."
        ),
    )

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=agent,
        output_processors=[
            PhoneticReplacementProcessor(
                {
                    "Siobhan": "shi-vawn",
                    "Nguyen": "win",
                }
            ),
            PauseProcessor(
                pattern=r"\+?\d[\d\s().-]{5,}\d",
                unit_pattern=r"\d",
                minimum_units=7,
                pause_ms=140,
            ),
        ],
    )

    # Equivalent one-liner using the README's convenience helper:
    #
    #   from easycat import default_pronunciation_processors
    #   output_processors=default_pronunciation_processors(
    #       name_pronunciations={"Siobhan": "shi-vawn", "Nguyen": "win"},
    #       phone_pause_ms=140,
    #   )

    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
