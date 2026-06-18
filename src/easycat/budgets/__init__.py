"""``easycat.budgets`` — the shared budget API for latency and cost.

This is a *submodule* export (``import easycat.budgets`` /
``from easycat.budgets import ...``) and deliberately does NOT count against the
top-level ``easycat.__all__`` cap.

Surface:

* :class:`CostBudget` — net-new per-session USD ceiling value object. The
  config field ``max_session_cost_usd`` is preserved as an alias.
* :class:`LatencyBudget` — re-exported from
  :mod:`easycat.validation.latency`; the legacy
  ``from easycat.validation.latency import LatencyBudget`` import keeps working.
* :func:`build_budget_report` / :class:`BudgetReport` — net-new shared budget
  evaluator covering BOTH the runtime journal-record path and the offline
  validation percentile path (reconciling the runtime, offline, and waterfall
  latency vocabularies into one report).

Milestone boundary (Phase 3 "Neo", M9): this package ships the budget public
API. Runtime metric emission (M12), the eval runner (M10), and the debugger
overlays (M13) all consume :func:`build_budget_report` rather than
re-implementing budget evaluation.
"""

from __future__ import annotations

from easycat.budgets.models import CostBudget, LatencyBudget
from easycat.budgets.report import (
    WATERFALL_STAGE_ALIASES,
    BudgetReport,
    BudgetViolation,
    build_budget_report,
    normalize_budget_stage,
)

__all__ = [
    "BudgetReport",
    "BudgetViolation",
    "CostBudget",
    "LatencyBudget",
    "WATERFALL_STAGE_ALIASES",
    "build_budget_report",
    "normalize_budget_stage",
]
