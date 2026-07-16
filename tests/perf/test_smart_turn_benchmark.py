from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

import perf.bench_smart_turn as bench_smart_turn
from easycat.validation.latency import LatencyPercentileStats
from perf.bench_smart_turn import _run_worker, compare_results, summarize_samples


def test_summarize_samples_reports_latency_distribution() -> None:
    summary = summarize_samples([1.0, 2.0, 3.0, 4.0, 10.0])

    assert summary == {
        "runs": 5,
        "p50_ms": 3.0,
        "p90_ms": 10.0,
        "p99_ms": 10.0,
        "mean_ms": 4.0,
        "min_ms": 1.0,
        "max_ms": 10.0,
    }


def test_summarize_samples_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="at least one"):
        summarize_samples([])


def test_summarize_samples_uses_canonical_latency_percentiles() -> None:
    samples = [float(value) for value in range(1, 11)]
    expected = LatencyPercentileStats.from_values(samples)
    assert expected.p50 is not None
    assert expected.p90 is not None
    assert expected.p99 is not None

    summary = summarize_samples(samples)

    assert summary["p50_ms"] == round(expected.p50, 3)
    assert summary["p90_ms"] == round(expected.p90, 3)
    assert summary["p99_ms"] == round(expected.p99, 3)


def test_compare_results_requires_identical_model_and_both_percentiles() -> None:
    easycat = {"model_sha256": "same", "p50_ms": 60.0, "p90_ms": 70.0}
    pipecat = {"model_sha256": "same", "p50_ms": 120.0, "p90_ms": 140.0}

    assert compare_results(easycat, pipecat) == {
        "models_identical": True,
        "easycat_faster": True,
        "easycat_p50_improvement_percent": 50.0,
        "easycat_p90_improvement_percent": 50.0,
    }

    pipecat["model_sha256"] = "different"
    comparison = compare_results(easycat, pipecat)
    assert comparison["models_identical"] is False
    assert comparison["easycat_faster"] is False


def test_worker_script_module_level_imports_are_stdlib_only() -> None:
    """The benchmark script re-executes itself as isolated framework workers.

    The Pipecat worker runs in an environment that contains only Pipecat, so
    the script's module-level imports must stay stdlib-only; easycat may only
    be imported lazily on the parent-process summarization path.
    """
    source = Path(bench_smart_turn.__file__).read_text(encoding="utf-8")
    module_level_imports: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            module_level_imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module_level_imports.add(node.module.split(".")[0])
    non_stdlib = {
        name
        for name in module_level_imports
        if name != "__future__" and name not in sys.stdlib_module_names
    }
    assert not non_stdlib, f"worker script gained non-stdlib module imports: {sorted(non_stdlib)}"


def test_run_worker_summarizes_raw_pipecat_samples_in_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_result = {
        "framework": "pipecat",
        "model_sha256": "abc",
        "intra_op_threads": 1,
        "samples_ms": [1.0, 2.0, 3.0, 4.0, 10.0],
    }

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = "noise\n" + bench_smart_turn._RESULT_PREFIX + json.dumps(worker_result) + "\n"
        return subprocess.CompletedProcess(command, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _run_worker("pipecat", python="python", warmup=1, runs=5)

    assert "samples_ms" not in result
    assert result["framework"] == "pipecat"
    assert result["model_sha256"] == "abc"
    assert result["intra_op_threads"] == 1
    assert result["p50_ms"] == 3.0
    assert result["p90_ms"] == 10.0
    assert result["runs"] == 5


def test_run_worker_reports_framework_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def time_out(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 300
        raise subprocess.TimeoutExpired(command, timeout=300)

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(RuntimeError, match="easycat worker timed out after 300 seconds"):
        _run_worker("easycat", python="python", warmup=1, runs=1)
