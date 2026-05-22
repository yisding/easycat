from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

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
from easycat.validation.runner import CommandResult, main, run_validation_slice


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
        "not integration_socket and not integration_live and not slow and not flaky",
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
