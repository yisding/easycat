from __future__ import annotations

import pytest

import easycat.debug.testing as debug_testing
import easycat.evals as evals
from easycat.evals.pytest import run_scenario, run_scenario_file
from easycat.evals.scenario import EvalScenario, EvalTurn
from easycat.evals.simulation import SimulationMode


class _EchoAgent:
    async def run(self, text: str) -> str:
        return f"echo: {text}"


def test_reexport_smoke() -> None:
    # Set (A): every re-export is importable and identical to debug.testing.
    for name in (
        "load_bundle",
        "assert_no_error",
        "assert_tool_called",
        "assert_regex",
        "assert_exact_match",
        "assert_latency",
        "assert_turn_completed",
    ):
        assert getattr(evals, name) is getattr(debug_testing, name)


def test_simulation_mode_values() -> None:
    assert SimulationMode.TEXT.value == "text"
    assert SimulationMode.AUDIO.value == "audio"
    assert SimulationMode.SYNTHETIC_CALLER.value == "synthetic_caller"


def test_promote_turn_to_test_is_implemented() -> None:
    # M11: the symbol is implemented and validates its arguments rather than
    # raising NotImplementedError. An unknown assert_on mode is a ValueError.
    with pytest.raises(ValueError, match="assert_on must be one of"):
        evals.promote_turn_to_test("bundle.zip", "t1", out="out.py", assert_on="nope")


def test_promotion_error_is_re_exported() -> None:
    assert evals.PromotionError is not None
    assert issubclass(evals.PromotionError, RuntimeError)


def test_assert_reply_hash_is_reexport() -> None:
    assert evals.assert_reply_hash is debug_testing.assert_reply_hash


async def test_run_scenario_glue_passes() -> None:
    scenario = EvalScenario(
        name="echo", turns=[EvalTurn(user="hi", expect_response_regex="echo: hi")]
    )
    result = await run_scenario(scenario, agent=_EchoAgent())
    assert result.passed


async def test_run_scenario_glue_raises_on_failure() -> None:
    scenario = EvalScenario(name="echo", turns=[EvalTurn(user="hi", expect_response_regex="nope")])
    with pytest.raises(AssertionError, match="scenario 'echo' failed"):
        await run_scenario(scenario, agent=_EchoAgent())


async def test_run_scenario_file_glue(tmp_path) -> None:  # noqa: ANN001
    import json

    path = tmp_path / "scenario.json"
    path.write_text(
        json.dumps(
            {"name": "f", "turns": [{"user": "hi", "expect": {"response_regex": "echo: hi"}}]}
        ),
        encoding="utf-8",
    )
    result = await run_scenario_file(path, agent=_EchoAgent())
    assert result.passed
