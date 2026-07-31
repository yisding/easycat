from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from easycat.validation import _reliability_policy
from easycat.validation._runner_support import CommandResult
from easycat.validation._slice_runner import run_validation_slice


@pytest.mark.parametrize("pytest_exit_code", [1, 2, 3, 4, 5, 6])
def test_slice_runner_normalizes_every_nonzero_pytest_exit(
    tmp_path: Path,
    pytest_exit_code: int,
) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        return CommandResult(exit_code=pytest_exit_code)

    result = run_validation_slice(
        "quick",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    assert result.run.status == "fail"
    assert result.run.tool_exit_codes == {"pytest": pytest_exit_code}
    assert result.run.failures[0].name == "pytest.quick"


def test_slice_runner_fails_on_corrupt_reliability_artifact(tmp_path: Path) -> None:
    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        Path(env["EASYCAT_RELIABILITY_SAMPLES_PATH"]).write_text("{not-json")
        return CommandResult(exit_code=0)

    result = run_validation_slice(
        "stress",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    assert result.run.tool_exit_codes == {"pytest": 0, "reliability_samples": 1}
    assert result.run.failures[0].failure_class == "reliability_artifact_error"
    assert "reliability" in result.run.artifacts


def test_slice_runner_parses_reliability_artifact_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_calls = 0
    original_load = _reliability_policy.load_reliability_samples

    def tracking_load(raw: str):
        nonlocal load_calls
        load_calls += 1
        return original_load(raw)

    monkeypatch.setattr(_reliability_policy, "load_reliability_samples", tracking_load)

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        Path(env["EASYCAT_RELIABILITY_SAMPLES_PATH"]).write_text("[]")
        return CommandResult(exit_code=0)

    result = run_validation_slice(
        "stress",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )

    assert result.exit_code == 0
    assert load_calls == 1


def test_slice_runner_redacts_structured_secondary_artifacts_before_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = 'plain-"runtime\\token-value'
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", secret)

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        Path(env["EASYCAT_RELIABILITY_SAMPLES_PATH"]).write_text(
            json.dumps(
                [
                    {
                        "sample_id": "socket-1",
                        "condition_id": "socket",
                        "mode": "socket",
                        "informational": True,
                        "eligible": False,
                        "signals": {"unavailable_reason": secret},
                    }
                ]
            )
        )
        Path(env["EASYCAT_WEBRTC_STATS_PATH"]).write_text(
            json.dumps({"credential_echo": secret}) + "\n"
        )
        return CommandResult(exit_code=0)

    result = run_validation_slice(
        "socket",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )

    reliability = Path(result.run.artifacts["reliability"].path).read_text()
    webrtc_stats = Path(result.run.artifacts["webrtc_stats"].path).read_text()
    assert result.exit_code == 0
    assert secret not in reliability
    assert secret not in webrtc_stats
    assert json.loads(reliability)[0]["signals"]["unavailable_reason"] == "[REDACTED_SECRET]"
    assert json.loads(webrtc_stats)["credential_echo"] == "[REDACTED_SECRET]"


def test_slice_runner_excludes_non_utf8_artifact_after_scrubbing_secret_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "plain-runtime-token-value"
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", secret)
    stats_path: Path | None = None

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        nonlocal stats_path
        stats_path = Path(env["EASYCAT_WEBRTC_STATS_PATH"])
        stats_path.write_bytes(b"\xffcredential=" + secret.encode())
        return CommandResult(exit_code=0)

    result = run_validation_slice(
        "socket",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    assert "webrtc_stats" not in result.run.artifacts
    assert result.run.tool_exit_codes["artifact_redaction"] == 1
    assert any(
        failure.name == "artifact_redaction.webrtc_stats"
        and failure.failure_class == "artifact_redaction_error"
        for failure in result.run.failures
    )
    assert stats_path is not None
    artifact = stats_path.read_bytes()
    assert secret.encode() not in artifact
    assert b"[REDACTED_SECRET]" in artifact


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            '{"credential_echo":"plain-runtime-token-value","too_large":' + "1" * 5000 + "}",
            id="oversized-json-integer",
        ),
        pytest.param(
            '{"credential_echo":"plain-runtime-token-value","nested":'
            + "[" * 2000
            + "0"
            + "]" * 2000
            + "}",
            id="recursive-json",
        ),
    ],
)
def test_slice_runner_fails_closed_when_structured_redaction_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    secret = "plain-runtime-token-value"
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", secret)

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        Path(env["EASYCAT_WEBRTC_STATS_PATH"]).write_text(payload)
        return CommandResult(exit_code=0)

    result = run_validation_slice(
        "socket",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    assert "webrtc_stats" not in result.run.artifacts
    assert result.run.tool_exit_codes["artifact_redaction"] == 1
    assert any(
        failure.name == "artifact_redaction.webrtc_stats"
        and failure.failure_class == "artifact_redaction_error"
        for failure in result.run.failures
    )
    assert secret not in result.report_path.read_text()


@pytest.mark.parametrize("failure_phase", ["read", "write"])
def test_slice_runner_does_not_publish_artifact_when_redaction_io_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    secret = "plain-runtime-token-value"
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", secret)
    original_read_text = Path.read_text
    original_write_text = Path.write_text
    stats_path: Path | None = None

    def guarded_read_text(path: Path, *args, **kwargs):
        if failure_phase == "read" and path == stats_path:
            raise PermissionError("simulated unreadable artifact")
        return original_read_text(path, *args, **kwargs)

    def guarded_write_text(path: Path, *args, **kwargs):
        if failure_phase == "write" and path == stats_path:
            raise PermissionError("simulated artifact write failure")
        return original_write_text(path, *args, **kwargs)

    def fake_command_runner(command: list[str], *, env: dict[str, str]) -> CommandResult:
        nonlocal stats_path
        stats_path = Path(env["EASYCAT_WEBRTC_STATS_PATH"])
        original_write_text(stats_path, secret)
        monkeypatch.setattr(Path, "read_text", guarded_read_text)
        monkeypatch.setattr(Path, "write_text", guarded_write_text)
        return CommandResult(exit_code=0)

    result = run_validation_slice(
        "socket",
        artifacts_dir=tmp_path,
        command_runner=fake_command_runner,
        started_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )

    assert result.exit_code == 1
    assert "webrtc_stats" not in result.run.artifacts
    assert result.run.tool_exit_codes["artifact_redaction"] == 1
    assert any(
        failure.name == "artifact_redaction.webrtc_stats"
        and failure.failure_class == "artifact_redaction_error"
        for failure in result.run.failures
    )
    assert any(
        check.name == "validation.artifact_redaction" and check.status == "fail"
        for check in result.run.checks
    )
    assert stats_path is not None
    assert secret in original_read_text(stats_path)
