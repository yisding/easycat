"""Dependency policy guards for optional extras and security-sensitive pins."""

from __future__ import annotations

import re
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


def test_all_extra_is_union_of_non_conflicting_extras() -> None:
    """``all`` must be the union of every extra except three deliberate exclusions."""
    extras = _pyproject()["project"]["optional-dependencies"]
    # ten-vad: non-permissive license. pydantic-ai / pydantic-ai-v2-beta:
    # mutually exclusive via [tool.uv].conflicts.
    excluded = {"ten-vad", "pydantic-ai", "pydantic-ai-v2-beta"}

    # Stale-exclusion guard (mirrors scripts/extras_matrix.py contract).
    assert excluded <= set(extras), "exclusion set names an extra pyproject no longer declares"

    union: set[str] = set()
    for name, deps in extras.items():
        if name == "all" or name in excluded:
            continue
        union |= set(deps)

    assert set(extras["all"]) == union


def test_lockfile_does_not_pin_vulnerable_onnx() -> None:
    onnx_packages = _locked_packages("onnx")
    vulnerable = [
        package["version"]
        for package in onnx_packages
        if Version(package["version"]) < Version("1.21.0")
    ]
    assert not vulnerable, "uv.lock pins vulnerable ONNX versions: " + ", ".join(vulnerable)


def test_project_declares_license_metadata() -> None:
    project = _pyproject()["project"]

    assert project["license"] == "BSD-2-Clause"
    assert project["license-files"] == ["LICENSE"]
    assert (REPO_ROOT / "LICENSE").exists()


def test_mypy_gated_paths_match_pyproject_overrides() -> None:
    justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    match = re.search(r'^mypy_gated_paths\s*:=\s*"(?P<paths>[^"]+)"', justfile, re.MULTILINE)
    assert match is not None, "justfile `mypy_gated_paths` variable not found"
    expected_modules = [
        path.removeprefix("src/").replace("/", ".") + ".*" for path in match.group("paths").split()
    ]
    overrides = _pyproject()["tool"]["mypy"]["overrides"]
    gated = next(o for o in overrides if o.get("check_untyped_defs"))
    assert gated["module"] == expected_modules


def _ruff_lint() -> dict:
    return _pyproject()["tool"]["ruff"]["lint"]


def test_ruff_lint_enables_async_bugbear_and_ruf006() -> None:
    """QW9: flake8-async/bugbear + RUF006 stay in the ruff select set."""
    select = set(_ruff_lint()["select"])
    assert {"ASYNC", "B", "RUF006"} <= select


def test_ruff_permanently_ignores_async109() -> None:
    """Explicit async timeout params (incl. timeouts.py) are deliberate public API."""
    assert "ASYNC109" in _ruff_lint().get("ignore", [])


def test_ruff_flake8_bugbear_treats_typer_defaults_as_immutable() -> None:
    """typer.Option / typer.Argument defaults are the Typer idiom, not B008 bugs."""
    bugbear = _ruff_lint()["flake8-bugbear"]
    assert bugbear["extend-immutable-calls"] == ["typer.Option", "typer.Argument"]


def test_rule_list_docs_stay_in_sync_with_ruff_select() -> None:
    """CLAUDE.md and justfile rule-list prose must mirror tool.ruff.lint.select."""
    expected = ", ".join(_ruff_lint()["select"])

    claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert f"- Ruff rules: {expected}" in claude_md

    justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    assert f"# Lint with ruff ({expected})." in justfile
