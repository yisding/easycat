from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.validation.latency import ReliabilitySample, ReliabilitySignals
from easycat.validation.report import (
    ArtifactRef,
    GitMetadata,
    ProviderCheck,
    ProviderCheckState,
    ValidationCheck,
    ValidationEnvironment,
    ValidationFailure,
    ValidationRun,
    ValidationSkip,
)
from easycat.validation.runner import (
    CommandResult,
    ValidationRunResult,
    main,
    run_live_validation,
    run_validation_slice,
)


def _validation_run(**overrides) -> ValidationRun:  # noqa: ANN003
    values = {
        "run_id": "20260521T120000Z-quick-12345",
        "command": ["uv", "run", "pytest", "-q"],
        "started_at": datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 5, 21, 12, 0, 3, tzinfo=UTC),
        "duration_s": 3.25,
        "status": "pass",
        "exit_code": 0,
        "tool_exit_codes": {"pytest": 0},
        "git": GitMetadata(sha="abc123", branch="feature/validation", dirty=True),
        "environment": ValidationEnvironment(
            python="3.12.13",
            platform="Linux",
            ci=False,
            env_vars={"OPENAI_API_KEY": True, "DEEPGRAM_API_KEY": False},
        ),
        "checks": [
            ValidationCheck(
                name="pytest.quick",
                status="pass",
                duration_s=2.75,
                command=["uv", "run", "pytest", "-q"],
                artifacts={
                    "junit": ArtifactRef(
                        kind="junit",
                        path=".easycat/validation/runs/20260521T120000Z-quick-12345/junit.xml",
                    )
                },
            )
        ],
    }
    values.update(overrides)
    return ValidationRun(**values)


def test_validation_run_serializes_required_fields_deterministically() -> None:
    run = _validation_run()

    payload = run.to_dict()

    assert payload["schema_version"] == 1
    assert payload["redaction_version"] == 1
    assert payload["kind"] == "validation_run"
    assert payload["run_id"] == "20260521T120000Z-quick-12345"
    assert payload["command"] == ["uv", "run", "pytest", "-q"]
    assert payload["started_at"] == "2026-05-21T12:00:00Z"
    assert payload["finished_at"] == "2026-05-21T12:00:03Z"
    assert payload["duration_s"] == 3.25
    assert payload["status"] == "pass"
    assert payload["exit_code"] == 0
    assert payload["tool_exit_codes"] == {"pytest": 0}
    assert payload["git"] == {"branch": "feature/validation", "dirty": True, "sha": "abc123"}
    assert payload["environment"]["env_vars"] == {
        "DEEPGRAM_API_KEY": False,
        "OPENAI_API_KEY": True,
    }
    assert payload["checks"][0]["artifacts"]["junit"] == {
        "kind": "junit",
        "path": ".easycat/validation/runs/20260521T120000Z-quick-12345/junit.xml",
    }
    assert payload["skips"] == []
    assert payload["failures"] == []
    assert payload["latency"] is None
    assert payload["providers"] == []
    assert payload["provider_reports"] == []
    assert payload["extras"] == []
    assert payload["artifacts"] == {}

    assert run.to_json() == run.to_json()
    assert json.loads(run.to_json()) == payload


def test_validation_report_redacts_secret_like_and_unsafe_values() -> None:
    secret = "sk-" + ("a" * 32)
    run = _validation_run(
        command=[
            "uv",
            "run",
            "pytest",
            f"--api-key={secret}",
            "https://api.example.test/v1?token=hidden-token",
        ],
        environment=ValidationEnvironment(
            python="3.12.13",
            platform="Linux",
            ci=False,
            env_vars={"OPENAI_API_KEY": True},
        ),
        failures=[
            ValidationFailure(
                name="pytest.quick",
                message=(
                    f"Authorization: Bearer {secret}; request req_123456789; "
                    "phone +1 (415) 555-2671; file /home/alice/project/test.py"
                ),
            )
        ],
    )

    serialized = run.to_json()

    assert secret not in serialized
    assert "hidden-token" not in serialized
    assert "https://api.example.test" not in serialized
    assert "+1 (415) 555-2671" not in serialized
    assert "req_123456789" not in serialized
    assert "/home/alice" not in serialized
    assert "OPENAI_API_KEY" in serialized
    assert "env_vars" in serialized


