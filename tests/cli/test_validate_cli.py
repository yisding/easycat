from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.validation.latency import LatencyMode
from easycat.validation.report import ArtifactRef, ValidationCheck
from easycat.validation.runner import ValidationRunResult

from ._validation_helpers import _validation_run


@pytest.mark.parametrize(
    "lane",
    ["quick", "socket", "stress", "contracts", "latency", "live", "release"],
)
def test_validate_lane_help_names_report_artifact_paths(cli: CliRunner, lane: str) -> None:
    result = cli.invoke(app, ["validate", lane, "--help"])
    help_text = " ".join(result.stdout.split())

    assert result.exit_code == 0
    assert "--artifacts-dir" in result.stdout
    assert "runs/" in help_text
    assert "report.json" in help_text
    assert "latest.json" in help_text


def test_validate_quick_cli_writes_report_and_prints_human_summary(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "validation.json"
    called: dict[str, object] = {}

    def fake_run_validation_slice(slice_name: str, **kwargs) -> ValidationRunResult:
        called["slice_name"] = slice_name
        called.update(kwargs)
        run = _validation_run()
        result_report = tmp_path / "run" / "report.json"
        result_report.parent.mkdir()
        result_report.write_text(run.to_json())
        # Mirror the real runner contract: it is the authoritative writer of
        # the requested ``--report`` path (the CLI no longer copies it).
        requested = kwargs.get("report_path")
        if requested is not None:
            Path(requested).write_text(run.to_json())
        return ValidationRunResult(
            run=run,
            run_dir=result_report.parent,
            report_path=requested or result_report,
            exit_code=0,
        )

    monkeypatch.setattr("easycat.cli.validate.run_validation_slice", fake_run_validation_slice)

    result = cli.invoke(app, ["validate", "quick", "--report", str(report_path)])

    assert result.exit_code == 0
    assert "quick: pass" in result.stdout
    assert report_path.exists()
    assert called["slice_name"] == "quick"
    assert called["report_path"] == report_path


def test_validate_quick_cli_json_uses_standard_stdout_envelope(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_validation_slice(slice_name: str, **kwargs) -> ValidationRunResult:
        run = _validation_run()
        result_report = tmp_path / "run" / "report.json"
        result_report.parent.mkdir()
        result_report.write_text(run.to_json())
        return ValidationRunResult(
            run=run,
            run_dir=result_report.parent,
            report_path=result_report,
            exit_code=0,
        )

    monkeypatch.setattr("easycat.cli.validate.run_validation_slice", fake_run_validation_slice)

    result = cli.invoke(app, ["validate", "quick", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "validate quick"
    assert payload["status"] == "ok"
    assert payload["validation"]["kind"] == "validation_run"


@pytest.mark.parametrize("json_output", [False, True])
def test_validate_quick_rejects_non_source_checkout(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EASYCAT_VALIDATION_PYTEST_COMMAND", raising=False)
    monkeypatch.delenv("EASYCAT_VALIDATION_TEST_PATHS", raising=False)
    monkeypatch.delenv("EASYCAT_VALIDATION_TEST_ROOT", raising=False)
    args = ["validate", "quick", *(["--json"] if json_output else [])]

    result = cli.invoke(app, args)

    assert result.exit_code == 2
    if json_output:
        payload = json.loads(result.stdout)
        assert payload["command"] == "validate quick"
        assert payload["status"] == "error"
        assert payload["exit_code"] == 2
        assert "require the EasyCat source checkout" in payload["message"]
    else:
        assert "require the EasyCat source checkout" in result.stderr


def test_validate_contracts_cli_json_uses_standard_stdout_envelope(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, object] = {}

    def fake_run_validation_slice(slice_name: str, **kwargs) -> ValidationRunResult:
        called["slice_name"] = slice_name
        called.update(kwargs)
        run = _validation_run(
            run_id="20260521T120000Z-contracts-12345",
            checks=[ValidationCheck(name="pytest.contracts", status="pass", duration_s=1.0)],
        )
        result_report = tmp_path / "run" / "report.json"
        result_report.parent.mkdir()
        result_report.write_text(run.to_json())
        return ValidationRunResult(
            run=run,
            run_dir=result_report.parent,
            report_path=result_report,
            exit_code=0,
        )

    monkeypatch.setattr("easycat.cli.validate.run_validation_slice", fake_run_validation_slice)

    result = cli.invoke(app, ["validate", "contracts", "--json"])

    assert result.exit_code == 0
    assert called["slice_name"] == "contracts"
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "validate contracts"
    assert payload["status"] == "ok"
    assert payload["validation"]["checks"][0]["name"] == "pytest.contracts"


def test_validate_quick_cli_show_output_streams_captured_logs(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_path = tmp_path / "run" / "stdout.log"
    stderr_path = tmp_path / "run" / "stderr.log"
    report_path = tmp_path / "run" / "report.json"
    stdout_path.parent.mkdir()
    stdout_path.write_text("pytest stdout\n", encoding="utf-8")
    stderr_path.write_text("pytest stderr\n", encoding="utf-8")

    def fake_run_validation_slice(slice_name: str, **kwargs) -> ValidationRunResult:
        run = _validation_run(
            artifacts={
                "stdout": ArtifactRef(kind="stdout", path=str(stdout_path)),
                "stderr": ArtifactRef(kind="stderr", path=str(stderr_path)),
                "report": ArtifactRef(kind="validation_report", path=str(report_path)),
            }
        )
        report_path.write_text(run.to_json(), encoding="utf-8")
        return ValidationRunResult(
            run=run,
            run_dir=stdout_path.parent,
            report_path=report_path,
            exit_code=0,
        )

    monkeypatch.setattr("easycat.cli.validate.run_validation_slice", fake_run_validation_slice)

    result = cli.invoke(app, ["validate", "quick", "--show-output"])

    assert result.exit_code == 0
    assert "pytest stdout" in result.stdout
    assert "quick: pass" in result.stdout
    assert "pytest stderr" in result.stderr


def test_validate_quick_cli_show_output_survives_non_utf8_captured_logs(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A captured log holding non-UTF-8 bytes must not fail the run (gh 1108).

    Slices capture child stdout/stderr byte-for-byte, so a tool emitting
    cp1252/latin-1 text leaves a log that is not valid UTF-8. A strict
    ``read_text`` raised ``UnicodeDecodeError`` *after* the validation had
    already completed, turning a finished run into a traceback.
    """
    stdout_path = tmp_path / "run" / "stdout.log"
    stderr_path = tmp_path / "run" / "stderr.log"
    report_path = tmp_path / "run" / "report.json"
    stdout_path.parent.mkdir()
    # Valid text plus a byte no UTF-8 decoder accepts.
    stdout_path.write_bytes(b"pytest stdout\n\xff\xfe latin1 tail\n")
    stderr_path.write_bytes(b"pytest stderr\n\x92smart quote\n")

    def fake_run_validation_slice(slice_name: str, **kwargs) -> ValidationRunResult:
        run = _validation_run(
            artifacts={
                "stdout": ArtifactRef(kind="stdout", path=str(stdout_path)),
                "stderr": ArtifactRef(kind="stderr", path=str(stderr_path)),
                "report": ArtifactRef(kind="validation_report", path=str(report_path)),
            }
        )
        report_path.write_text(run.to_json(), encoding="utf-8")
        return ValidationRunResult(
            run=run,
            run_dir=stdout_path.parent,
            report_path=report_path,
            exit_code=0,
        )

    monkeypatch.setattr("easycat.cli.validate.run_validation_slice", fake_run_validation_slice)

    result = cli.invoke(app, ["validate", "quick", "--show-output"])

    assert result.exit_code == 0
    # The decodable part still reaches the operator; the rest is replaced.
    assert "pytest stdout" in result.stdout
    assert "latin1 tail" in result.stdout
    assert "quick: pass" in result.stdout
    assert "pytest stderr" in result.stderr


def test_validate_quick_cli_json_show_output_keeps_stdout_parseable(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_path = tmp_path / "run" / "stdout.log"
    stderr_path = tmp_path / "run" / "stderr.log"
    report_path = tmp_path / "run" / "report.json"
    stdout_path.parent.mkdir()
    stdout_path.write_text("pytest stdout\n", encoding="utf-8")
    stderr_path.write_text("pytest stderr\n", encoding="utf-8")

    def fake_run_validation_slice(slice_name: str, **kwargs) -> ValidationRunResult:
        run = _validation_run(
            artifacts={
                "stdout": ArtifactRef(kind="stdout", path=str(stdout_path)),
                "stderr": ArtifactRef(kind="stderr", path=str(stderr_path)),
                "report": ArtifactRef(kind="validation_report", path=str(report_path)),
            }
        )
        report_path.write_text(run.to_json(), encoding="utf-8")
        return ValidationRunResult(
            run=run,
            run_dir=stdout_path.parent,
            report_path=report_path,
            exit_code=0,
        )

    monkeypatch.setattr("easycat.cli.validate.run_validation_slice", fake_run_validation_slice)

    result = cli.invoke(app, ["validate", "quick", "--json", "--show-output"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "validate quick"
    assert "pytest stdout" in result.stderr
    assert "pytest stderr" in result.stderr
    assert "pytest stdout" not in result.stdout
    assert "pytest stderr" not in result.stdout


def test_validate_socket_cli_returns_validation_exit_code(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_validation_slice(slice_name: str, **kwargs) -> ValidationRunResult:
        run = _validation_run(status="fail", exit_code=1, tool_exit_codes={"pytest": 5})
        result_report = tmp_path / "run" / "report.json"
        result_report.parent.mkdir()
        result_report.write_text(run.to_json())
        return ValidationRunResult(
            run=run,
            run_dir=result_report.parent,
            report_path=result_report,
            exit_code=1,
        )

    monkeypatch.setattr("easycat.cli.validate.run_validation_slice", fake_run_validation_slice)

    result = cli.invoke(app, ["validate", "socket"])

    assert result.exit_code == 1
    assert "socket: fail" in result.stdout


def test_validate_live_cli_json_uses_standard_stdout_envelope(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, object] = {}

    def fake_run_live_validation(**kwargs) -> ValidationRunResult:
        called.update(kwargs)
        run = _validation_run()
        result_report = tmp_path / "run" / "report.json"
        result_report.parent.mkdir()
        result_report.write_text(run.to_json())
        return ValidationRunResult(
            run=run,
            run_dir=result_report.parent,
            report_path=result_report,
            exit_code=0,
        )

    monkeypatch.setattr("easycat.cli.validate.run_live_validation", fake_run_live_validation)

    result = cli.invoke(
        app,
        [
            "validate",
            "live",
            "--provider",
            "openai",
            "--surface",
            "stt",
            "--strict",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert called["providers"] == ["openai"]
    assert called["surfaces"] == ["stt"]
    assert called["strict"] is True
    payload = json.loads(result.stdout)
    assert payload["command"] == "validate live"
    assert payload["status"] == "ok"
    assert payload["validation"]["kind"] == "validation_run"


def test_validate_release_cli_json_uses_standard_stdout_envelope(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, object] = {}

    def fake_run_release_validation(**kwargs) -> ValidationRunResult:
        called.update(kwargs)
        run = _validation_run(
            run_id="20260523T120000Z-release-12345",
            command=["easycat", "validate", "release"],
            checks=[ValidationCheck(name="release.quick", status="pass", duration_s=1.0)],
        )
        result_report = tmp_path / "run" / "report.json"
        result_report.parent.mkdir()
        result_report.write_text(run.to_json())
        return ValidationRunResult(
            run=run,
            run_dir=result_report.parent,
            report_path=result_report,
            exit_code=0,
        )

    monkeypatch.setattr(
        "easycat.cli.validate.run_release_validation",
        fake_run_release_validation,
    )

    result = cli.invoke(
        app,
        [
            "validate",
            "release",
            "--python",
            "3.12",
            "--extra",
            "openai",
            "--provider",
            "openai",
            "--surface",
            "stt",
            "--latency-smoke",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert called["python_version"] == "3.12"
    assert called["extras"] == ["openai"]
    assert called["providers"] == ["openai"]
    assert called["surfaces"] == ["stt"]
    assert called["latency_mode"] == LatencyMode.SMOKE
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "validate release"
    assert payload["status"] == "ok"
    assert payload["validation"]["checks"][0]["name"] == "release.quick"


@pytest.mark.parametrize(
    ("args", "json_mode"),
    [
        (["--show-output"], False),
        (["--json", "--show-output"], True),
    ],
)
def test_validate_release_cli_show_output_streams_child_report_logs(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    json_mode: bool,
) -> None:
    parent_dir = tmp_path / "parent"
    child_dir = tmp_path / "child"
    parent_dir.mkdir()
    child_dir.mkdir()
    parent_stdout = parent_dir / "stdout.log"
    parent_stderr = parent_dir / "stderr.log"
    parent_report = parent_dir / "report.json"
    child_stdout = child_dir / "stdout.log"
    child_stderr = child_dir / "stderr.log"
    child_report = child_dir / "report.json"
    parent_stdout.write_text("release stdout\n", encoding="utf-8")
    parent_stderr.write_text("release stderr\n", encoding="utf-8")
    child_stdout.write_text("child pytest stdout\n", encoding="utf-8")
    child_stderr.write_text("child pytest stderr\n", encoding="utf-8")

    child_run = _validation_run(
        run_id="20260523T120001Z-quick-child",
        status="fail",
        exit_code=1,
        tool_exit_codes={"pytest": 1},
        checks=[
            ValidationCheck(
                name="pytest.quick",
                status="fail",
                duration_s=1.0,
                artifacts={
                    "stdout": ArtifactRef(kind="stdout", path=str(child_stdout)),
                    "stderr": ArtifactRef(kind="stderr", path=str(child_stderr)),
                },
            )
        ],
        artifacts={
            "report": ArtifactRef(kind="validation_report", path=str(child_report)),
            "stdout": ArtifactRef(kind="stdout", path=str(child_stdout)),
            "stderr": ArtifactRef(kind="stderr", path=str(child_stderr)),
        },
    )
    child_report.write_text(child_run.to_json(), encoding="utf-8")

    parent_run = _validation_run(
        run_id="20260523T120000Z-release-parent",
        command=["easycat", "validate", "release"],
        status="fail",
        exit_code=1,
        tool_exit_codes={"release.quick": 1},
        checks=[
            ValidationCheck(
                name="release.quick",
                status="fail",
                duration_s=1.0,
                artifacts={
                    "report": ArtifactRef(kind="validation_report", path=str(child_report)),
                },
            )
        ],
        artifacts={
            "report": ArtifactRef(kind="validation_report", path=str(parent_report)),
            "stdout": ArtifactRef(kind="stdout", path=str(parent_stdout)),
            "stderr": ArtifactRef(kind="stderr", path=str(parent_stderr)),
            "quick_report": ArtifactRef(kind="validation_report", path=str(child_report)),
        },
    )
    parent_report.write_text(parent_run.to_json(), encoding="utf-8")

    def fake_run_release_validation(**kwargs) -> ValidationRunResult:
        return ValidationRunResult(
            run=parent_run,
            run_dir=parent_dir,
            report_path=parent_report,
            exit_code=1,
        )

    monkeypatch.setattr(
        "easycat.cli.validate.run_release_validation",
        fake_run_release_validation,
    )

    result = cli.invoke(app, ["validate", "release", *args])

    assert result.exit_code == 1
    assert "release stderr" in result.stderr
    assert "child pytest stderr" in result.stderr
    if json_mode:
        payload = json.loads(result.stdout)
        assert payload["command"] == "validate release"
        assert payload["status"] == "error"
        assert "release stdout" in result.stderr
        assert "child pytest stdout" in result.stderr
        assert "child pytest stdout" not in result.stdout
    else:
        assert "release stdout" in result.stdout
        assert "child pytest stdout" in result.stdout
        assert "release: fail" in result.stdout


def test_validate_release_cli_show_output_ignores_untrusted_child_report_log_paths(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_dir = tmp_path / "parent"
    child_dir = tmp_path / "child"
    outside_dir = tmp_path / "outside"
    parent_dir.mkdir()
    child_dir.mkdir()
    outside_dir.mkdir()
    parent_stdout = parent_dir / "stdout.log"
    parent_stderr = parent_dir / "stderr.log"
    parent_report = parent_dir / "report.json"
    child_report = child_dir / "report.json"
    outside_secret = outside_dir / "secret.txt"
    parent_stdout.write_text("release stdout\n", encoding="utf-8")
    parent_stderr.write_text("release stderr\n", encoding="utf-8")
    outside_secret.write_text("EASYCAT_POC_SECRET=child_report_arbitrary_read\n", encoding="utf-8")

    child_payload = {
        "kind": "validation_run",
        "schema_version": 1,
        "artifacts": {
            "stdout": {"kind": "not_stdout", "path": str(outside_secret)},
            "stderr": {"kind": "stderr", "path": str(outside_secret)},
        },
    }
    child_report.write_text(json.dumps(child_payload), encoding="utf-8")

    parent_run = _validation_run(
        run_id="20260523T120000Z-release-parent",
        command=["easycat", "validate", "release"],
        status="fail",
        exit_code=1,
        tool_exit_codes={"release.quick": 1},
        checks=[
            ValidationCheck(
                name="release.quick",
                status="fail",
                duration_s=1.0,
                artifacts={
                    "report": ArtifactRef(kind="validation_report", path=str(child_report)),
                },
            )
        ],
        artifacts={
            "report": ArtifactRef(kind="validation_report", path=str(parent_report)),
            "stdout": ArtifactRef(kind="stdout", path=str(parent_stdout)),
            "stderr": ArtifactRef(kind="stderr", path=str(parent_stderr)),
            "quick_report": ArtifactRef(kind="validation_report", path=str(child_report)),
        },
    )
    parent_report.write_text(parent_run.to_json(), encoding="utf-8")

    def fake_run_release_validation(**kwargs) -> ValidationRunResult:
        return ValidationRunResult(
            run=parent_run,
            run_dir=parent_dir,
            report_path=parent_report,
            exit_code=1,
        )

    monkeypatch.setattr(
        "easycat.cli.validate.run_release_validation",
        fake_run_release_validation,
    )

    result = cli.invoke(app, ["validate", "release", "--show-output"])

    assert result.exit_code == 1
    assert "release stdout" in result.stdout
    assert "release stderr" in result.stderr
    assert "EASYCAT_POC_SECRET" not in result.stdout
    assert "EASYCAT_POC_SECRET" not in result.stderr


def test_validate_release_cli_rejects_conflicting_latency_modes(cli: CliRunner) -> None:
    result = cli.invoke(
        app,
        ["validate", "release", "--latency-smoke", "--latency-sweep"],
    )

    assert result.exit_code == 2
    assert "choose only one of --latency-smoke or --latency-sweep" in result.stdout


def test_validate_release_cli_rejects_conflicting_latency_modes_json_envelope(
    cli: CliRunner,
) -> None:
    result = cli.invoke(
        app,
        ["validate", "release", "--latency-smoke", "--latency-sweep", "--json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "validate release"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert "choose only one of --latency-smoke or --latency-sweep" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_journey_menu_lists_validate_after_registration(cli: CliRunner) -> None:
    result = cli.invoke(app, [])

    assert result.exit_code == 0
    assert "Validation" in result.stdout
    assert "validate" in result.stdout
