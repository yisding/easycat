from __future__ import annotations

import statistics
from datetime import UTC, datetime

import pytest

from easycat.validation.latency import (
    DEFAULT_BUDGETS,
    LatencyBudget,
    LatencyComparisonThresholds,
    LatencyMode,
    LatencyPercentileStats,
    LatencySample,
    LatencyStageDurations,
    build_latency_artifact,
    compare_latency_baseline,
    evaluate_budgets,
)


def _make_sample(
    *,
    sample_id: str,
    condition_id: str = "baseline",
    total_ms: float | None = 500.0,
    stt_ms: float | None = 100.0,
    tts_ttfb_ms: float | None = 150.0,
    llm_ttft_ms: float | None = 200.0,
    warmup: bool = False,
    failure_class: str | None = None,
    provider: dict[str, str] | None = None,
) -> LatencySample:
    return LatencySample(
        sample_id=sample_id,
        condition_id=condition_id,
        warmup=warmup,
        timestamp_source="event_monotonic",
        provider=provider or {"stt": "openai-realtime", "region": "us-east-1"},
        model={"llm": "gpt-5.4", "tts": "gpt-4o-mini-tts"},
        transport={"kind": "websocket"},
        debug={"journal": "off"},
        stages=LatencyStageDurations(
            total_ms=total_ms,
            stt_ms=stt_ms,
            tts_ttfb_ms=tts_ttfb_ms,
            llm_ttft_ms=llm_ttft_ms,
        ),
        failure_class=failure_class,
    )


# ---------------------------------------------------------------------------
# LatencyPercentileStats
# ---------------------------------------------------------------------------


def test_latency_percentile_stats_from_values_empty_input() -> None:
    stats = LatencyPercentileStats.from_values([])

    assert stats.count == 0
    assert stats.p50 is None
    assert stats.p90 is None
    assert stats.p95 is None
    assert stats.p99 is None


def test_latency_percentile_stats_from_values_skips_none() -> None:
    values = [100.0, None, 200.0, None, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]

    stats = LatencyPercentileStats.from_values(values)

    assert stats.count == 10
    cleaned = [v for v in values if v is not None]
    expected = statistics.quantiles(cleaned, n=100, method="exclusive")
    assert stats.p50 == pytest.approx(expected[49])
    assert stats.p90 == pytest.approx(expected[89])
    assert stats.p95 == pytest.approx(expected[94])
    assert stats.p99 == pytest.approx(expected[98])


def test_latency_percentile_stats_uses_linear_interpolation() -> None:
    # Ten samples, evenly spaced. With exclusive (N+1)*p method these
    # percentiles are interpolated, not nearest-rank.
    values = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]

    stats = LatencyPercentileStats.from_values(values)

    # Expected per `statistics.quantiles(..., method="exclusive")` semantics
    assert stats.p50 == pytest.approx(550.0)
    assert stats.p90 == pytest.approx(990.0)
    assert stats.p95 == pytest.approx(1045.0)
    assert stats.p99 == pytest.approx(1089.0)
    assert stats.count == 10


def test_latency_percentile_stats_two_value_input_still_interpolates() -> None:
    stats = LatencyPercentileStats.from_values([100.0, 200.0])

    assert stats.count == 2
    # statistics.quantiles with n=100, method="exclusive" on [100, 200]
    # gives a linearly-interpolated p50 == 150.0 (not nearest-rank == 100/200)
    assert stats.p50 == pytest.approx(150.0)
    assert stats.p95 == pytest.approx(285.0)


def test_latency_percentile_stats_single_value_short_circuits() -> None:
    # statistics.quantiles requires n >= 2 inputs, so a single-value input is a
    # special case: count=1 and all percentiles equal that single value.
    stats = LatencyPercentileStats.from_values([742.0])

    assert stats.count == 1
    assert stats.p50 == pytest.approx(742.0)
    assert stats.p90 == pytest.approx(742.0)
    assert stats.p95 == pytest.approx(742.0)
    assert stats.p99 == pytest.approx(742.0)


def test_latency_percentile_stats_one_tail_spike_pulls_p95_high() -> None:
    # The operator intuition that motivates exclusive: one bad sample in 20
    # should push p95 close to that bad value, not get smoothed away to a
    # number near the median.
    values = [500.0] * 19 + [2000.0]

    stats = LatencyPercentileStats.from_values(values)

    assert stats.count == 20
    assert stats.p50 == pytest.approx(500.0)
    # Under exclusive (N+1)*p, idx 94 on this distribution is 1925.
    # Under inclusive it would be 575 -- which is the footgun this pins.
    assert stats.p95 == pytest.approx(1925.0)


# ---------------------------------------------------------------------------
# build_latency_artifact: percentiles block
# ---------------------------------------------------------------------------


