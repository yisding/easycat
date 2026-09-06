"""Offline regression tests for this project's workflow, router, and pipeline.

No API keys, no network, no microphone. `tools.py`'s routing logic runs for
real, `agent.py`'s wiring is asserted as built, and `ScriptedReasoning`
stands in for the specialists while EasyCat's real turn machinery
(send_text, the audio pipeline, journal and latency metrics) runs end to
end. A green run means the app is wired and the pipeline is healthy; it
says nothing about live model quality — an offline stub cannot prove which
specialist a real model would pick. See AGENTS.md for the live eval ladder.

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
    """Stands in for the specialists: no model call, but this project's real router.

    It calls ``tools.pick_specialist`` through the module so a test can swap
    the real router for a failing one, the way an outage would. It — and
    every other test in this file except the two behind ``importorskip``
    below — imports no name from ``agent``, so it runs with no framework SDK
    installed: ``agent.py`` imports ``pydantic_ai`` at module scope.
    """

    async def on_user_turn(self, text: str) -> str:
        key = tools.pick_specialist(text)
        return f"[{key}] You said: {text}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I need a refund", "billing"),
        ("my browser audio is broken", "technical"),
        ("please help with setup", "technical"),
    ],
)
def test_pick_specialist_routes_by_keyword(text: str, expected: str) -> None:
    assert tools.pick_specialist(text) == expected


def test_agent_wires_its_specialists() -> None:
    pytest.importorskip("pydantic_ai", reason="run `uv sync` to install pydantic-ai")
    from pydantic_ai.models.test import TestModel

    from agent import PROMPT, make_specialists, make_workflow

    assert not re.fullmatch(r"\$[A-Z_]+", PROMPT)
    # TestModel is injected, not overridden afterwards: PydanticAI resolves
    # "openai:..." inside Agent(...), so make_specialists() with no argument
    # needs a real key even to build. Injecting the stub keeps this key-free.
    assert set(make_specialists(TestModel())) == {"billing", "technical"}
    assert set(make_workflow(TestModel()).specialists) == {"billing", "technical"}
    # make_config() is not exercised here: unlike VoiceApp, EasyConfig
    # validates credentials at construction time (not just at run()), so
    # calling it with no API key set would fail this offline test. Put a real
    # key in .env and run `uv run --env-file .env python agent.py` to exercise
    # make_config() for real.


def test_workflow_routes_each_turn_to_the_specialist_it_picked() -> None:
    pytest.importorskip("pydantic_ai", reason="run `uv sync` to install pydantic-ai")
    from pydantic_ai.models.test import TestModel

    from agent import make_workflow

    workflow = make_workflow(TestModel())
    # Each stand-in answers with its own name, so an inverted router or a
    # swapped specialist key changes which reply comes back.
    with (
        workflow.specialists["billing"].override(model=TestModel(custom_output_text="billing")),
        workflow.specialists["technical"].override(
            model=TestModel(custom_output_text="technical")
        ),
    ):
        billed = asyncio.run(workflow.on_user_turn("I need a refund"))
        fixed = asyncio.run(workflow.on_user_turn("my browser audio is broken"))
    assert billed == "billing"
    assert fixed == "technical"


def test_two_turns_share_one_session() -> None:
    billing, technical = asyncio.run(
        run_text_turns(ScriptedReasoning(), ["I need a refund", "my browser audio is broken"])
    )

    assert billing.response == "[billing] You said: I need a refund"
    assert technical.response == "[technical] You said: my browser audio is broken"
    assert billing.turn_id != technical.turn_id
    assert_turn_completed(technical, technical.turn_id)
    assert_no_error(technical, turn_id=technical.turn_id)
    # The stub answers instantly; 5 s catches pipeline hangs without
    # flaking on slow CI machines.
    assert_latency([billing, technical], max_ms=5000.0, percentile="p95")


def test_tool_failure_surfaces_instead_of_hanging(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(text: str) -> str:
        raise RuntimeError("router 'pick_specialist' is unavailable")

    # Break the real router, not a stand-in for it: renaming or deleting
    # pick_specialist in tools.py fails this test too.
    monkeypatch.setattr(tools, "pick_specialist", unavailable)

    # The traceback EasyCat logs here is expected: it is the failure being
    # reported, not a test error.
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(run_text_turns(ScriptedReasoning(), ["I need a refund"]))


def test_scripted_audio_turn_reaches_the_agent() -> None:
    result = asyncio.run(
        run_scripted_audio_turn(ScriptedReasoning(), transcript="I need a refund")
    )

    assert result.response == "[billing] You said: I need a refund"
    assert_turn_completed(result, result.turn_id)
    assert_no_error(result, turn_id=result.turn_id)
