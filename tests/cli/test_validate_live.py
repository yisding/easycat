from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from easycat.validation.runner import CommandResult, run_live_validation

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_live_validation_skips_missing_secret_in_non_strict_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    commands: list[list[str]] = []

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
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
        command_runner=lambda command, *, env: CommandResult(exit_code=0, stdout="", stderr=""),
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert payload["status"] == "fail"
    assert payload["providers"][0]["state"] == "failed_missing_required_secret"
    assert payload["failures"][0]["failure_class"] == "auth_or_quota"
    assert payload["provider_reports"][0]["status"] == "auth_failure"
    assert payload["provider_reports"][0]["failure_class"] == "auth_or_quota"


def test_live_validation_release_fails_missing_required_prerequisite_without_provider_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    commands: list[list[str]] = []

    result = run_live_validation(
        surfaces=["agent_bridge"],
        release=True,
        artifacts_dir=tmp_path,
        command_runner=lambda command, *, env: (
            commands.append(command) or CommandResult(exit_code=0, stdout="", stderr="")
        ),
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    assert result.exit_code == 1
    assert commands == []
    assert payload["command"] == [
        "easycat",
        "validate",
        "live",
        "--surface",
        "agent_bridge",
        "--release",
    ]
    assert payload["providers"][0] == {
        "provider": "openai-agents",
        "surface": "agent_bridge",
        "state": "failed_missing_required_secret",
        "credential_env": "OPENAI_API_KEY",
        "required": True,
        "failure_class": "auth_or_quota",
    }
    assert payload["failures"][0]["failure_class"] == "auth_or_quota"
    assert payload["provider_reports"][0]["status"] == "auth_failure"
    assert payload["provider_reports"][0]["auth"]["credential_env_var_present"] is False


def test_live_validation_runs_configured_provider_and_redacts_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + ("c" * 32)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    commands: list[list[str]] = []

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
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
        command_runner=lambda command, *, env: CommandResult(exit_code=0, stdout="", stderr=""),
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

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
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
        command_runner=lambda command, *, env: CommandResult(
            exit_code=1, stderr="429 quota exceeded"
        ),
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
        command_runner=lambda command, *, env: CommandResult(exit_code=0, stdout="", stderr=""),
        started_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.report_path.read_text())
    assert "--release" in payload["command"]