def test_build_latency_artifact_emits_percentiles_block_per_stage() -> None:
    samples = [
        _make_sample(
            sample_id=f"baseline-{i}",
            condition_id="baseline",
            total_ms=float(100 + i * 100),
            stt_ms=float(10 + i * 10),
            tts_ttfb_ms=float(20 + i * 20),
            llm_ttft_ms=float(30 + i * 30),
        )
        for i in range(10)
    ]

    artifact = build_latency_artifact(
        mode=LatencyMode.SWEEP,
        samples=samples,
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert "percentiles" in artifact
    percentiles = artifact["percentiles"]
    assert set(percentiles) >= {"overall", "by_condition"}
    overall = percentiles["overall"]
    # Stage keys present
    assert "total_ms" in overall
    assert "stt_ms" in overall
    assert "tts_ttfb_ms" in overall
    assert "llm_ttft_ms" in overall

    total_overall = overall["total_ms"]
    expected_total = statistics.quantiles(
        [100.0 + i * 100 for i in range(10)], n=100, method="exclusive"
    )
    assert total_overall["count"] == 10
    assert total_overall["p50"] == pytest.approx(expected_total[49])
    assert total_overall["p95"] == pytest.approx(expected_total[94])

    by_condition = percentiles["by_condition"]
    assert "baseline" in by_condition
    assert by_condition["baseline"]["total_ms"]["count"] == 10


def test_build_latency_artifact_percentiles_skip_warmup_and_failed_samples() -> None:
    samples = [
        _make_sample(sample_id="warmup-1", warmup=True, total_ms=10_000.0),
        _make_sample(sample_id="fail-1", failure_class="provider_timeout", total_ms=99_999.0),
        _make_sample(sample_id="ok-1", total_ms=100.0),
        _make_sample(sample_id="ok-2", total_ms=200.0),
        _make_sample(sample_id="ok-3", total_ms=300.0),
    ]

    artifact = build_latency_artifact(
        mode=LatencyMode.SWEEP,
        samples=samples,
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    overall = artifact["percentiles"]["overall"]["total_ms"]
    assert overall["count"] == 3
    # The warmup/failed values (10_000, 99_999) must not show up in p95.
    # Exclusive method can extrapolate above the max sample (300) on small
    # n, so the leak-detection bound is set well below the warmup/fail values.
    assert overall["p95"] is not None
    assert overall["p95"] < 1000.0


def test_build_latency_artifact_percentiles_split_by_condition() -> None:
    baseline_samples = [_make_sample(sample_id=f"b-{i}", total_ms=200.0) for i in range(5)]
    other_samples = [
        _make_sample(sample_id=f"o-{i}", condition_id="alt", total_ms=900.0) for i in range(5)
    ]

    artifact = build_latency_artifact(
        mode=LatencyMode.SWEEP,
        samples=[*baseline_samples, *other_samples],
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    by_condition = artifact["percentiles"]["by_condition"]
    assert set(by_condition) == {"baseline", "alt"}
    assert by_condition["baseline"]["total_ms"]["count"] == 5
    assert by_condition["alt"]["total_ms"]["count"] == 5
    # All values for a condition are equal, so all percentiles match
    assert by_condition["baseline"]["total_ms"]["p95"] == pytest.approx(200.0)
    assert by_condition["alt"]["total_ms"]["p95"] == pytest.approx(900.0)
    overall = artifact["percentiles"]["overall"]["total_ms"]
    assert overall["count"] == 10


def test_build_latency_artifact_percentiles_empty_when_no_eligible_samples() -> None:
    artifact = build_latency_artifact(
        mode=LatencyMode.SMOKE,
        samples=[],
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    overall = artifact["percentiles"]["overall"]
    assert overall["total_ms"]["count"] == 0
    assert overall["total_ms"]["p50"] is None
    assert overall["total_ms"]["p95"] is None
    assert artifact["percentiles"]["by_condition"] == {}


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_default_budgets_cover_required_stages() -> None:
    by_stage = {budget.stage: budget for budget in DEFAULT_BUDGETS}

    assert "total_ms" in by_stage
    assert by_stage["total_ms"].max_ms <= 1500
    assert by_stage["total_ms"].percentile == "p95"

    assert "tts_ttfb_ms" in by_stage
    assert by_stage["tts_ttfb_ms"].max_ms <= 200
    assert by_stage["tts_ttfb_ms"].percentile == "p95"

    assert "llm_ttft_ms" in by_stage
    assert by_stage["llm_ttft_ms"].max_ms <= 500
    assert by_stage["llm_ttft_ms"].percentile == "p95"


def test_evaluate_budgets_returns_empty_when_all_pass() -> None:
    percentiles = {
        "overall": {
            "total_ms": {"p50": 500.0, "p90": 900.0, "p95": 1100.0, "p99": 1300.0, "count": 20},
            "tts_ttfb_ms": {"p50": 80.0, "p90": 120.0, "p95": 150.0, "p99": 180.0, "count": 20},
            "llm_ttft_ms": {"p50": 200.0, "p90": 350.0, "p95": 400.0, "p99": 470.0, "count": 20},
        },
        "by_condition": {},
    }

    violations = evaluate_budgets(percentiles, DEFAULT_BUDGETS)

    assert violations == []


def test_evaluate_budgets_flags_overall_violation() -> None:
    percentiles = {
        "overall": {
            "total_ms": {"p50": 500.0, "p90": 900.0, "p95": 1700.0, "p99": 1900.0, "count": 20},
            "tts_ttfb_ms": {"p50": 80.0, "p90": 120.0, "p95": 150.0, "p99": 180.0, "count": 20},
            "llm_ttft_ms": {"p50": 200.0, "p90": 350.0, "p95": 400.0, "p99": 470.0, "count": 20},
        },
        "by_condition": {},
    }
    budgets = (LatencyBudget(stage="total_ms", max_ms=1500.0, percentile="p95"),)

    violations = evaluate_budgets(percentiles, budgets)

    assert len(violations) == 1
    violation = violations[0]
    assert violation.stage == "total_ms"
    assert violation.percentile == "p95"
    assert violation.observed_ms == pytest.approx(1700.0)
    assert violation.budget_ms == pytest.approx(1500.0)
    assert violation.scope == "overall"


def test_evaluate_budgets_flags_per_condition_violation() -> None:
    percentiles = {
        "overall": {
            "total_ms": {"p50": 500.0, "p90": 900.0, "p95": 1100.0, "p99": 1300.0, "count": 20},
        },
        "by_condition": {
            "baseline": {
                "total_ms": {
                    "p50": 500.0,
                    "p90": 900.0,
                    "p95": 1100.0,
                    "p99": 1300.0,
                    "count": 10,
                },
            },
            "slow": {
                "total_ms": {
                    "p50": 1400.0,
                    "p90": 1600.0,
                    "p95": 1800.0,
                    "p99": 1900.0,
                    "count": 10,
                },
            },
        },
    }
    budgets = (LatencyBudget(stage="total_ms", max_ms=1500.0, percentile="p95"),)

    violations = evaluate_budgets(percentiles, budgets)

    scopes = {v.scope for v in violations}
    assert "condition:slow" in scopes
    assert "condition:baseline" not in scopes


def test_evaluate_budgets_skips_missing_percentile_values() -> None:
    percentiles = {
        "overall": {
            "total_ms": {"p50": None, "p90": None, "p95": None, "p99": None, "count": 0},
        },
        "by_condition": {},
    }
    budgets = (LatencyBudget(stage="total_ms", max_ms=1500.0, percentile="p95"),)

    violations = evaluate_budgets(percentiles, budgets)

    # A missing percentile (None) is not a violation; it's "no data".
    assert violations == []


def test_evaluate_budgets_skips_stages_absent_from_percentiles() -> None:
    percentiles = {
        "overall": {
            "total_ms": {"p50": 500.0, "p90": 900.0, "p95": 1100.0, "p99": 1300.0, "count": 20},
        },
        "by_condition": {},
    }
    # tts_ttfb_ms is not present at all
    budgets = (LatencyBudget(stage="tts_ttfb_ms", max_ms=200.0, percentile="p95"),)

    violations = evaluate_budgets(percentiles, budgets)

    assert violations == []


def test_build_latency_artifact_includes_budget_violations_when_present() -> None:
    samples = [
        _make_sample(sample_id=f"s-{i}", total_ms=3000.0, tts_ttfb_ms=500.0, llm_ttft_ms=900.0)
        for i in range(10)
    ]

    artifact = build_latency_artifact(
        mode=LatencyMode.SWEEP,
        samples=samples,
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert "budget_violations" in artifact
    violations = artifact["budget_violations"]
    assert isinstance(violations, list)
    assert violations, "expected at least one budget violation for total_ms"
    stages = {entry["stage"] for entry in violations}
    assert "total_ms" in stages


def test_build_latency_artifact_emits_empty_budget_violations_when_passing() -> None:
    samples = [
        _make_sample(sample_id=f"s-{i}", total_ms=400.0, tts_ttfb_ms=100.0, llm_ttft_ms=300.0)
        for i in range(10)
    ]

    artifact = build_latency_artifact(
        mode=LatencyMode.SWEEP,
        samples=samples,
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert "budget_violations" in artifact
    assert artifact["budget_violations"] == []


# ---------------------------------------------------------------------------
# Percentile-aware baseline comparison
# ---------------------------------------------------------------------------


def test_latency_comparison_thresholds_defaults_to_p95() -> None:
    thresholds = LatencyComparisonThresholds()

    assert thresholds.regression_percentile == "p95"


def _comparison_artifact(
    totals: list[float],
    *,
    condition_id: str = "baseline",
) -> dict[str, object]:
    samples = [
        _make_sample(
            sample_id=f"{condition_id}-{index}",
            condition_id=condition_id,
            total_ms=total_ms,
        )
        for index, total_ms in enumerate(totals)
    ]
    return build_latency_artifact(
        mode=LatencyMode.SWEEP,
        samples=samples,
        generated_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
        baseline={
            "comparison": "baseline",
            "conditions": {condition_id: {"version": "2026-05-22"}},
        },
    )


def test_latency_baseline_comparison_p95_flags_when_p50_would_not() -> None:
    # Construct distributions where the medians are essentially equal,
    # but the p95 tail is significantly worse on `current`.
    baseline_totals = [500.0] * 19 + [550.0]  # p50 == 500, p95 ~= 550
    current_totals = [500.0] * 19 + [2000.0]  # p50 == 500, p95 ~= 2000

    baseline = _comparison_artifact(baseline_totals)
    current = _comparison_artifact(current_totals)

    comparison = compare_latency_baseline(
        current,
        baseline,
        thresholds=LatencyComparisonThresholds(
            relative_regression=0.2,
            absolute_regression_ms=200.0,
            min_samples=3,
            regression_percentile="p95",
        ),
    )

    assert comparison["status"] == "fail"
    assert comparison["conditions"][0]["status"] == "fail"
    assert comparison["conditions"][0]["failure_class"] == "easycat_latency_regression"


def test_latency_baseline_comparison_p50_does_not_flag_tail_regression() -> None:
    baseline_totals = [500.0] * 19 + [550.0]
    current_totals = [500.0] * 19 + [2000.0]

    baseline = _comparison_artifact(baseline_totals)
    current = _comparison_artifact(current_totals)

    comparison = compare_latency_baseline(
        current,
        baseline,
        thresholds=LatencyComparisonThresholds(
            relative_regression=0.2,
            absolute_regression_ms=200.0,
            min_samples=3,
            regression_percentile="p50",
        ),
    )

    assert comparison["status"] == "pass"
    assert comparison["conditions"][0]["status"] == "pass"


def test_comparison_thresholds_rejects_unknown_percentile() -> None:
    # Today the percentile lookup silently falls back to p50 on a typo; that
    # is a silent footgun. An unknown percentile must raise a clear error
    # naming the offending value.
    bad_thresholds = LatencyComparisonThresholds(
        relative_regression=0.2,
        absolute_regression_ms=200.0,
        min_samples=3,
        regression_percentile="p42",
    )
    baseline = _comparison_artifact([500.0] * 20)
    current = _comparison_artifact([500.0] * 20)

    with pytest.raises((ValueError, KeyError), match="p42"):
        compare_latency_baseline(current, baseline, thresholds=bad_thresholds)


# ---------------------------------------------------------------------------
# evaluate_budgets input strictness
# ---------------------------------------------------------------------------


def test_evaluate_budgets_raises_on_non_numeric_observed() -> None:
    # Today evaluate_budgets silently swallows non-numeric percentile values
    # via a try/except float() block. The percentiles dict is always produced
    # by EasyCat itself and only ever contains None | float, so a non-numeric
    # entry is a programmer bug and must surface, not be hidden.
    percentiles = {
        "overall": {
            "total_ms": {
                "p50": 500.0,
                "p90": 900.0,
                "p95": "not a number",
                "p99": 1300.0,
                "count": 20,
            },
        },
        "by_condition": {},
    }
    budgets = (LatencyBudget(stage="total_ms", max_ms=1500.0, percentile="p95"),)

    with pytest.raises((TypeError, ValueError)):
        evaluate_budgets(percentiles, budgets)


# ---------------------------------------------------------------------------
# Public package re-exports
# ---------------------------------------------------------------------------


def test_validation_package_reexports_phase1_symbols() -> None:
    # The Phase 1 latency symbols must be importable from the validation
    # package surface, not just from the submodule.
    from easycat.validation import (
        DEFAULT_BUDGETS,
        LatencyBudget,
        LatencyBudgetViolation,
        LatencyPercentileStats,
        evaluate_budgets,
    )

    # Sanity-check that they are the same objects as in the submodule (not
    # name-shadowed re-bindings).
    from easycat.validation import latency as _latency_module

    assert LatencyBudget is _latency_module.LatencyBudget
    assert LatencyBudgetViolation is _latency_module.LatencyBudgetViolation
    assert LatencyPercentileStats is _latency_module.LatencyPercentileStats
    assert DEFAULT_BUDGETS is _latency_module.DEFAULT_BUDGETS
    assert evaluate_budgets is _latency_module.evaluate_budgets
