from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_wheel_size import MAX_WHEEL_BYTES, MAX_WHEEL_MIB, MIB, main, wheel_size


def test_default_wheel_budget_is_thirteen_mib() -> None:
    assert MAX_WHEEL_MIB == 13
    assert MAX_WHEEL_BYTES == 13 * MIB


def test_wheel_size_accepts_a_wheel_at_the_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel = tmp_path / "easycat-test.whl"
    wheel.write_bytes(b"x" * MIB)

    assert main([str(wheel), "--max-mib", "1"]) == 0
    assert "wheel-size: PASS" in capsys.readouterr().out
    assert wheel_size(wheel) == MIB


def test_wheel_size_rejects_a_wheel_over_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel = tmp_path / "easycat-test.whl"
    wheel.write_bytes(b"x" * (MIB + 1))

    assert main([str(wheel), "--max-mib", "1"]) == 1
    assert "wheel-size: FAIL" in capsys.readouterr().err


def test_wheel_size_rejects_missing_and_non_wheel_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_wheel = tmp_path / "easycat.tar.gz"
    not_wheel.write_text("not a wheel", encoding="utf-8")

    assert main([str(tmp_path / "missing.whl"), str(not_wheel)]) == 1
    stderr = capsys.readouterr().err
    assert "does not exist" in stderr
    assert "is not a .whl artifact" in stderr


def test_build_smoke_enforces_wheel_budget_before_twine() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    build_smoke = workflow.split("  build-smoke:", 1)[1]

    build = build_smoke.index("- run: uv build")
    budget = build_smoke.index("python scripts/check_wheel_size.py dist/*.whl")
    twine = build_smoke.index("- run: uvx twine check dist/*")

    assert build < budget < twine
