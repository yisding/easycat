from __future__ import annotations

import json
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.debug.bundle import FORMAT_VERSION


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


_PROMOTE_TURN = "turn-1"


def _promote_records() -> list[dict]:
    reply = "Refund issued for order 9876."
    return [
        {"sequence": 1, "kind": "event", "name": "turn_started", "turn_id": _PROMOTE_TURN},
        {
            "sequence": 2,
            "kind": "event",
            "name": "agent_final",
            "turn_id": _PROMOTE_TURN,
            "data": {"text": reply, "generated_text": reply},
        },
        {"sequence": 3, "kind": "event", "name": "turn_ended", "turn_id": _PROMOTE_TURN},
    ]


def _write_bundle(path: Path, records: list[dict]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"format_version": FORMAT_VERSION, "provider_versions": {}}),
        )
        zf.writestr("journal.ndjson", "\n".join(json.dumps(r) for r in records))


def test_eval_promote_json_envelope(cli: CliRunner, tmp_path: Path) -> None:
    bundle = tmp_path / "session.zip"
    _write_bundle(bundle, _promote_records())
    out = tmp_path / "test_regressions.py"

    result = cli.invoke(
        app, ["eval", "promote", str(bundle), _PROMOTE_TURN, "--out", str(out), "--json"]
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "eval promote"
    assert payload["status"] == "ok"
    assert payload["turn_id"] == _PROMOTE_TURN
    assert payload["out"] == str(out)
    assert payload["redacted"] is True
    assert payload["include_audio"] is False
    assert out.exists()


def test_eval_promote_missing_turn_error(cli: CliRunner, tmp_path: Path) -> None:
    bundle = tmp_path / "session.zip"
    _write_bundle(bundle, _promote_records())
    out = tmp_path / "test_regressions.py"

    result = cli.invoke(
        app, ["eval", "promote", str(bundle), "missing", "--out", str(out), "--json"]
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["command"] == "eval promote"
    assert payload["status"] == "error"
    assert "No journal records" in payload["message"]
    assert not out.exists()


def test_eval_promote_default_redacts_reply(cli: CliRunner, tmp_path: Path) -> None:
    bundle = tmp_path / "session.zip"
    _write_bundle(bundle, _promote_records())
    out = tmp_path / "test_regressions.py"

    result = cli.invoke(app, ["eval", "promote", str(bundle), _PROMOTE_TURN, "--out", str(out)])

    assert result.exit_code == 0, result.stdout
    source = out.read_text(encoding="utf-8")
    # Default mode never embeds the verbatim reply; it hashes the redacted text.
    assert "Refund issued for order 9876." not in source
    assert "assert_reply_hash" in source


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
