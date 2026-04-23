"""Pre-TTS output processors: pronunciation fixes and phone-number pacing.

  * ``PhoneticReplacementProcessor`` rewrites hard-to-pronounce names
    (``Siobhan`` → ``shi-vawn``).
  * ``PauseProcessor`` inserts pauses between digits in phone numbers
    (SSML breaks for providers that support it; ellipsis fallback otherwise).

Once running, ask: "What's Siobhan's number?"

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart
Run:   uv run python examples/output_processors.py
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. Install with: uv sync --extra quickstart"
    ) from exc

from easycat import EasyCatConfig, PauseProcessor, PhoneticReplacementProcessor, run

# default_pronunciation_processors(name_pronunciations=..., phone_pause_ms=...)
# bundles the two below into one call.
run(
    EasyCatConfig.mic(
        agent=Agent(
            name="assistant",
            instructions=(
                "You are a helpful voice assistant for a fictional contact directory. "
                "When asked about a contact, answer with their full name and a phone "
                "number. Use 'Siobhan Nguyen' at '+1 555 123 4567' as your canned answer."
            ),
        ),
        output_processors=[
            PhoneticReplacementProcessor({"Siobhan": "shi-vawn", "Nguyen": "win"}),
            PauseProcessor(
                pattern=r"\+?\d[\d\s().-]{5,}\d",
                unit_pattern=r"\d",
                minimum_units=7,
                pause_ms=140,
            ),
        ],
    )
)
