"""Net-new budget assertion helper for eval scenarios.

``assert_budgets_pass`` evaluates a :class:`~easycat.budgets.BudgetReport`
(produced by :func:`~easycat.budgets.build_budget_report`) and raises a
pytest-friendly :class:`AssertionError` listing every budget violation. It is
distinct from :func:`easycat.debug.testing.assert_latency`, which budgets a raw
latency percentile rather than a shared budget report.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from easycat.budgets import (
    BudgetReport,
    CostBudget,
    LatencyBudget,
    build_budget_report,
)

__all__ = ["assert_budgets_pass"]


def assert_budgets_pass(
    report_or_records: BudgetReport | Iterable[object],
    budgets: Sequence[LatencyBudget | CostBudget] | None = None,
) -> BudgetReport:
    """Assert that no budget in a report is violated.

    Accepts either a pre-built :class:`~easycat.budgets.BudgetReport` (single
    argument) or a ``(records, budgets)`` pair that is evaluated through
    :func:`~easycat.budgets.build_budget_report`. Returns the evaluated report
    on success so callers can inspect ``sampled_stages`` etc.
    """
    if isinstance(report_or_records, BudgetReport):
        if budgets is not None:
            raise TypeError(
                "assert_budgets_pass() takes either a BudgetReport or (records, budgets), not both"
            )
        report = report_or_records
    else:
        report = build_budget_report(report_or_records, budgets or [])

    if report.passed:
        return report

    lines = [
        f"  {violation.kind} {violation.stage}: "
        f"observed {violation.observed:.1f} > limit {violation.limit:.1f} "
        f"(scope={violation.scope})"
        for violation in report.violations
    ]
    raise AssertionError("budget violations:\n" + "\n".join(lines))
