"""Keep Chapter 12's small-sample P95 influence visible."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "12-evals-and-latency"


def load_evals():
    path = CHAPTER / "evals.py"
    spec = importlib.util.spec_from_file_location("teaching_ch12_p95_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_p95_sensitivity_probe_names_the_controlling_fixture() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "p95_sensitivity_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "full_p95_ms": 2420.0,
        "leave_one_out_max_ms": 2420.0,
        "leave_one_out_min_ms": 1160.0,
        "leave_one_out_p95_ms": {
            "tools_01_weather.bundle": 2420.0,
            "turn_01_fast.bundle": 2420.0,
            "turn_02_slow_agent.bundle": 1160.0,
            "turn_03_ghost_interrupt.bundle": 2420.0,
            "turn_04_real_interrupt.bundle": 2420.0,
            "turn_05_medium.bundle": 2420.0,
        },
        "most_influential_bundle": "turn_02_slow_agent.bundle",
        "most_influential_delta_ms": -1260.0,
        "sample_count": 6,
    }


def test_p95_sensitivity_requires_multiple_samples_and_is_not_a_ci() -> None:
    evals = load_evals()

    with pytest.raises(ValueError, match="requires at least two samples"):
        evals.p95_sensitivity({"only.bundle": 100.0})
    assert "not a confidence interval" in " ".join(evals.p95_sensitivity.__doc__.split())


def test_evals_cli_prints_leave_one_out_range(capsys, monkeypatch) -> None:
    evals = load_evals()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evals.py",
            str(CHAPTER / "bundles"),
            str(CHAPTER / "ground_truth.csv"),
        ],
    )

    evals.main()
    output = capsys.readouterr().out
    assert "P95 leave-one-out range" in output
    assert "1160–2420 ms" in output
    assert "turn_02_slow_agent.bundle (-1260 ms)" in output


def test_evals_cli_keeps_reporting_for_one_bundle(tmp_path, capsys, monkeypatch) -> None:
    evals = load_evals()
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    shutil.copyfile(
        CHAPTER / "bundles" / "turn_01_fast.bundle",
        bundles / "turn_01_fast.bundle",
    )
    ground_truth = tmp_path / "ground_truth.csv"
    ground_truth.write_text(
        "bundle,reference_transcript,had_real_barge_in\nturn_01_fast.bundle,what time is it,0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["evals.py", str(bundles), str(ground_truth)])

    evals.main()

    output = capsys.readouterr().out
    assert "P95 leave-one-out range" in output
    assert "n/a (requires at least two bundles)" in output
    assert "=== WER ===" in output
    assert "=== Barge-in F1 ===" in output


def test_lesson_calls_sensitivity_an_influence_check_not_uncertainty() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "p95_sensitivity_probe.py" in lesson
    assert "One turn controls this P95" in lesson
    assert "influence diagnostic, not a confidence interval" in lesson
    assert "leave-one-out rows print `n/a` instead of aborting" in lesson
    assert "five turn bundles" not in lesson
    assert "N standard deviations" not in lesson
