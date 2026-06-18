from __future__ import annotations

import pytest

from easycat.budgets import BudgetReport, BudgetViolation, CostBudget, LatencyBudget
from easycat.evals import assert_budgets_pass


def test_assert_budgets_pass_with_passing_report() -> None:
    report = BudgetReport()
    assert assert_budgets_pass(report) is report


def test_assert_budgets_pass_raises_on_violation() -> None:
    report = BudgetReport(
        violations=(
            BudgetViolation(
                kind="latency",
                stage="total_ms",
                observed=2000.0,
                limit=1500.0,
                scope="runtime",
            ),
        )
    )
    with pytest.raises(AssertionError, match="latency total_ms"):
        assert_budgets_pass(report)


def test_assert_budgets_pass_from_records_and_budgets() -> None:
    records = [{"stage": "total_ms", "observed_ms": 900.0}]
    budgets = [LatencyBudget(stage="total_ms", max_ms=1500.0)]
    report = assert_budgets_pass(records, budgets)
    assert report.passed
    assert "total_ms" in report.sampled_stages


def test_assert_budgets_pass_records_path_raises() -> None:
    records = [{"total_usd": 0.10}]
    budgets = [CostBudget(max_session_usd=0.05)]
    with pytest.raises(AssertionError, match="cost max_session_usd"):
        assert_budgets_pass(records, budgets)


def test_assert_budgets_pass_rejects_report_plus_budgets() -> None:
    with pytest.raises(TypeError, match="either a BudgetReport"):
        assert_budgets_pass(BudgetReport(), [LatencyBudget(stage="total_ms", max_ms=1.0)])
