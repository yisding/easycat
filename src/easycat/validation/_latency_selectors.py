"""Pytest selectors for latency validation modes."""

from __future__ import annotations

from easycat.validation._latency_models import LatencyMode

__all__ = ["latency_pytest_args"]

LATENCY_TEST_FILE = "tests/e2e/test_plan_7_latency_benchmark.py"
LATENCY_SMOKE_TEST = "test_single_full_stack_latency_probe"
LATENCY_SWEEP_TEST = "test_latency_benchmark_by_pipeline_flags"


def latency_pytest_args(mode: LatencyMode | str) -> list[str]:
    mode = LatencyMode(mode)
    if mode is LatencyMode.SMOKE:
        return [f"{LATENCY_TEST_FILE}::{LATENCY_SMOKE_TEST}"]
    return [f"{LATENCY_TEST_FILE}::{LATENCY_SWEEP_TEST}"]
