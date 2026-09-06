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

    It calls ``tools.connection_help`` through the module so a test can swap
    the real tool for a failing one, the way an outage would.
    """

    async def run(self, text: str) -> str:
        if "connect" in text.lower():
            return tools.connection_help()
        return f"You said: {text}"


def test_connection_help_points_at_the_browser_demo() -> None:
    assert "http://localhost:8080" in tools.connection_help()


def test_agent_wires_its_instructions_and_tools() -> None:
    pytest.importorskip("agents", reason="run `uv sync` to install the agent SDK")
    from agent import AGENT_NAME, INSTRUCTIONS, make_agent

    # Substitution happened: no bare "$PLACEHOLDER" survived. A dollar sign
    # inside your own name or instructions is text, and stays legal here.
    assert not re.fullmatch(r"\$[A-Z_]+", AGENT_NAME)
    assert not re.fullmatch(r"\$[A-Z_]+", INSTRUCTIONS)
    agent = make_agent()
    assert agent.name == AGENT_NAME
    assert agent.instructions == INSTRUCTIONS
    assert [tool.name for tool in agent.tools] == ["connection_help"]
    # make_config() is not exercised here: unlike VoiceApp, EasyConfig
    # validates credentials at construction time (not just at run()), so
    # calling it with no API key set would fail this offline test. Put a real
    # key in .env and run `uv run --env-file .env python agent.py` to exercise
    # make_config() for real.


def test_two_turns_share_one_session() -> None:
    hello, howto = asyncio.run(
        run_text_turns(ScriptedReasoning(), ["hello", "how do I connect?"])
    )

    assert hello.response == "You said: hello"
    assert "localhost:8080" in howto.response
    assert hello.turn_id != howto.turn_id
    assert_turn_completed(howto, howto.turn_id)
    assert_no_error(howto, turn_id=howto.turn_id)
    # The stub answers instantly; 5 s catches pipeline hangs without
    # flaking on slow CI machines.
    assert_latency([hello, howto], max_ms=5000.0, percentile="p95")


def test_tool_failure_surfaces_instead_of_hanging(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> str:
        raise RuntimeError("tool 'connection_help' is unavailable")

    # Break the real tool, not a stand-in for it: renaming or deleting
    # connection_help in tools.py fails this test too.
    monkeypatch.setattr(tools, "connection_help", unavailable)

    # The traceback EasyCat logs here is expected: it is the failure being
    # reported, not a test error.
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(run_text_turns(ScriptedReasoning(), ["how do I connect?"]))


def test_scripted_audio_turn_reaches_the_agent() -> None:
    result = asyncio.run(run_scripted_audio_turn(ScriptedReasoning(), transcript="hello"))

    assert result.response == "You said: hello"
    assert_turn_completed(result, result.turn_id)
    assert_no_error(result, turn_id=result.turn_id)
