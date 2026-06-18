"""``easycat.evals`` — native eval and simulation APIs.

This is a *submodule* export (``import easycat.evals`` /
``from easycat.evals import ...``) and deliberately does NOT count against the
top-level ``easycat.__all__`` cap.

The surface is two distinct sets carrying different testing obligations:

**(A) Pure re-exports of :mod:`easycat.debug.testing`** — no new behavior, just
an import surface so promoted regression tests and scenario authors import
everything from one place:

* :func:`load_bundle`
* :func:`assert_no_error`
* :func:`assert_tool_called`
* :func:`assert_regex`
* :func:`assert_exact_match`
* :func:`assert_latency`
* :func:`assert_turn_completed`

**(B) Net-new symbols** — fully unit-tested here:

* :class:`EvalScenario` / :class:`EvalTurn` — the scenario data model.
* :class:`EvalRunner` / :class:`ScenarioResult` — the text-first runner.
* :func:`assert_budgets_pass` — assert a shared :class:`~easycat.budgets.BudgetReport`.
* :func:`promote_turn_to_test` — scaffold in M10; hardened in M11.

``EvalScenario.budgets`` is ``list[LatencyBudget | CostBudget]`` where
``CostBudget`` is imported from :mod:`easycat.budgets` (M9) — it is never
redefined here.
"""

from __future__ import annotations

# (A) Pure re-exports of easycat.debug.testing — no new behavior.
from easycat.debug.testing import (
    assert_exact_match,
    assert_latency,
    assert_no_error,
    assert_regex,
    assert_tool_called,
    assert_turn_completed,
    load_bundle,
)

# (B) Net-new symbols — fully unit-tested.
from easycat.evals.assertions import assert_budgets_pass
from easycat.evals.promote import promote_turn_to_test
from easycat.evals.runner import EvalRunner, ScenarioResult
from easycat.evals.scenario import EvalScenario, EvalTurn, load_scenario

__all__ = [
    # (A) pure re-exports
    "assert_exact_match",
    "assert_latency",
    "assert_no_error",
    "assert_regex",
    "assert_tool_called",
    "assert_turn_completed",
    "load_bundle",
    # (B) net-new
    "EvalRunner",
    "EvalScenario",
    "EvalTurn",
    "ScenarioResult",
    "assert_budgets_pass",
    "load_scenario",
    "promote_turn_to_test",
]
