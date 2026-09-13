"""Offline regression tests for this project's agent and turn pipeline.

No API keys, no network, no microphone. `agent.py`'s wiring is asserted as
built, and `ScriptedReasoning` stands in for the model while EasyCat's real
turn machinery (send_text, the audio pipeline, journal and latency metrics)
runs end to end. A green run means the app is wired and the pipeline is
healthy; it says nothing about live model quality — an offline stub cannot
prove what a live model would say. See AGENTS.md for the live eval ladder.

Run with: uv run pytest
"""

import asyncio
import re

import pytest
from easycat.debug.testing import (
    assert_latency,
    assert_no_error,
    assert_turn_completed,
    run_scripted_audio_turn,
    run_text_turns,
)


class ScriptedReasoning:
    """Stands in for the LLM: no model call."""

    async def run(self, text: str) -> str:
        return f"You said: {text}"


def test_agent_wires_its_instructions() -> None:
    pytest.importorskip("agents", reason="run `uv sync` to install the agent SDK")
    from agent import AGENT_NAME, INSTRUCTIONS, make_agent

    # Substitution happened: no bare "$PLACEHOLDER" survived. A dollar sign
    # inside your own name or instructions is text, and stays legal here.
    assert not re.fullmatch(r"\$[A-Z_]+", AGENT_NAME)
    assert not re.fullmatch(r"\$[A-Z_]+", INSTRUCTIONS)
    agent = make_agent()
    assert agent.name == AGENT_NAME
    assert agent.instructions == INSTRUCTIONS


def test_two_turns_share_one_session() -> None:
    hello, world = asyncio.run(run_text_turns(ScriptedReasoning(), ["hello", "world"]))

    assert hello.response == "You said: hello"
    assert world.response == "You said: world"
    assert hello.turn_id != world.turn_id
    assert_turn_completed(world, world.turn_id)
    assert_no_error(world, turn_id=world.turn_id)
    # The stub answers instantly; 5 s catches pipeline hangs without
    # flaking on slow CI machines.
    assert_latency([hello, world], max_ms=5000.0, percentile="p95")


def test_scripted_audio_turn_reaches_the_agent() -> None:
    result = asyncio.run(run_scripted_audio_turn(ScriptedReasoning(), transcript="hello"))

    assert result.response == "You said: hello"
    assert_turn_completed(result, result.turn_id)
    assert_no_error(result, turn_id=result.turn_id)
