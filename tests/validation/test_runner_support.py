from __future__ import annotations

from pathlib import Path

import pytest

from easycat.validation._runner_support import (
    ValidationSourceCheckoutError,
    ensure_validation_source_checkout,
    pytest_command_prefix,
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

    ensure_validation_source_checkout()

    assert pytest_command_prefix() == ["/opt/easycat/python", "-m", "pytest"]
