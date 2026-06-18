"""Pytest glue for running eval scenarios as regression tests.

:func:`run_scenario_file` loads a scenario file, replays it against an agent
with :class:`~easycat.evals.runner.EvalRunner`, and raises ``AssertionError``
when any turn assertion or budget fails — the shape pytest expects.

Example::

    import pytest
    from easycat.evals.pytest import run_scenario_file


    @pytest.mark.asyncio
    async def test_refund_flow():
        await run_scenario_file("tests/evals/refund_flow.yaml", agent=my_agent)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from easycat.evals.runner import EvalRunner, ScenarioResult
from easycat.evals.scenario import EvalScenario, load_scenario

__all__ = ["run_scenario", "run_scenario_file"]


async def run_scenario(scenario: EvalScenario, *, agent: Any) -> ScenarioResult:
    """Run one scenario and raise ``AssertionError`` if it did not pass."""
    result = await EvalRunner(agent).run(scenario)
    _assert_passed(result)
    return result


async def run_scenario_file(path: str | Path, *, agent: Any) -> ScenarioResult:
    """Load a scenario file, run it, and raise ``AssertionError`` on failure."""
    return await run_scenario(load_scenario(path), agent=agent)


def _assert_passed(result: ScenarioResult) -> None:
    if result.passed:
        return
    lines = list(result.assertion_errors)
    lines.extend(
        f"  budget {violation.kind} {violation.stage}: "
        f"observed {violation.observed:.1f} > limit {violation.limit:.1f}"
        for violation in result.budget_report.violations
    )
    raise AssertionError(f"scenario {result.name!r} failed:\n" + "\n".join(lines))
