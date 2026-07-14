"""Exercise Chapter 15's gate with a real production-shape CLI report."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from easycat.cli._app import _register_commands, app
from easycat.debug.export import export_debug_bundle
from easycat.runtime import JournalRecord, TimingInfo

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "15-operate-in-production"
GATE = CHAPTER / "latency_gate.py"


def _production_bundle(path: Path) -> None:
    records: list[JournalRecord] = []
    sequence = 1
    millisecond = 1_000_000
    for index, total_ms in enumerate((500, 600, 700, 800, 900), start=1):
        turn_id = f"turn-{index}"
        for name, offset_ms in (
            ("vad_stop_speaking", 0),
            ("stt_final", 100),
            ("agent_request_started", 150),
            ("agent_delta", 300),
            ("tts_frame", total_ms),
        ):
            records.append(
                JournalRecord(
                    sequence=sequence,
                    session_id="ch15",
                    turn_id=turn_id,
                    name=name,
                    timing=TimingInfo(wall_ns=(index * 10_000 + offset_ms) * millisecond),
                )
            )
            sequence += 1
    export_debug_bundle(SimpleNamespace(journal=SimpleNamespace(read=lambda: records)), path)


def _run_gate(report: str, *, max_ms: int, min_samples: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--metric",
            "vad->tts",
            "--percentile",
            "p95",
            "--max-ms",
            str(max_ms),
            "--min-samples",
            str(min_samples),
        ],
        cwd=ROOT,
        input=report,
        capture_output=True,
        text=True,
        check=False,
    )


def test_latency_gate_consumes_the_real_cli_envelope(tmp_path: Path) -> None:
    bundle = tmp_path / "production.bundle"
    _production_bundle(bundle)
    _register_commands()
    latency = CliRunner().invoke(app, ["latency", str(bundle), "--json"])
    assert latency.exit_code == 0, latency.output

    passed = _run_gate(latency.stdout, max_ms=950, min_samples=5)
    passed_payload = json.loads(passed.stdout)
    assert passed.returncode == 0
    assert passed_payload == {
        "status": "pass",
        "reason": "within_budget",
        "path": str(bundle),
        "metric": "vad->tts",
        "percentile": "p95",
        "observed_ms": pytest.approx(900.0),
        "max_ms": 950.0,
        "sample_count": 5,
        "min_samples": 5,
    }

    over = _run_gate(latency.stdout, max_ms=850, min_samples=5)
    assert over.returncode == 1
    assert json.loads(over.stdout)["reason"] == "over_budget"

    sparse = _run_gate(latency.stdout, max_ms=950, min_samples=6)
    assert sparse.returncode == 1
    assert json.loads(sparse.stdout)["reason"] == "insufficient_samples"

    invalid = _run_gate("{}", max_ms=950, min_samples=5)
    assert invalid.returncode == 2
    assert json.loads(invalid.stdout)["reason"] == "invalid_report"


def test_chapter_does_not_route_production_bundles_through_teaching_fixtures() -> None:
    text = "\n".join(
        (CHAPTER / name).read_text(encoding="utf-8") for name in ("README.md", "EXERCISES.md")
    )

    assert "translate.py" not in text
    assert "translated.ndjson" not in text
    assert "pipe the output into `evals.py`" not in text
    assert "easycat latency PATH --json" in text
    assert "latency_gate.py" in text


def test_shell_examples_quote_latency_metric_redirection() -> None:
    for name in ("README.md", "EXERCISES.md", "latency_gate.py"):
        text = (CHAPTER / name).read_text(encoding="utf-8")
        assert "--metric 'vad->tts'" in text, name
        assert "--metric vad->tts" not in text, name
