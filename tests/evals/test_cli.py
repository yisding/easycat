from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from easycat.cli._app import app


def _write_scenario(path: Path, name: str = "echo_flow", regex: str = "hi") -> None:
    path.write_text(
        json.dumps(
            {
                "name": name,
                "turns": [{"user": "hi", "expect": {"response_regex": regex}}],
            }
        ),
        encoding="utf-8",
    )


def test_eval_run_file_json_envelope(cli: CliRunner, tmp_path: Path) -> None:
    scenario = tmp_path / "echo.json"
    _write_scenario(scenario)

    result = cli.invoke(app, ["eval", "run", str(scenario), "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "eval run"
    assert payload["status"] == "ok"
    assert payload["report"]["kind"] == "eval_run"
    assert payload["report"]["status"] == "pass"
    assert payload["report"]["scenario_count"] == 1


def test_eval_run_directory_json(cli: CliRunner, tmp_path: Path) -> None:
    _write_scenario(tmp_path / "a.json", name="a")
    _write_scenario(tmp_path / "b.json", name="b")

    result = cli.invoke(app, ["eval", "run", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["command"] == "eval run"
    assert payload["report"]["scenario_count"] == 2


def test_eval_run_failing_scenario_exit_code(cli: CliRunner, tmp_path: Path) -> None:
    scenario = tmp_path / "bad.json"
    _write_scenario(scenario, regex="will-not-match")

    result = cli.invoke(app, ["eval", "run", str(scenario), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["command"] == "eval run"
    assert payload["status"] == "error"
    assert payload["report"]["status"] == "fail"


def test_eval_run_writes_report_file(cli: CliRunner, tmp_path: Path) -> None:
    scenario = tmp_path / "echo.json"
    _write_scenario(scenario)
    out = tmp_path / "report.json"

    result = cli.invoke(app, ["eval", "run", str(scenario), "--out", str(out), "--json"])

    assert result.exit_code == 0, result.stdout
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
    assert saved["kind"] == "eval_run"


def test_eval_report_json_envelope(cli: CliRunner, tmp_path: Path) -> None:
    scenario = tmp_path / "echo.json"
    _write_scenario(scenario)
    out = tmp_path / "report.json"
    cli.invoke(app, ["eval", "run", str(scenario), "--out", str(out)])

    result = cli.invoke(app, ["eval", "report", str(out), "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "eval report"
    assert payload["status"] == "ok"
    assert payload["report_path"] == str(out)
    assert payload["report"]["kind"] == "eval_run"


def test_eval_report_missing_file_error(cli: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"

    result = cli.invoke(app, ["eval", "report", str(missing), "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["command"] == "eval report"
    assert payload["status"] == "error"
    assert "eval report not found" in payload["message"]


def test_eval_report_bad_schema_error(cli: CliRunner, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": 2, "kind": "eval_run"}), encoding="utf-8")

    result = cli.invoke(app, ["eval", "report", str(bad), "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert "unsupported eval report schema_version" in payload["message"]
