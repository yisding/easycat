from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "12-evals-and-latency"
EVALS = runpy.run_path(str(CHAPTER / "evals.py"))
nearest_rank_percentile = EVALS["_nearest_rank_percentile"]
bundle_stats = EVALS["_bundle_stats"]


def test_nearest_rank_p95_keeps_the_observed_tail() -> None:
    values = [2200.0, 1150.0, 2900.0, 900.0, 1250.0, 1700.0]

    assert nearest_rank_percentile(values, 0.95) == 2900.0
    assert values == [2200.0, 1150.0, 2900.0, 900.0, 1250.0, 1700.0]


def test_chapter_12_fixture_set_has_six_bundles_and_a_2900_ms_p95() -> None:
    bundles = sorted((CHAPTER / "bundles").glob("*.bundle"))
    latencies = [bundle_stats(path)["total_gap_ms"] for path in bundles]

    assert len(bundles) == 6
    assert all(latency is not None for latency in latencies)
    assert nearest_rank_percentile(latencies, 0.95) == 2900.0


def test_chapter_12_docs_track_the_six_fixture_inventory() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    generator = (CHAPTER / "generate_bundles.py").read_text(encoding="utf-8")

    assert "## The six pre-recorded bundles" in readme
    assert "nearest-rank P95 is the slowest turn" in readme
    assert "0% on all six" in readme
    assert "The six chapter-12 fixtures" in exercises
    assert "P95 over 6 bundles" in exercises
    assert '"""Build six eval bundles for chapter 12.' in generator
