from __future__ import annotations

import pytest

from easycat.budgets import (
    BudgetReport,
    CostBudget,
    LatencyBudget,
    build_budget_report,
    normalize_budget_stage,
)
from easycat.runtime.records import JournalRecord, JournalRecordKind


def test_normalize_budget_stage_maps_waterfall_names() -> None:
    assert normalize_budget_stage("agent_request_to_first_token_ms") == "llm_ttft_ms"
    assert normalize_budget_stage("agent_first_token_to_tts_first_byte_ms") == "tts_ttfb_ms"
    # Flat names pass through unchanged.
    assert normalize_budget_stage("total_ms") == "total_ms"


def test_build_budget_report_empty_passes() -> None:
    report = build_budget_report()
    assert isinstance(report, BudgetReport)
    assert report.passed
    assert report.violations == ()


# ── Source 1: runtime total_ms single observation ──────────────────────────


def test_runtime_total_ms_record_violates_budget() -> None:
    record = JournalRecord(
        sequence=1,
        session_id="s1",
        kind=JournalRecordKind.METRIC,
        name="turn_total_latency_ms",
        turn_id="t1",
        data={"stage": "total_ms", "value": 9000.0},
    )
    report = build_budget_report(
        [record],
        [LatencyBudget(stage="total_ms", max_ms=8000.0)],
    )
    assert not report.passed
    assert "total_ms" in report.sampled_stages
    violation = report.violations[0]
    assert violation.kind == "latency"
    assert violation.stage == "total_ms"
    assert violation.observed == pytest.approx(9000.0)
    assert violation.limit == pytest.approx(8000.0)
    assert violation.scope == "runtime"


def test_runtime_total_ms_record_within_budget_passes() -> None:
    record = JournalRecord(
        sequence=1,
        session_id="s1",
        kind=JournalRecordKind.METRIC,
        name="turn_total_latency_ms",
        data={"stage": "total_ms", "value": 1000.0},
    )
    report = build_budget_report(
        [record],
        [LatencyBudget(stage="total_ms", max_ms=8000.0)],
    )
    assert report.passed
    assert report.evaluated_latency_budgets == 1


def test_runtime_flat_mapping_observation() -> None:
    # A flat mapping carrying stage/observed_ms is also accepted.
    record = {"stage": "total_ms", "observed_ms": 9000.0}
    report = build_budget_report([record], [LatencyBudget(stage="total_ms", max_ms=8000.0)])
    assert not report.passed


# ── Source 2: offline percentile columns ───────────────────────────────────


def test_offline_percentile_columns_evaluated() -> None:
    percentiles = {
        "overall": {
            "tts_ttfb_ms": {"p95": 2500.0},
            "llm_ttft_ms": {"p95": 4000.0},
            "total_ms": {"p95": 500.0},
        },
        "by_condition": {},
    }
    budgets = [
        LatencyBudget(stage="tts_ttfb_ms", max_ms=1500.0),
        LatencyBudget(stage="llm_ttft_ms", max_ms=2500.0),
        LatencyBudget(stage="total_ms", max_ms=8000.0),
    ]
    report = build_budget_report(budgets=budgets, percentiles=percentiles)
    assert not report.passed
    stages = {v.stage for v in report.violations}
    assert stages == {"tts_ttfb_ms", "llm_ttft_ms"}
    for violation in report.violations:
        assert violation.scope == "overall"
        assert violation.percentile == "p95"


def test_offline_percentile_columns_pass_when_under_budget() -> None:
    percentiles = {"overall": {"total_ms": {"p95": 400.0}}, "by_condition": {}}
    report = build_budget_report(
        budgets=[LatencyBudget(stage="total_ms", max_ms=8000.0)],
        percentiles=percentiles,
    )
    assert report.passed


# ── Source 3: waterfall *_to_*_ms milestones ───────────────────────────────


def test_waterfall_milestone_names_map_to_flat_stages() -> None:
    record = {
        "data": {
            "agent_first_token_to_tts_first_byte_ms": 2000.0,
            "agent_request_to_first_token_ms": 3000.0,
        }
    }
    budgets = [
        LatencyBudget(stage="tts_ttfb_ms", max_ms=1500.0),
        LatencyBudget(stage="llm_ttft_ms", max_ms=2500.0),
    ]
    report = build_budget_report([record], budgets)
    assert not report.passed
    stages = {v.stage for v in report.violations}
    assert stages == {"tts_ttfb_ms", "llm_ttft_ms"}
    # Waterfall observations are surfaced under their flat sampled-stage names.
    assert "tts_ttfb_ms" in report.sampled_stages
    assert "llm_ttft_ms" in report.sampled_stages


def test_waterfall_milestone_within_budget_passes() -> None:
    record = {"data": {"agent_first_token_to_tts_first_byte_ms": 100.0}}
    report = build_budget_report([record], [LatencyBudget(stage="tts_ttfb_ms", max_ms=1500.0)])
    assert report.passed


# ── Cost budgets ───────────────────────────────────────────────────────────


def test_cost_budget_violation_from_records() -> None:
    records = [
        {"name": "cost_update", "data": {"total_usd": 0.02}},
        {"name": "cost_update", "data": {"total_usd": 0.08}},
    ]
    report = build_budget_report(records, [CostBudget(max_session_usd=0.05)])
    assert not report.passed
    assert report.evaluated_cost_budgets == 1
    violation = report.violations[0]
    assert violation.kind == "cost"
    assert violation.stage == "max_session_usd"
    assert violation.observed == pytest.approx(0.08)
    assert violation.limit == pytest.approx(0.05)


def test_cost_budget_passes_when_under_ceiling() -> None:
    records = [{"data": {"total_usd": 0.01}}]
    report = build_budget_report(records, [CostBudget(max_session_usd=0.05)])
    assert report.passed


def test_cost_and_latency_budgets_combined() -> None:
    records = [
        {"stage": "total_ms", "observed_ms": 9000.0},
        {"data": {"total_usd": 0.10}},
    ]
    budgets = [
        LatencyBudget(stage="total_ms", max_ms=8000.0),
        CostBudget(max_session_usd=0.05),
    ]
    report = build_budget_report(records, budgets)
    kinds = {v.kind for v in report.violations}
    assert kinds == {"latency", "cost"}
    assert report.evaluated_latency_budgets == 1
    assert report.evaluated_cost_budgets == 1


def test_report_to_dict_shape() -> None:
    report = build_budget_report(
        [{"stage": "total_ms", "observed_ms": 9000.0}],
        [LatencyBudget(stage="total_ms", max_ms=8000.0)],
    )
    payload = report.to_dict()
    assert payload["passed"] is False
    assert payload["evaluated_latency_budgets"] == 1
    assert payload["evaluated_cost_budgets"] == 0
    assert payload["sampled_stages"] == ["total_ms"]
    assert payload["violations"][0]["kind"] == "latency"
