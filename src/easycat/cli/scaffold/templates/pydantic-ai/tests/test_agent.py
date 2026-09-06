"""Offline regression tests for this project's agent, tools, and pipeline.

No API keys, no network, no microphone. `tools.py` runs for real, `agent.py`'s
wiring is asserted as built, and `ScriptedReasoning` stands in for the model
while EasyCat's real turn machinery (send_text, the audio pipeline, journal and
latency metrics) runs end to end. A green run means the app is wired and the
pipeline is healthy; it says nothing about live model quality — an offline stub
cannot prove which tool a model chooses. See AGENTS.md for the live eval ladder.

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

import tools


class ScriptedReasoning:
    """Stands in for the LLM: no model call, but this project's real tools.

    It calls ``tools.current_time`` through the module so a test can swap the
    real tool for a failing one, the way an outage would.
    """

    async def run(self, text: str) -> str:
        if "time" in text.lower():
            return f"It is {tools.current_time()}."
        return f"You said: {text}"


def test_current_time_tool_speaks_hh_mm() -> None:
    assert re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", tools.current_time())


def test_agent_wires_its_instructions_and_tools() -> None:
    pytest.importorskip("pydantic_ai", reason="run `uv sync` to install pydantic-ai")
    from pydantic_ai.messages import SystemPromptPart
    from pydantic_ai.models.test import TestModel

    from agent import AGENT_NAME, INSTRUCTIONS, make_agent

    # Substitution happened: no bare "$PLACEHOLDER" survived. A dollar sign
    # inside your own name or instructions is text, and stays legal here.
    assert not re.fullmatch(r"\$[A-Z_]+", AGENT_NAME)
    assert not re.fullmatch(r"\$[A-Z_]+", INSTRUCTIONS)
    # TestModel is injected, not overridden afterwards: PydanticAI resolves
    # "openai:..." inside Agent(...), so make_agent() with no argument needs a
    # real key even to build. Injecting the stub is what keeps this key-free.
    agent = make_agent(TestModel())
    assert agent.name == AGENT_NAME

    # TestModel calls every registered tool and echoes the result, so dropping
    # tools=[current_time] from make_agent() fails here; the request it sent
    # carries the system prompt only if INSTRUCTIONS was wired in.
    result = asyncio.run(agent.run("what time is it?"))
    assert "current_time" in result.output
    parts = [part for message in result.all_messages() for part in message.parts]
    assert [p.content for p in parts if isinstance(p, SystemPromptPart)] == [INSTRUCTIONS]
    # make_config() is not exercised here: unlike VoiceApp, EasyConfig
    # validates credentials at construction time (not just at run()), so
    # calling it with no API key set would fail this offline test. Put a real
    # key in .env and run `uv run --env-file .env python agent.py` to exercise
    # make_config() for real.


def test_two_turns_share_one_session() -> None:
    hello, asked = asyncio.run(
        run_text_turns(ScriptedReasoning(), ["hello", "what time is it?"])
    )

    assert hello.response == "You said: hello"
    assert re.search(r"\d\d:\d\d", asked.response)
    assert hello.turn_id != asked.turn_id
    assert_turn_completed(asked, asked.turn_id)
    assert_no_error(asked, turn_id=asked.turn_id)
    # The stub answers instantly; 5 s catches pipeline hangs without
    # flaking on slow CI machines.
    assert_latency([hello, asked], max_ms=5000.0, percentile="p95")


def test_tool_failure_surfaces_instead_of_hanging(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> str:
        raise RuntimeError("tool 'current_time' is unavailable")

    # Break the real tool, not a stand-in for it: renaming or deleting
    # current_time in tools.py fails this test too.
    monkeypatch.setattr(tools, "current_time", unavailable)

    # The traceback EasyCat logs here is expected: it is the failure being
    # reported, not a test error.
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(run_text_turns(ScriptedReasoning(), ["what time is it?"]))


def test_scripted_audio_turn_reaches_the_agent() -> None:
    result = asyncio.run(run_scripted_audio_turn(ScriptedReasoning(), transcript="hello"))

    assert result.response == "You said: hello"
    assert_turn_completed(result, result.turn_id)
    assert_no_error(result, turn_id=result.turn_id)