def test_validation_schema_represents_pass_fail_and_expected_skip() -> None:
    run = _validation_run(
        status="fail",
        exit_code=1,
        tool_exit_codes={"pytest": 1},
        checks=[
            ValidationCheck(name="pytest.quick", status="pass", duration_s=1.0),
            ValidationCheck(name="pytest.socket", status="fail", duration_s=1.0),
            ValidationCheck(name="provider.openai", status="skip", duration_s=0.0),
        ],
        skips=[ValidationSkip(name="provider.openai", reason="OPENAI_API_KEY missing")],
        failures=[ValidationFailure(name="pytest.socket", message="1 test failed")],
    )

    payload = run.to_dict()

    assert payload["status"] == "fail"
    assert [check["status"] for check in payload["checks"]] == ["pass", "fail", "skip"]
    assert payload["skips"] == [
        {"expected": True, "name": "provider.openai", "reason": "OPENAI_API_KEY missing"}
    ]
    assert payload["failures"] == [{"message": "1 test failed", "name": "pytest.socket"}]


def test_provider_states_distinguish_expected_skip_from_required_secret_failure() -> None:
    assert {state.value for state in ProviderCheckState} == {
        "not_requested",
        "skipped_missing_secret",
        "failed_missing_required_secret",
        "passed",
        "failed",
    }

    run = _validation_run(
        providers=[
            ProviderCheck(
                provider="openai",
                surface="stt",
                state=ProviderCheckState.SKIPPED_MISSING_SECRET,
                credential_env="OPENAI_API_KEY",
            ),
            ProviderCheck(
                provider="deepgram",
                surface="stt",
                state=ProviderCheckState.FAILED_MISSING_REQUIRED_SECRET,
                credential_env="DEEPGRAM_API_KEY",
                required=True,
            ),
        ]
    )

    assert run.to_dict()["providers"] == [
        {
            "credential_env": "OPENAI_API_KEY",
            "provider": "openai",
            "required": False,
            "state": "skipped_missing_secret",
            "surface": "stt",
        },
        {
            "credential_env": "DEEPGRAM_API_KEY",
            "provider": "deepgram",
            "required": True,
            "state": "failed_missing_required_secret",
            "surface": "stt",
        },
    ]


