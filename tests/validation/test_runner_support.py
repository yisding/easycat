from __future__ import annotations

from pathlib import Path

import pytest

from easycat.validation._runner_support import (
    ValidationSourceCheckoutError,
    ensure_validation_source_checkout,
    pytest_command_prefix,
    redact_validation_artifacts,
)


def _clear_validation_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "EASYCAT_VALIDATION_PYTEST_COMMAND",
        "EASYCAT_VALIDATION_TEST_PATHS",
        "EASYCAT_VALIDATION_TEST_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_validation_requires_source_checkout_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_validation_overrides(monkeypatch)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(
        ValidationSourceCheckoutError,
        match="validation lanes require the EasyCat source checkout",
    ):
        pytest_command_prefix()


def test_validation_accepts_explicit_installed_wheel_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_validation_overrides(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "EASYCAT_VALIDATION_PYTEST_COMMAND",
        "/opt/easycat/python -m pytest",
    )
    monkeypatch.setenv("EASYCAT_VALIDATION_TEST_PATHS", "/source/tests")

    ensure_validation_source_checkout(test_override_mode="paths")

    assert pytest_command_prefix(test_override_mode="paths") == [
        "/opt/easycat/python",
        "-m",
        "pytest",
    ]


@pytest.mark.parametrize(
    ("configured_override", "required_mode", "missing_override"),
    [
        ("EASYCAT_VALIDATION_TEST_PATHS", "root", "EASYCAT_VALIDATION_TEST_ROOT"),
        ("EASYCAT_VALIDATION_TEST_ROOT", "paths", "EASYCAT_VALIDATION_TEST_PATHS"),
    ],
)
def test_validation_rejects_an_override_for_the_wrong_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_override: str,
    required_mode: str,
    missing_override: str,
) -> None:
    _clear_validation_overrides(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "EASYCAT_VALIDATION_PYTEST_COMMAND",
        "/opt/easycat/python -m pytest",
    )
    monkeypatch.setenv(configured_override, "/source/tests")

    with pytest.raises(ValidationSourceCheckoutError, match=missing_override):
        ensure_validation_source_checkout(test_override_mode=required_mode)


def test_artifact_redaction_allows_missing_optional_artifact(tmp_path: Path) -> None:
    failures = redact_validation_artifacts(
        [("samples", tmp_path / "missing.json", "json")],
        (),
    )

    assert failures == {}


def test_artifact_redaction_rejects_existing_non_utf8_artifact(tmp_path: Path) -> None:
    secret = "plain-runtime-token-value"
    artifact = tmp_path / "samples.json"
    artifact.write_bytes(b"\xffcredential=" + secret.encode())

    failures = redact_validation_artifacts(
        [("samples", artifact, "json")],
        (secret,),
    )

    assert failures["samples"].name == "artifact_redaction.samples"
    assert failures["samples"].failure_class == "artifact_redaction_error"
    assert secret.encode() not in artifact.read_bytes()
