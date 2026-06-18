"""Shared budget report covering BOTH the runtime and offline percentile paths.

``build_budget_report`` is the single budget evaluator used by the debugger
(`/api/budgets`), ``easycat eval run``, ``easycat latency``, validation reports,
and promoted regression tests. It deliberately reconciles the THREE distinct
latency vocabularies that exist in the codebase rather than fragmenting them:

1. **Runtime / journal records** emit only ``stage="total_ms"`` single
   observations (``session/_turn_runner.py``); these flow through the
   push-based :class:`~easycat.session._latency_budget.LatencyBudgetMonitor`.
2. **Offline validation percentile columns** (``tts_ttfb_ms`` / ``llm_ttft_ms``
   / ``total_ms``) live as ``LatencyRow`` fields evaluated by
   :func:`~easycat.validation.latency.evaluate_budgets` against
   ``DEFAULT_BUDGETS`` (``validation/latency.py``).
3. **Waterfall milestones** use ``*_to_*_ms`` names
   (``debug/_turn_timeline.py``); these are mapped onto the flat budget stage
   names so a single ``LatencyBudget(stage=...)`` evaluates uniformly.

The builder evaluates ``LatencyBudget`` against runtime/waterfall single
observations, retrofits the offline percentile path onto
:func:`~easycat.validation.latency.evaluate_budgets`, and evaluates
``CostBudget`` against cost records.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from easycat.budgets.models import CostBudget, LatencyBudget
from easycat.validation.latency import (
    LatencyBudgetViolation,
    evaluate_budgets,
)

__all__ = [
    "BudgetReport",
    "BudgetViolation",
    "WATERFALL_STAGE_ALIASES",
    "build_budget_report",
    "normalize_budget_stage",
]


# Waterfall ``*_to_*_ms`` milestone names map onto the flat runtime/offline
# budget stage vocabulary so a single budget evaluates across all three
# sources. Keep this in sync with ``debug/_turn_timeline.py`` milestones and
# the runtime metric names in ``session/_turn_runner.py``.
WATERFALL_STAGE_ALIASES: dict[str, str] = {
    "vad_endpoint_to_stt_final_ms": "stt_final_latency_ms",
    "agent_request_to_first_token_ms": "llm_ttft_ms",
    "agent_first_token_to_tts_first_byte_ms": "tts_ttfb_ms",
    "vad_endpoint_to_tts_first_byte_ms": "first_audio_ms",
    "user_speech_start_to_bot_stopped_ms": "barge_in_ack_ms",
}


def normalize_budget_stage(stage: str) -> str:
    """Collapse a runtime/waterfall metric name onto its flat budget stage.

    Waterfall ``*_to_*_ms`` names are mapped through
    :data:`WATERFALL_STAGE_ALIASES`; all other names pass through unchanged so
    the offline percentile columns (``total_ms``, ``tts_ttfb_ms``,
    ``llm_ttft_ms``) and the runtime ``total_ms`` records line up.
    """
    return WATERFALL_STAGE_ALIASES.get(stage, stage)


def _budget_matches_stage(observed_stage: str, budget_stage: str) -> bool:
    """Whether ``budget_stage`` targets a record observed as ``observed_stage``.

    Mirrors ``session/_latency_budget._budget_matches_stage`` so the shared
    report and the runtime monitor agree: a budget stage matches the observed
    stage exactly, or via the ``_ms`` / ``_latency_ms`` suffix forms.
    """
    key = budget_stage.strip()
    return key in {
        observed_stage,
        f"{observed_stage}_ms",
        f"{observed_stage}_latency_ms",
    }


@dataclass(frozen=True)
class BudgetViolation:
    """A single budget breach, latency or cost."""

    kind: str  # "latency" | "cost"
    stage: str
    observed: float
    limit: float
    scope: str
    percentile: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "stage": self.stage,
            "observed": self.observed,
            "limit": self.limit,
            "scope": self.scope,
        }
        if self.percentile is not None:
            payload["percentile"] = self.percentile
        return payload


@dataclass(frozen=True)
class BudgetReport:
    """Result of evaluating a set of budgets against latency and cost data."""

    violations: tuple[BudgetViolation, ...] = ()
    evaluated_latency_budgets: int = 0
    evaluated_cost_budgets: int = 0
    sampled_stages: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": [violation.to_dict() for violation in self.violations],
            "evaluated_latency_budgets": self.evaluated_latency_budgets,
            "evaluated_cost_budgets": self.evaluated_cost_budgets,
            "sampled_stages": list(self.sampled_stages),
        }


def _record_field(record: Any, name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latency_observation(record: Any) -> tuple[str, float] | None:
    """Extract a ``(normalized_stage, observed_ms)`` pair from one record.

    Accepts both journal-record shapes (a ``data`` mapping carrying ``stage`` /
    ``value``) and flat mappings that carry ``stage`` / ``observed_ms`` /
    ``value`` directly, plus waterfall milestone mappings keyed by
    ``*_to_*_ms`` names.
    """
    data = _record_field(record, "data")
    stage = _record_field(record, "stage")
    if stage is None and isinstance(data, Mapping):
        stage = data.get("stage")
    if stage is None:
        return None
    observed = _record_field(record, "observed_ms")
    if observed is None:
        observed = _record_field(record, "value")
    if observed is None and isinstance(data, Mapping):
        observed = data.get("observed_ms", data.get("value"))
    observed_ms = _float_or_none(observed)
    if observed_ms is None:
        return None
    return normalize_budget_stage(str(stage)), observed_ms


def _waterfall_observations(record: Any) -> list[tuple[str, float]]:
    """Yield flat ``(stage, observed_ms)`` pairs from a waterfall milestone map.

    A milestone record carries ``*_to_*_ms`` keys (in ``data`` or at the top
    level); each known key is normalized onto its flat budget stage.
    """
    results: list[tuple[str, float]] = []
    candidate = _record_field(record, "data")
    if not isinstance(candidate, Mapping):
        candidate = record if isinstance(record, Mapping) else None
    if not isinstance(candidate, Mapping):
        return results
    for key, value in candidate.items():
        if key not in WATERFALL_STAGE_ALIASES:
            continue
        observed_ms = _float_or_none(value)
        if observed_ms is None:
            continue
        results.append((WATERFALL_STAGE_ALIASES[key], observed_ms))
    return results


def _cost_observation(record: Any) -> float | None:
    """Extract a cumulative session cost (USD) from a cost record, if any."""
    data = _record_field(record, "data")
    for source in (record, data):
        if source is None:
            continue
        for key in ("total_usd", "session_cost_usd", "cost_usd"):
            value = _float_or_none(_record_field(source, key))
            if value is not None:
                return value
    return None


def _evaluate_latency_records(
    records: Iterable[Any],
    latency_budgets: Sequence[LatencyBudget],
) -> tuple[list[BudgetViolation], set[str]]:
    """Evaluate single-observation runtime/waterfall records against budgets."""
    violations: list[BudgetViolation] = []
    sampled: set[str] = set()
    observations: list[tuple[str, float]] = []
    for record in records:
        single = _latency_observation(record)
        if single is not None:
            observations.append(single)
        observations.extend(_waterfall_observations(record))
    for stage, observed_ms in observations:
        sampled.add(stage)
        for budget in latency_budgets:
            if not _budget_matches_stage(stage, budget.stage):
                continue
            if observed_ms > budget.max_ms:
                violations.append(
                    BudgetViolation(
                        kind="latency",
                        stage=budget.stage,
                        observed=observed_ms,
                        limit=float(budget.max_ms),
                        scope="runtime",
                        percentile=budget.percentile,
                    )
                )
    return violations, sampled


def _evaluate_cost_records(
    records: Iterable[Any],
    cost_budgets: Sequence[CostBudget],
) -> list[BudgetViolation]:
    """Evaluate cost budgets against the peak cumulative session cost."""
    if not cost_budgets:
        return []
    peak: float | None = None
    for record in records:
        observed = _cost_observation(record)
        if observed is None:
            continue
        peak = observed if peak is None else max(peak, observed)
    if peak is None:
        return []
    violations: list[BudgetViolation] = []
    for budget in cost_budgets:
        if peak > budget.max_session_usd:
            violations.append(
                BudgetViolation(
                    kind="cost",
                    stage="max_session_usd",
                    observed=peak,
                    limit=budget.max_session_usd,
                    scope="session",
                )
            )
    return violations


def _evaluate_offline_percentiles(
    percentiles: Mapping[str, Any],
    latency_budgets: Sequence[LatencyBudget],
) -> list[BudgetViolation]:
    """Retrofit ``validation/latency.evaluate_budgets`` onto the shared report.

    This keeps the offline percentile path from being structurally excluded
    (CONS-7): the same ``LatencyBudget`` set evaluated at runtime is run through
    the existing percentile evaluator and lifted into ``BudgetViolation``s.
    """
    raw: list[LatencyBudgetViolation] = evaluate_budgets(percentiles, latency_budgets)
    return [
        BudgetViolation(
            kind="latency",
            stage=violation.stage,
            observed=violation.observed_ms,
            limit=violation.budget_ms,
            scope=violation.scope,
            percentile=violation.percentile,
        )
        for violation in raw
    ]


def build_budget_report(
    records: Iterable[Any] | None = None,
    budgets: Sequence[LatencyBudget | CostBudget] | None = None,
    *,
    percentiles: Mapping[str, Any] | None = None,
) -> BudgetReport:
    """Evaluate ``budgets`` against runtime records AND offline percentiles.

    ``records`` are runtime journal records (or flat mappings) carrying
    single-observation latency stages, waterfall ``*_to_*_ms`` milestones, and
    cost rollups. ``percentiles`` is the offline validation percentile block
    (``{"overall": {...}, "by_condition": {...}}``); when supplied, latency
    budgets are ALSO evaluated through
    :func:`~easycat.validation.latency.evaluate_budgets` so the runtime and
    offline paths converge on one report.

    Passing neither ``records`` nor ``percentiles`` yields an empty (passing)
    report against the supplied budget counts.
    """
    record_list = list(records or [])
    budget_list = list(budgets or [])
    latency_budgets = [b for b in budget_list if isinstance(b, LatencyBudget)]
    cost_budgets = [b for b in budget_list if isinstance(b, CostBudget)]

    violations: list[BudgetViolation] = []
    runtime_violations, sampled = _evaluate_latency_records(record_list, latency_budgets)
    violations.extend(runtime_violations)
    violations.extend(_evaluate_cost_records(record_list, cost_budgets))
    if percentiles is not None:
        violations.extend(_evaluate_offline_percentiles(percentiles, latency_budgets))

    return BudgetReport(
        violations=tuple(violations),
        evaluated_latency_budgets=len(latency_budgets),
        evaluated_cost_budgets=len(cost_budgets),
        sampled_stages=tuple(sorted(sampled)),
    )
