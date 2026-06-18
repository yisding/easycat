from __future__ import annotations

import pytest

from easycat.budgets import CostBudget, LatencyBudget
from easycat.evals import EvalRunner, EvalScenario, EvalTurn, ScenarioResult


class _EchoAgent:
    """Deterministic stand-in for an LLM-backed agent."""

    async def run(self, text: str) -> str:
        return f"echo: {text}"


async def test_runner_executes_text_scenario_without_audio() -> None:
    scenario = EvalScenario(
        name="echo_flow",
        turns=[
            EvalTurn(user="hello", expect_response_regex=r"^echo: hello$"),
            EvalTurn(user="world", expect_response_regex=r"world"),
        ],
    )
    result = await EvalRunner(_EchoAgent()).run(scenario)

    assert isinstance(result, ScenarioResult)
    assert result.name == "echo_flow"
    assert result.passed
    assert len(result.turns) == 2
    assert result.turns[0].response == "echo: hello"


async def test_runner_records_are_assertion_compatible() -> None:
    scenario = EvalScenario(name="r", turns=[EvalTurn(user="ping")])
    result = await EvalRunner(_EchoAgent()).run(scenario)

    names = {record.get("name") for record in result.records()}
    assert "turn_started" in names
    assert "agent_final" in names
    assert "turn_ended" in names


async def test_runner_failed_regex_records_assertion_error() -> None:
    scenario = EvalScenario(
        name="bad",
        turns=[EvalTurn(user="hello", expect_response_regex=r"goodbye")],
    )
    result = await EvalRunner(_EchoAgent()).run(scenario)

    assert not result.passed
    assert result.assertion_errors
    assert not result.turns[0].passed


async def test_runner_turn_total_latency_budget_passes() -> None:
    # A generous total_ms budget against an instant echo agent passes.
    scenario = EvalScenario(
        name="latency_ok",
        turns=[EvalTurn(user="hi")],
        budgets=[LatencyBudget(stage="total_ms", max_ms=60_000.0)],
    )
    result = await EvalRunner(_EchoAgent()).run(scenario)
    assert result.budget_report.passed
    assert "total_ms" in result.budget_report.sampled_stages


async def test_runner_cost_budget_with_no_cost_records_passes() -> None:
    # Text turns emit no cost records, so a cost budget evaluates against zero
    # observed cost and passes (cost is opt-in via runtime cost records).
    scenario = EvalScenario(
        name="cost_ok",
        turns=[EvalTurn(user="hi")],
        budgets=[CostBudget(max_session_usd=0.05)],
    )
    result = await EvalRunner(_EchoAgent()).run(scenario)
    assert result.budget_report.passed


@pytest.mark.parametrize("stage", ["tts_ttfb_ms", "llm_ttft_ms"])
async def test_runner_provider_stage_budget_raises_no_samples(stage: str) -> None:
    # TEST-4: a provider-stage latency budget on a text scenario would evaluate
    # against ZERO samples and pass vacuously; the runner must RAISE instead.
    scenario = EvalScenario(
        name="provider_stage",
        turns=[EvalTurn(user="hi")],
        budgets=[LatencyBudget(stage=stage, max_ms=100.0)],
    )
    with pytest.raises(ValueError, match=f"no samples for stage {stage!r}"):
        await EvalRunner(_EchoAgent()).run(scenario)


async def test_runner_requires_an_agent() -> None:
    scenario = EvalScenario(name="x", turns=[EvalTurn(user="hi")])
    with pytest.raises(ValueError, match="needs an agent"):
        await EvalRunner().run(scenario)


async def test_runner_accepts_per_run_agent() -> None:
    scenario = EvalScenario(name="x", turns=[EvalTurn(user="hi", expect_response_regex="hi")])
    result = await EvalRunner().run(scenario, agent=_EchoAgent())
    assert result.passed
