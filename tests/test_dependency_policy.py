"""Dependency policy guards for optional extras and security-sensitive pins."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _locked_packages(name: str) -> list[dict]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return [package for package in lock["package"] if package["name"] == name]


def _locked_package_names() -> set[str]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {package["name"] for package in lock["package"]}


def _requirement(deps: list[str], name: str) -> str:
    for dep in deps:
        if Requirement(dep).name == name:
            return dep
    raise AssertionError(f"{name!r} not declared in {deps}")


def test_funasr_vad_extra_uses_in_tree_runtime_dependencies() -> None:
    """FunASR VAD must not reintroduce the stale funasr-onnx dependency."""
    extras = _pyproject()["project"]["optional-dependencies"]
    deps = extras["funasr-vad"]
    names = {Requirement(dep).name for dep in deps}

    assert "kaldi-native-fbank>=1.22.3" in deps
    assert "numpy>=1.24.0" in deps
    # Reuse the canonical in-tree onnxruntime constraint shared by the sibling
    # ONNX VAD extras instead of pinning a divergent version.
    assert _requirement(deps, "onnxruntime") == _requirement(extras["silero-vad"], "onnxruntime")
    for package_name in ("funasr-onnx", "modelscope", "jieba", "onnx"):
        assert package_name not in names


def test_lockfile_does_not_keep_removed_funasr_onnx_dependency_tree() -> None:
    locked_names = _locked_package_names()

    for package_name in ("funasr-onnx", "modelscope", "jieba", "onnx"):
        assert package_name not in locked_names


def test_all_extra_includes_funasr_runtime_frontend_dependency() -> None:
    deps = _pyproject()["project"]["optional-dependencies"]["all"]

    assert "kaldi-native-fbank>=1.22.3" in deps


def test_lockfile_does_not_pin_vulnerable_onnx() -> None:
    onnx_packages = _locked_packages("onnx")
    vulnerable = [
        package["version"]
        for package in onnx_packages
        if Version(package["version"]) < Version("1.21.0")
    ]
    assert not vulnerable, "uv.lock pins vulnerable ONNX versions: " + ", ".join(vulnerable)
