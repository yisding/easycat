"""Keep Chapter 12's metrics honest about data coverage and judge output."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "12-evals-and-latency"


def load_script(filename: str):
    path = CHAPTER / filename
    name = f"teaching_ch12_coverage_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_coverage_probe_names_every_silent_exclusion() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "coverage_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "exact_manifest": None,
        "missing_label": "coverage mismatch: missing labels for ['turn_b.bundle']",
        "missing_turn_gap": (
            "turn_a.bundle: expected exactly one turn.gap, found 0; "
            "missing first-audio turns must not disappear from latency coverage"
        ),
        "multi_turn_bundle": (
            "turn_a.bundle: expected exactly one stt.final, found 2; "
            "split multi-turn runs into one labeled fixture per turn"
        ),
        "one_turn_stats": {
            "hypothesis": "hello",
            "observed_interruption": False,
            "total_gap_ms": 800.0,
        },
    }


def test_ground_truth_rejects_duplicate_and_mistyped_labels(tmp_path: Path) -> None:
    evals = load_script("evals.py")
    duplicate = tmp_path / "duplicate.csv"
    duplicate.write_text(
        "bundle,reference_transcript,had_real_barge_in\na.bundle,hello,0\na.bundle,hello,1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate bundle 'a.bundle'"):
        evals._load_ground_truth(duplicate)

    invalid = tmp_path / "invalid.csv"
    invalid.write_text(
        "bundle,reference_transcript,had_real_barge_in\na.bundle,hello,yes\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="had_real_barge_in must be 0 or 1"):
        evals._load_ground_truth(invalid)


def test_evals_cli_fails_closed_on_incomplete_manifest(tmp_path: Path, monkeypatch) -> None:
    evals = load_script("evals.py")
    partial = tmp_path / "partial.csv"
    partial.write_text(
        "bundle,reference_transcript,had_real_barge_in\nturn_01_fast.bundle,what time is it,0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["evals.py", str(CHAPTER / "bundles"), str(partial)],
    )

    with pytest.raises(SystemExit, match="Invalid eval set: coverage mismatch"):
        evals.main()


def test_single_turn_contract_rejects_missing_and_multi_turn_metrics() -> None:
    evals = load_script("evals.py")
    stt = {"name": "stt.final", "data": {"text": "hello"}}
    gap = {"name": "turn.gap", "data": {"total_gap_ms": 500}}

    with pytest.raises(ValueError, match="expected exactly one turn.gap, found 0"):
        evals._stats_from_records([stt], bundle_name="missing.bundle")
    with pytest.raises(ValueError, match="expected exactly one stt.final, found 2"):
        evals._stats_from_records([stt, stt, gap], bundle_name="multi.bundle")


def test_barge_f1_is_not_invented_without_positive_examples(capsys, monkeypatch) -> None:
    evals = load_script("evals.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evals.py",
            str(CHAPTER / "bundles" / "golden"),
            str(CHAPTER / "bundles" / "golden" / "ground_truth.csv"),
        ],
    )

    evals.main()
    output = capsys.readouterr().out
    assert "TP=0  FP=0  FN=0  TN=3" in output
    assert "precision = n/a   recall = n/a   F1 = n/a" in output


@pytest.mark.asyncio
async def test_llm_judge_closes_client_and_validates_scores(monkeypatch) -> None:
    judge = load_script("llm_judge.py")
    events: list[str] = []
    raw = json.dumps(
        {
            "relevance": 5,
            "fluency": 4,
            "appropriate_length": 3,
            "reasoning": "clear",
        }
    )

    class Completions:
        async def create(self, **_kwargs):
            events.append("judge.request")
            message = types.SimpleNamespace(content=raw)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    class Client:
        def __init__(self) -> None:
            self.chat = types.SimpleNamespace(completions=Completions())

        async def __aenter__(self):
            events.append("client.open")
            return self

        async def __aexit__(self, _exc_type, _exc, _tb) -> None:
            events.append("client.close")

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(judge, "AsyncOpenAI", Client)
    monkeypatch.setattr(judge, "extract_transcript", lambda _path: "User: hi\nBot: hello")

    result = await judge.judge(Path("turn.bundle"))
    assert result == json.loads(raw)
    assert events == ["client.open", "judge.request", "client.close"]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ([], "judge returned a non-object"),
        (
            {"relevance": 0, "fluency": 4, "appropriate_length": 3, "reasoning": "x"},
            "judge returned invalid relevance score",
        ),
        (
            {"relevance": 5, "fluency": "4", "appropriate_length": 3, "reasoning": "x"},
            "judge returned invalid fluency score",
        ),
        (
            {"relevance": 5, "fluency": 4, "appropriate_length": 3},
            "judge returned invalid reasoning",
        ),
    ],
)
def test_llm_judge_rejects_invalid_json_objects(payload, error: str) -> None:
    judge = load_script("llm_judge.py")
    assert judge.parse_judgment(json.dumps(payload))["error"] == error


def test_chapter_teaches_coverage_before_point_estimates() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split())

    assert "Coverage before scores" in readme
    assert "coverage_probe.py" in readme
    assert "missing first-audio turns" in normalized
    assert "coverage_probe.py" in exercises
    assert "no universal good WER" in normalized
    assert "Calibrate" in normalized
    assert "±2%" not in exercises
