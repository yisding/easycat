"""Keep the credential-free checkpoint spine complete and executable."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
TEACHING = ROOT / "docs" / "teaching"
SPINE = TEACHING / "offline_spine.py"


def _load_spine():
    spec = importlib.util.spec_from_file_location("teaching_offline_spine", SPINE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_offline_spine_tracks_one_checkpoint_per_chapter() -> None:
    spine = _load_spine()
    rows = spine.catalog()
    chapter_dirs = sorted(path.name for path in TEACHING.iterdir() if path.name[:2].isdigit())

    assert [row["chapter"] for row in rows] == list(range(16))
    assert [row["folder"] for row in rows] == chapter_dirs
    assert len({row["command"] for row in rows}) == len(rows)
    assert len({row["prediction"] for row in rows}) == len(rows)
    assert len({row["evidence"] for row in rows}) == len(rows)
    assert len({row["reflection"] for row in rows}) == len(rows)
    for row in rows:
        path = ROOT / "docs" / "teaching" / row["folder"] / row["script"]
        expected_extra = "local" if row["chapter"] <= 1 else "quickstart"
        assert path.is_file()
        assert row["setup_command"] == f"uv sync --extra {expected_extra} --group dev"
        assert row["command"] == f"uv run python {path.relative_to(ROOT).as_posix()}"
        assert row["prediction"].strip()
        assert row["evidence"].strip()
        assert row["reflection"].strip()


def test_offline_spine_prioritizes_primary_chapter_questions() -> None:
    spine = _load_spine()
    checkpoints = {row["chapter"]: row for row in spine.catalog()}
    expected = {
        2: ("partial_policy_probe.py", "partial vs final commitment"),
        3: ("timeout_policy_probe.py", "silence-timeout tradeoff"),
        4: ("preroll_probe.py", "VAD pre-roll frame order"),
        5: ("gap_decomposition_probe.py", "blocking first-audio gap"),
        6: ("tts_delivery_probe.py", "sentence-level TTS handoff"),
        7: ("filler_delivery_probe.py", "tool filler delivery"),
        13: ("matrix_probe.py", "provider × transport matrix"),
        14: ("workflow_state_probe.py", "plain workflow bridge contract"),
        15: ("manager_probe.py", "multi-session manager rollback"),
    }

    for chapter, (script, concept) in expected.items():
        assert checkpoints[chapter]["script"] == script
        assert checkpoints[chapter]["concept"] == concept


def test_offline_spine_json_list_is_documented() -> None:
    completed = script_runner.run(
        [sys.executable, str(SPINE), "--json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    readme = (TEACHING / "README.md").read_text(encoding="utf-8")
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert payload["mode"] == "list"
    assert payload["through"] is None
    assert payload["count"] == 16
    assert len(payload["checkpoints"]) == 16
    assert "Hardware-free checkpoint spine" in readme
    assert "uv run python docs/teaching/offline_spine.py --json" in readme
    assert "strips all `*_API_KEY` variables" in readme
    assert "[hardware-free teaching spine](docs/teaching/#hardware-free-checkpoint-spine)" in (
        root_readme
    )
    assert "uv run python docs/teaching/offline_spine.py --run --jobs 4" in root_readme
    assert "prediction prompts, setup commands, evidence cues," in readme
    assert "reflection prompts, and individual commands" in readme
    assert "a mismatch is evidence to explain" in readme
    assert "uv sync --extra quickstart --group dev" in readme
    assert "--run --through 5 --jobs 4" in readme
    assert "--show-evidence" in readme
    assert "row's `observed` value" in readme


def test_offline_spine_lists_only_completed_chapters() -> None:
    completed = script_runner.run(
        [sys.executable, str(SPINE), "--through", "5", "--json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["mode"] == "list"
    assert payload["through"] == 5
    assert payload["count"] == 6
    assert [row["chapter"] for row in payload["checkpoints"]] == list(range(6))


def test_offline_spine_runs_only_completed_chapters() -> None:
    completed = script_runner.run(
        [sys.executable, str(SPINE), "--run", "--through", "1", "--jobs", "2", "--json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)

    assert payload["mode"] == "run"
    assert payload["through"] == 1
    assert payload["count"] == 2
    assert payload["passed"] == 2
    assert payload["failed"] == 0
    assert [row["chapter"] for row in payload["checkpoints"]] == [0, 1]
    assert isinstance(payload["checkpoints"][0]["observed"], list)
    assert payload["checkpoints"][1]["observed"]["accepted"] == 2


def test_offline_spine_rejects_out_of_range_chapter() -> None:
    for chapter in ("-1", "16"):
        completed = script_runner.run(
            [sys.executable, str(SPINE), "--through", chapter],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        assert completed.returncode == 2
        assert "--through must be between 0 and 15" in completed.stderr


def test_offline_spine_show_evidence_requires_run() -> None:
    completed = script_runner.run(
        [sys.executable, str(SPINE), "--show-evidence"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "--show-evidence requires --run" in completed.stderr


def test_offline_spine_human_run_can_show_observed_evidence() -> None:
    completed = script_runner.run(
        [sys.executable, str(SPINE), "--run", "--through", "0", "--show-evidence"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert "PASS     0  audio format boundaries" in completed.stdout
    assert "Predict: Which rates belong to the wire" in completed.stdout
    assert "Observed:" in completed.stdout
    assert '"sample_rate_hz": 8000' in completed.stdout
    assert "Reflect: If any rate surprised you" in completed.stdout


def test_offline_spine_rejects_invalid_evidence_streams(tmp_path: Path) -> None:
    spine = _load_spine()
    script = tmp_path / "docs" / "teaching" / "probe.py"
    script.parent.mkdir(parents=True)
    checkpoint = spine.Checkpoint(
        0,
        ".",
        "probe.py",
        "concept",
        "prediction",
        "evidence",
        "reflection",
    )
    spine.REPO_ROOT = tmp_path

    script.write_text("print('not json')\n", encoding="utf-8")
    invalid_json = spine._run_checkpoint(checkpoint, timeout_s=5)
    assert invalid_json["status"] == "fail"
    assert invalid_json["observed"] is None
    assert str(invalid_json["detail"]).startswith("stdout is not one JSON document:")

    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'ok': True}))\n"
        "print('unexpected noise', file=sys.stderr)\n",
        encoding="utf-8",
    )
    noisy = spine._run_checkpoint(checkpoint, timeout_s=5)
    assert noisy["status"] == "fail"
    assert noisy["observed"] is None
    assert noisy["detail"] == "unexpected stderr: unexpected noise"

    script.write_text(
        "import json, sys\nprint(json.dumps({'ok': True}))\nprint(' ', file=sys.stderr)\n",
        encoding="utf-8",
    )
    whitespace_only_stderr = spine._run_checkpoint(checkpoint, timeout_s=5)
    assert whitespace_only_stderr["status"] == "fail"
    assert whitespace_only_stderr["detail"] == "unexpected stderr: ' \\n'"


def test_offline_spine_text_list_pairs_commands_with_evidence() -> None:
    spine = _load_spine()
    completed = script_runner.run(
        [sys.executable, str(SPINE)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.count("Predict:") == len(spine.catalog())
    assert completed.stdout.count("Setup:") == len(spine.catalog())
    assert completed.stdout.count("Run:") == len(spine.catalog())
    assert completed.stdout.count("Look for:") == len(spine.catalog())
    assert completed.stdout.count("Explain after:") == len(spine.catalog())
    for row in spine.catalog():
        assert row["prediction"] in completed.stdout
        assert row["setup_command"] in completed.stdout
        assert row["command"] in completed.stdout
        assert row["evidence"] in completed.stdout
        assert row["reflection"] in completed.stdout


def test_offline_chapter_cumulative_replay_restores_quickstart_setup() -> None:
    for chapter, number in (("11-journal", 11), ("12-evals-and-latency", 12)):
        readme = (TEACHING / chapter / "README.md").read_text(encoding="utf-8")
        exercises = (TEACHING / chapter / "EXERCISES.md").read_text(encoding="utf-8")

        assert "uv sync --group dev" in readme
        assert "uv sync --extra quickstart --group dev" in exercises
        assert f"--run --through {number} --jobs 4" in exercises


def test_offline_spine_runs_every_checkpoint_without_credentials() -> None:
    pytest.importorskip(
        "openai",
        reason="running the full spine requires the documented quickstart extras",
    )
    root_entries_before = {
        path.name: path.read_bytes() if path.is_file() else None for path in ROOT.iterdir()
    }
    before = {
        path.relative_to(TEACHING): path.read_bytes() if path.is_file() else None
        for path in TEACHING.rglob("*")
    }
    completed = script_runner.run(
        [sys.executable, str(SPINE), "--run", "--jobs", "4", "--json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    payload = json.loads(completed.stdout)

    assert payload["mode"] == "run"
    assert payload["through"] is None
    assert payload["count"] == 16
    assert payload["passed"] == 16
    assert payload["failed"] == 0
    assert {row["status"] for row in payload["checkpoints"]} == {"pass"}
    assert {row["returncode"] for row in payload["checkpoints"]} == {0}
    assert {row["detail"] for row in payload["checkpoints"]} == {""}
    assert all(isinstance(row["observed"], dict | list) for row in payload["checkpoints"])
    after = {
        path.relative_to(TEACHING): path.read_bytes() if path.is_file() else None
        for path in TEACHING.rglob("*")
    }
    assert after == before
    assert {
        path.name: path.read_bytes() if path.is_file() else None for path in ROOT.iterdir()
    } == root_entries_before


def test_offline_spine_documents_workspace_hygiene() -> None:
    source = SPINE.read_text(encoding="utf-8")
    readme = (TEACHING / "README.md").read_text(encoding="utf-8")

    assert 'environment["PYTHONDONTWRITEBYTECODE"] = "1"' in source
    assert "leave files in the checkout" in source
    assert "full run leaves the checkout unchanged" in readme
