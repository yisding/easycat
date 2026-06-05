"""Dependency policy guards for optional extras and security-sensitive pins."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _locked_packages(name: str) -> list[dict]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return [package for package in lock["package"] if package["name"] == name]


def test_funasr_vad_extra_pins_fixed_onnx_and_python_range() -> None:
    """FunASR pulls ONNX transitively; keep its security floor explicit.

    ``funasr-onnx==0.4.1`` currently caps NumPy at ``<=1.26.4``. ONNX 1.21.0
    is the first fixed release for the open Dependabot ONNX advisories, but on
    Python 3.13+ it requires a newer NumPy through ``ml-dtypes``. Keep the
    supported FunASR extra range explicit until upstream resolves that split.
    """
    deps = _pyproject()["project"]["optional-dependencies"]["funasr-vad"]

    assert "onnx>=1.21.0; python_version < '3.13'" in deps
    for package_name in ("funasr-onnx", "modelscope", "jieba"):
        matches = [dep for dep in deps if dep.startswith(f"{package_name}>=")]
        assert matches, f"funasr-vad extra missing {package_name}"
        assert all("python_version < '3.13'" in dep for dep in matches)


def test_lockfile_does_not_pin_vulnerable_onnx() -> None:
    onnx_packages = _locked_packages("onnx")
    assert onnx_packages, "uv.lock should include ONNX while funasr-vad is supported"

    vulnerable = [
        package["version"]
        for package in onnx_packages
        if Version(package["version"]) < Version("1.21.0")
    ]
    assert not vulnerable, "uv.lock pins vulnerable ONNX versions: " + ", ".join(vulnerable)
