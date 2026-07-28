"""Keep the published framework comparison tied to its raw evidence."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPARISON = ROOT / "docs" / "comparison.md"
SNAPSHOT = ROOT / "perf" / "framework-latency-2026-07-28.json"
WORKFLOW = ROOT / ".github" / "workflows" / "framework-benchmark.yml"


def test_published_framework_numbers_match_raw_snapshot() -> None:
    document = COMPARISON.read_text()
    payload = json.loads(SNAPSHOT.read_text())

    assert payload["kind"] == "framework_latency_benchmark"
    assert payload["easycat_revision"]["dirty"] is False
    assert len(payload["easycat_revision"]["commit"]) == 40
    assert payload["generated_at"].startswith("2026-07-28T")
    assert payload["workload"]["iterations"] == 30

    labels = {
        "easycat": "EasyCat",
        "pipecat": "Pipecat",
        "livekit": "LiveKit Agents",
    }
    for framework, label in labels.items():
        result = payload["results"][framework]
        row = f"| {label} | {result['latency_p50_ms']:.2f} | {result['latency_p95_ms']:.2f} | 30 |"
        assert row in document


def test_comparison_exposes_evidence_non_goals_and_refresh_workflow() -> None:
    document = COMPARISON.read_text()
    readme = (ROOT / "README.md").read_text()
    workflow = WORKFLOW.read_text()

    assert "docs/comparison.md" in readme
    assert "EasyCat's explicit non-goals" in document
    assert "../perf/framework-latency-2026-07-28.json" in document
    assert "schedule:" in workflow
    assert "perf/bench_framework_latency.py" in workflow
    assert "framework-latency-${GITHUB_SHA}.json" in workflow