def test_live_validation_skips_missing_secret_in_non_strict_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    commands: list[list[str]] = []

    def fake_command_runner(command: list[str]) -> CommandResult:
        commands.append(command)
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_live_validation(
        providers=["openai"],
        surfaces=["stt"],
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    assert result.exit_code == 0
    assert commands == []
    assert payload["status"] == "pass"
    assert payload["providers"][0]["state"] == "skipped_missing_secret"
    assert payload["skips"][0]["expected"] is True
    assert payload["provider_reports"][0]["status"] == "expected_skip"
    assert payload["provider_reports"][0]["auth"]["credential_env_var_present"] is False


def test_live_validation_fails_missing_secret_for_explicit_strict_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = run_live_validation(
        providers=["openai"],
        surfaces=["stt"],
        strict=True,
        artifacts_dir=tmp_path,
        command_runner=lambda command: CommandResult(exit_code=0, stdout="", stderr=""),
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert payload["status"] == "fail"
    assert payload["providers"][0]["state"] == "failed_missing_required_secret"
    assert payload["failures"][0]["failure_class"] == "auth_or_quota"
    assert payload["provider_reports"][0]["status"] == "auth_failure"
    assert payload["provider_reports"][0]["failure_class"] == "auth_or_quota"


def test_live_validation_runs_configured_provider_and_redacts_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + ("c" * 32)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    commands: list[list[str]] = []

    def fake_command_runner(command: list[str]) -> CommandResult:
        commands.append(command)
        return CommandResult(exit_code=0, stdout=f"ok {secret}", stderr="")

    result = run_live_validation(
        providers=["openai"],
        surfaces=["stt"],
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    serialized = result.report_path.read_text()
    assert result.exit_code == 0
    assert commands
    assert commands[0][:4] == ["uv", "run", "pytest", "-q"]
    assert "tests/stt/test_stt_openai.py::test_live_openai_stt" in commands[0]
    assert commands[0][-2:] == [
        "-m",
        "integration_live and provider_openai and surface_stt and not flaky",
    ]
    assert secret not in serialized
    assert payload["providers"][0]["state"] == "passed"
    assert payload["provider_reports"][0]["status"] == "pass"
    assert payload["provider_reports"][0]["capabilities"]["streaming"] is False
    report_artifact = result.run.artifacts["provider_openai_stt"].path
    assert Path(report_artifact).exists()


def test_live_validation_rejects_unknown_provider_selector(tmp_path: Path) -> None:
    result = run_live_validation(
        providers=["opneai"],
        artifacts_dir=tmp_path,
        command_runner=lambda command: CommandResult(exit_code=0, stdout="", stderr=""),
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert payload["status"] == "fail"
    assert payload["checks"][0]["name"] == "provider.selector"
    assert payload["failures"][0]["message"] == "unknown live provider selector: opneai"
    assert payload["provider_reports"] == []


def test_live_validation_redacts_exact_runtime_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "plain-runtime-token-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    def fake_command_runner(command: list[str]) -> CommandResult:
        return CommandResult(exit_code=1, stdout=f"stdout {secret}", stderr=f"stderr {secret}")

    result = run_live_validation(
        providers=["openai"],
        surfaces=["stt"],
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    serialized = result.report_path.read_text()
    assert secret not in serialized
    assert secret not in (result.run_dir / "stdout.log").read_text()
    assert secret not in (result.run_dir / "stderr.log").read_text()
    assert "[REDACTED_SECRET]" in serialized


def test_live_validation_preserves_provider_quota_failure_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "plain-runtime-token-value")

    result = run_live_validation(
        providers=["openai"],
        surfaces=["stt"],
        artifacts_dir=tmp_path,
        command_runner=lambda command: CommandResult(exit_code=1, stderr="429 quota exceeded"),
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert payload["failures"][0]["failure_class"] == "provider_quota"
    assert payload["provider_reports"][0]["status"] == "quota_failure"
    assert payload["provider_reports"][0]["failure_class"] == "provider_quota"


def test_live_validation_release_mode_is_audited_in_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = run_live_validation(
        providers=["openai"],
        surfaces=["stt"],
        release=True,
        artifacts_dir=tmp_path,
        command_runner=lambda command: CommandResult(exit_code=0, stdout="", stderr=""),
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    assert "--release" in payload["command"]


def test_validation_runner_quick_writes_report_junit_logs_and_latest(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    secret = "sk-" + ("b" * 32)

    def fake_command_runner(command: list[str]) -> CommandResult:
        commands.append(command)
        junit_arg = next(arg for arg in command if arg.startswith("--junitxml="))
        Path(junit_arg.removeprefix("--junitxml=")).write_text("<testsuite />")
        return CommandResult(exit_code=0, stdout=f"ok {secret}", stderr="")

    result = run_validation_slice(
        "quick",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert result.exit_code == 0
    assert len(commands) == 1
    command = commands[0]
    assert command[:4] == ["uv", "run", "pytest", "-q"]
    assert command[-2:] == [
        "-m",
        (
            "not integration_socket and not integration_live and not slow "
            "and not stress and not flaky"
        ),
    ]
    assert any(arg.startswith("--junitxml=") for arg in command)

    report_path = result.run_dir / "report.json"
    latest_path = tmp_path / "latest.json"
    stdout_path = result.run_dir / "stdout.log"
    assert report_path.exists()
    assert latest_path.read_text() == report_path.read_text()
    assert secret not in stdout_path.read_text()

    payload = json.loads(report_path.read_text())
    assert payload["status"] == "pass"
    assert payload["exit_code"] == 0
    assert payload["tool_exit_codes"] == {"pytest": 0}
    assert payload["checks"][0]["name"] == "pytest.quick"
    assert payload["checks"][0]["artifacts"]["junit"]["path"].endswith("/junit.xml")


def test_validation_runner_embeds_reliability_samples_for_stress_slices(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str]) -> CommandResult:
        reliability_path = Path(os.environ["EASYCAT_RELIABILITY_SAMPLES_PATH"])
        reliability_path.write_text(
            json.dumps(
                [
                    ReliabilitySample(
                        sample_id="stress-1",
                        condition_id="fifty_turns_single_session_scripted",
                        mode="stress",
                        informational=True,
                        eligible=False,
                        signals=ReliabilitySignals(
                            journal_degraded=False,
                            active_sessions=1,
                            memory_growth_kib=128,
                        ),
                    ).to_dict()
                ]
            )
        )
        return CommandResult(exit_code=0, stdout="", stderr="")

    result = run_validation_slice(
        "stress",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    report = json.loads(result.report_path.read_text())
    assert report["status"] == "pass"
    assert report["reliability"]["kind"] == "reliability_validation"
    assert report["reliability"]["samples"][0]["sample_id"] == "stress-1"
    assert report["reliability"]["samples"][0]["signals"]["journal_degraded"] is False
    assert "queue_depth" not in report["reliability"]["samples"][0]["signals"]
    assert "reliability" in report["checks"][0]["artifacts"]


def test_validation_runner_failed_pytest_still_writes_report(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str]) -> CommandResult:
        return CommandResult(exit_code=5, stdout="", stderr="no tests collected")

    result = run_validation_slice(
        "socket",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    payload = json.loads((result.run_dir / "report.json").read_text())
    assert payload["status"] == "fail"
    assert payload["exit_code"] == 1
    assert payload["tool_exit_codes"] == {"pytest": 5}
    assert (tmp_path / "latest.json").exists()


def test_validation_runner_creates_isolated_run_directories(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str]) -> CommandResult:
        return CommandResult(exit_code=0, stdout="", stderr="")

    first = run_validation_slice(
        "quick",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
    )
    second = run_validation_slice(
        "quick",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert first.run_dir != second.run_dir
    assert first.run_dir.exists()
    assert second.run_dir.exists()


def test_validation_main_dispatches_socket_slice(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_command_runner(command: list[str]) -> CommandResult:
        commands.append(command)
        return CommandResult(exit_code=0, stdout="", stderr="")

    exit_code = main(
        ["socket", "--artifacts-dir", str(tmp_path)],
        command_runner=fake_command_runner,
    )

    assert exit_code == 0
    assert commands[0][-2:] == ["-m", "integration_socket and not integration_live and not flaky"]


def test_validation_main_dispatches_stress_slice(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_command_runner(command: list[str]) -> CommandResult:
        commands.append(command)
        return CommandResult(exit_code=0, stdout="", stderr="")

    exit_code = main(
        ["stress", "--artifacts-dir", str(tmp_path)],
        command_runner=fake_command_runner,
    )

    assert exit_code == 0
    assert commands[0][-2:] == ["-m", "stress and not integration_live and not flaky"]


def test_validation_runner_can_use_installed_wheel_pytest_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setenv("EASYCAT_VALIDATION_PYTEST_COMMAND", "/tmp/venv/bin/python -m pytest")
    monkeypatch.setenv("EASYCAT_VALIDATION_TEST_PATHS", f"/repo/tests{os.pathsep}/repo/smoke")

    def fake_command_runner(command: list[str]) -> CommandResult:
        commands.append(command)
        return CommandResult(exit_code=0, stdout="", stderr="")

    run_validation_slice(
        "quick",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert commands[0][:6] == [
        "/tmp/venv/bin/python",
        "-m",
        "pytest",
        "-q",
        "/repo/tests",
        "/repo/smoke",
    ]


def test_validate_quick_cli_writes_report_and_prints_human_summary(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "validation.json"
    called: dict[str, object] = {}

    def fake_run_validation_slice(slice_name: str, **kwargs) -> ValidationRunResult:  # noqa: ANN003
        called["slice_name"] = slice_name
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
    def fake_run_validation_slice(slice_name: str, **kwargs) -> ValidationRunResult:  # noqa: ANN003
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


def test_validate_socket_cli_returns_validation_exit_code(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_validation_slice(slice_name: str, **kwargs) -> ValidationRunResult:  # noqa: ANN003
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

    def fake_run_live_validation(**kwargs) -> ValidationRunResult:  # noqa: ANN003
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


def test_validate_report_cli_renders_summary(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    junit_path = tmp_path / "junit.xml"
    junit_path.write_text("<testsuite />")
    run = _validation_run(
        artifacts={
            "junit": ArtifactRef(kind="junit", path=str(junit_path)),
            "missing": ArtifactRef(kind="log", path=str(tmp_path / "missing.log")),
        },
        skips=[ValidationSkip(name="provider.openai", reason="OPENAI_API_KEY missing")],
    )
    report_path.write_text(run.to_json())

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 0
    assert "validation_run" in result.stdout
    assert "pytest.quick" in result.stdout
    assert "git: feature/validation abc123 dirty=True" in result.stdout
    assert "skip: provider.openai expected=True OPENAI_API_KEY missing" in result.stdout
    assert f"artifact junit: {junit_path}" in result.stdout
    assert f"artifact missing: {tmp_path / 'missing.log'} [missing]" in result.stdout


def test_validate_report_cli_rejects_invalid_json(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text("{no")

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 2
    assert "invalid validation report JSON" in result.stdout


def test_validate_report_cli_rejects_missing_report(cli: CliRunner, tmp_path: Path) -> None:
    result = cli.invoke(app, ["validate", "report", str(tmp_path / "missing.json")])

    assert result.exit_code == 2
    assert "validation report not found" in result.stdout


def test_validate_report_cli_rejects_unsupported_schema(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    payload = _validation_run().to_dict()
    payload["schema_version"] = 999
    report_path.write_text(json.dumps(payload))

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 2
    assert "unsupported validation report schema_version: 999" in result.stdout


def test_validate_report_cli_rejects_unknown_kind(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    payload = _validation_run().to_dict()
    payload["kind"] = "other"
    report_path.write_text(json.dumps(payload))

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 2
    assert "unknown validation report kind: other" in result.stdout


def test_validate_report_cli_renders_latency_percentiles(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    """When latency.percentiles is present, render a one-line summary per stage."""
    report_path = tmp_path / "report.json"
    run = _validation_run(
        latency={
            "schema_version": 1,
            "kind": "latency_validation",
            "mode": "sweep",
            "generated_at": "2026-05-22T12:00:00Z",
            "baseline": {"comparison": "not_configured"},
            "environment": {},
            "clock_source": "time.monotonic",
            "samples": [],
            "reliability_samples": [],
            "summary": {},
            "percentiles": {
                "overall": {
                    "total_ms": {
                        "p50": 500.0,
                        "p90": 900.0,
                        "p95": 1100.0,
                        "p99": 1300.0,
                        "count": 20,
                    },
                    "tts_ttfb_ms": {
                        "p50": 80.0,
                        "p90": 120.0,
                        "p95": 150.0,
                        "p99": 180.0,
                        "count": 20,
                    },
                },
                "by_condition": {},
            },
            "budget_violations": [],
        },
    )
    report_path.write_text(run.to_json())

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 0
    # Each stage rendered on its own line with p50/p95/p99 figures.
    assert "total_ms" in result.stdout
    assert "p50=500" in result.stdout
    assert "p95=1100" in result.stdout
    assert "p99=1300" in result.stdout
    assert "tts_ttfb_ms" in result.stdout
    assert "p95=150" in result.stdout


def test_validate_report_cli_renders_failed_run_details(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        _validation_run(
            status="fail",
            exit_code=1,
            tool_exit_codes={"pytest": 1},
            failures=[
                ValidationFailure(
                    name="pytest.quick",
                    message="1 test failed",
                    failure_class="easycat_regression",
                )
            ],
        ).to_json()
    )

    result = cli.invoke(app, ["validate", "report", str(report_path)])

    assert result.exit_code == 1
    assert "failure: pytest.quick easycat_regression 1 test failed" in result.stdout


def test_journey_menu_lists_validate_after_registration(cli: CliRunner) -> None:
    result = cli.invoke(app, [])

    assert result.exit_code == 0
    assert "Validation" in result.stdout
    assert "validate" in result.stdout
