"""Shared reliability artifact loading and budget policy for validation lanes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from easycat.validation._latency_artifacts import load_reliability_samples
from easycat.validation._latency_budgets import (
    DEFAULT_RELIABILITY_BUDGETS,
    ReliabilityBudget,
    evaluate_reliability_budgets,
)
from easycat.validation._latency_models import ReliabilitySample
from easycat.validation.report import ValidationFailure


def load_reliability(
    path: Path,
) -> tuple[list[ReliabilitySample] | None, ValidationFailure | None]:
    """Load reliability samples once, returning a structured parse failure."""
    if not path.exists():
        return None, None
    try:
        samples = load_reliability_samples(path.read_text())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, RecursionError) as exc:
        return (
            None,
            ValidationFailure(
                name="reliability.samples",
                message=f"could not load reliability samples: {exc}",
                failure_class="reliability_artifact_error",
            ),
        )
    return samples, None


def reliability_budget_failure(
    samples: Sequence[ReliabilitySample],
    budgets: Sequence[ReliabilityBudget] = DEFAULT_RELIABILITY_BUDGETS,
) -> ValidationFailure | None:
    """Return a failure when any eligible sample breaches a reliability budget."""
    violations = evaluate_reliability_budgets(samples, budgets)
    if not violations:
        return None
    return ValidationFailure(
        name="reliability.budget",
        message="reliability budget violated",
        failure_class="reliability_budget",
        details={"violations": [violation.to_dict() for violation in violations]},
    )
