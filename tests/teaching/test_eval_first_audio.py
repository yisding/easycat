from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from easycat.debug.testing import load_bundle
from easycat.runtime import InMemoryRingBuffer

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAPTER = REPO_ROOT / "docs" / "teaching" / "12-evals-and-latency"


def _load_chapter_module(filename: str):
    path = CHAPTER / filename
    module_name = f"teaching_ch12_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _first(records, name: str):
    return next(record for record in records if record["name"] == name)


def test_checked_in_eval_fixtures_end_turn_gap_at_first_audio() -> None:
    primary_bundles = sorted((CHAPTER / "bundles").glob("*.bundle"))
    bundles = primary_bundles + sorted((CHAPTER / "bundles" / "golden").glob("*.bundle"))

    assert len(bundles) == 9
    primary_gaps = []
    for path in bundles:
        records = list(load_bundle(path).records())
        stt_final = _first(records, "stt.final")["data"]["t_ms"]
        first_audio = _first(records, "tts.first_audio")["data"]["t_ms"]
        turn_gap = _first(records, "turn.gap")["data"]["total_gap_ms"]

        assert first_audio - stt_final == pytest.approx(turn_gap), path.name
        if path in primary_bundles:
            primary_gaps.append(turn_gap)

    assert max(primary_gaps) == 2420

    tool_records = list(load_bundle(CHAPTER / "bundles" / "tools_01_weather.bundle").records())
    tool_names = [record["name"] for record in tool_records]
    assert tool_names.index("tts.first_audio") < tool_names.index("tool.call.started")


def test_chapter_evals_reuse_maintained_small_sample_percentiles(capsys) -> None:
    evals = _load_chapter_module("evals.py")
    bundles = CHAPTER / "bundles"
    ground_truth = CHAPTER / "ground_truth.csv"

    original_argv = sys.argv
    try:
        sys.argv = ["evals.py", str(bundles), str(ground_truth)]
        evals.main()
    finally:
        sys.argv = original_argv

    output = capsys.readouterr().out
    assert "P50                                        810 ms" in output
    assert "P95                                       2420 ms" in output
    assert "P95 / P50 ratio                           2.99" in output


def test_slow_agent_budget_does_not_blame_all_sentence_tts(capsys) -> None:
    budget = _load_chapter_module("latency_budget.py")
    path = CHAPTER / "bundles" / "turn_02_slow_agent.bundle"

    assert budget.measure(path) == {
        "stt_final_to_first_token_ms": 2100.0,
        "first_token_to_audio_ms": 320.0,
        "first_audio_gap_ms": 2420.0,
    }

    budget.analyze(path)
    output = capsys.readouterr().out
    assert "stt final → first token          2100 ms" in output
    assert "first token → first audio         320 ms" in output
    assert "first token → first audio         320 ms     budget   400 ms    OK" in output
    assert "tts synth" not in output
    assert "→ done" not in output


def test_fixture_generator_keeps_tool_work_off_the_first_audio_path() -> None:
    generator = _load_chapter_module("generate_bundles.py")
    journal = InMemoryRingBuffer(capacity=100)

    generator._turn(
        journal,
        "tool-turn",
        t_start=10_000.0,
        stt_text="check weather",
        agent_first_token_delay_ms=500,
        tts_spans_ms=[250, 400],
        tool_calls=[
            {
                "name": "weather",
                "elapsed_ms": 1200,
                "result": "sunny",
            }
        ],
    )

    records = [
        {
            "name": record.name,
            "data": record.data,
        }
        for record in journal.read()
    ]
    stt_final = _first(records, "stt.final")["data"]["t_ms"]
    first_audio = _first(records, "tts.first_audio")["data"]["t_ms"]
    turn_gap = _first(records, "turn.gap")["data"]["total_gap_ms"]

    names = [record["name"] for record in records]
    assert first_audio - stt_final == 500 + 250
    assert turn_gap == 750
    assert names.index("tts.first_audio") < names.index("tool.call.started")
