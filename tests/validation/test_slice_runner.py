from __future__ import annotations

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
